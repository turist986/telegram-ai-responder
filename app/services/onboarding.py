"""Мастер добавления аккаунта: прокси → api_id/api_hash (my.telegram.org) → вход в Telegram.

Обязательные правила:
  * без рабочего прокси мастер не стартует — ни один запрос (ни к my.telegram.org, ни к
    Telegram) не идёт с IP сервера;
  * прокси не должен использоваться другим аккаунтом (один аккаунт — один IP);
  * вход в Telegram выполняется теми же api_id, профилем устройства и прокси, с которыми
    аккаунт потом работает — «логин» и «работа» выглядят как одно и то же устройство;
  * пока идёт вход, сессия защищена SessionLock, а созданный аккаунт добавляется в БД
    выключенным: первые ответы лучше отложить (см. подсказку в панели).

Состояние мастера хранится в памяти процесса веб-панели (она работает с -w 1)."""
import asyncio
import datetime as dt
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy.orm import Session
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)

from ..config import settings
from ..models import Account, ApiCredential
from . import device_profile
from .api_app_creator import ApiAppCreator, CreatorError
from .proxy import ProxyConfigError, choose_ip_family, normalize_proxy, parse_proxy, test_proxy
from .session_lock import SessionInUseError, SessionLock
from .settings_store import get_protection

logger = logging.getLogger(__name__)


class OnboardingError(ValueError):
    """Ошибка, текст которой безопасно показать пользователю."""


# шаги: web_code → api_ready(переходный) → login_code → password → done | failed
@dataclass
class Onboarding:
    token: str
    identifier: str
    phone: str
    proxy: str
    manager_name: str
    lang: tuple[str, str]
    profile: dict
    session_path: Path
    step: str = "starting"
    message: str = ""
    api_id: int | None = None
    api_hash: str | None = None
    created_at: float = field(default_factory=time.monotonic)
    creator: ApiAppCreator | None = None
    client: TelegramClient | None = None
    phone_code_hash: str | None = None
    lock: SessionLock | None = None
    busy: asyncio.Lock = field(default_factory=asyncio.Lock)


_STATES: dict[str, Onboarding] = {}


def get_state(token: str) -> Onboarding | None:
    return _STATES.get(token)


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if not 7 <= len(digits) <= 15:
        raise OnboardingError("Введите номер телефона в международном формате, например +12223334455")
    return "+" + digits


def proxy_in_use(db: Session, proxy: str, exclude_identifier: str | None = None) -> str | None:
    """Идентификатор аккаунта, у которого уже стоит тот же host:port прокси, иначе None."""
    target = urlparse(proxy)
    for acc in db.query(Account).all():
        if not acc.proxy or acc.identifier == exclude_identifier:
            continue
        other = urlparse(acc.proxy)
        if (other.hostname, other.port) == (target.hostname, target.port):
            return acc.identifier
    return None


def validate_new_proxy(db: Session, raw_proxy: str, exclude_identifier: str | None = None) -> str:
    """Нормализует прокси, проверяет формат и уникальность. Бросает OnboardingError."""
    try:
        normalized = normalize_proxy(raw_proxy)
        if not normalized:
            raise OnboardingError("Прокси обязателен: без него аккаунт нельзя добавить")
        parse_proxy(normalized)
    except ProxyConfigError as exc:
        raise OnboardingError(str(exc)) from exc
    owner = proxy_in_use(db, normalized, exclude_identifier)
    if owner:
        raise OnboardingError(f"Этот прокси уже используется аккаунтом {owner}. Нужен отдельный прокси на каждый аккаунт")
    return normalized


def list_api_pools(db: Session) -> list[dict]:
    """Все известные «пулы» api_id: аккаунты, у которых уже стоит этот api_id/api_hash
    (собственное приложение, созданное через мастер или назначенное вручную), ПЛЮС
    загруженные через add_credentials пары, ещё ни к одному аккаунту не привязанные
    (count=0 — свободная ёмкость про запас). Один api_id может стоять сразу за
    несколькими аккаунтами: Telegram сам так устроен у официальных клиентов, это не
    признак связи аккаунтов — в отличие от общего прокси/IP, который у каждого
    аккаунта всегда свой."""
    groups: dict[int, dict] = {}
    for acc in db.query(Account).filter(Account.api_id.isnot(None), Account.api_hash.isnot(None)).all():
        g = groups.setdefault(acc.api_id, {"api_id": acc.api_id, "api_hash": acc.api_hash, "label": None, "members": []})
        g["members"].append(acc.identifier)
    for cred in db.query(ApiCredential).all():
        g = groups.setdefault(cred.api_id, {"api_id": cred.api_id, "api_hash": cred.api_hash, "label": None, "members": []})
        g["label"] = cred.label or g["label"]
        g["api_hash"] = g["api_hash"] or cred.api_hash
    return [
        {**g, "count": len(g["members"]), "members": sorted(g["members"])}
        for _, g in sorted(groups.items())
    ]


def _pool_api_hash(db: Session, api_id: int) -> str | None:
    # библиотека загруженных приложений — источник истины для ещё не назначенных пар
    cred = db.query(ApiCredential).filter_by(api_id=api_id).one_or_none()
    if cred:
        return cred.api_hash
    acc = (
        db.query(Account)
        .filter(Account.api_id == api_id, Account.api_hash.isnot(None))
        .first()
    )
    return acc.api_hash if acc else None


def _pool_count(db: Session, api_id: int, exclude_identifier: str | None = None) -> int:
    q = db.query(Account).filter(Account.api_id == api_id)
    if exclude_identifier:
        q = q.filter(Account.identifier != exclude_identifier)
    return q.count()


def validate_pool_choice(db: Session, api_id: int, exclude_identifier: str | None = None) -> str:
    """Проверяет, что пул существует и в нём есть место. Возвращает api_hash пула.
    Бросает OnboardingError с понятным текстом иначе."""
    api_hash = _pool_api_hash(db, api_id)
    if api_hash is None:
        raise OnboardingError(f"Пул api_id {api_id} не найден")
    limit = get_protection(db)["api_pool_max_accounts"]
    count = _pool_count(db, api_id, exclude_identifier)
    if count >= limit:
        raise OnboardingError(
            f"В пуле api_id {api_id} уже {count} аккаунтов — лимит {limit} (настраивается в «Настройки» → "
            f"«Защита аккаунтов»). Выберите другой пул или создайте новое приложение"
        )
    return api_hash


class CredentialUploadError(ValueError):
    """Ошибка разбора/загрузки пары api_id:api_hash, текст безопасен для показа пользователю."""


_CRED_LINE_RE = re.compile(r"[:\s]+")


def parse_credential_lines(text: str) -> list[tuple[int, str, str | None]]:
    """Одна строка — одна пара: `api_id:api_hash` или `api_id:api_hash:метка`
    (разделитель — «:» или пробел/таб). Пустые строки и строки с «#» пропускаются.
    Бросает CredentialUploadError на первой некорректной строке — так проще увидеть
    опечатку, чем потом гадать, какая из пар не сохранилась."""
    out: list[tuple[int, str, str | None]] = []
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = _CRED_LINE_RE.split(line, maxsplit=2)
        if len(parts) < 2:
            raise CredentialUploadError(f"Строка {i}: нужен формат api_id:api_hash[:метка]")
        api_id_s, api_hash, *rest = parts
        if not api_id_s.isdigit():
            raise CredentialUploadError(f"Строка {i}: api_id должен быть числом")
        api_hash = api_hash.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{32}", api_hash):
            raise CredentialUploadError(f"Строка {i}: api_hash должен быть 32 шестнадцатеричными символами")
        label = rest[0].strip() if rest and rest[0].strip() else None
        out.append((int(api_id_s), api_hash, label))
    if not out:
        raise CredentialUploadError("Не найдено ни одной строки вида api_id:api_hash")
    return out


def add_credentials(db: Session, text: str) -> dict:
    """Добавляет пачку приложений в общую библиотеку (ApiCredential). api_id, который
    уже где-то есть — в библиотеке или у любого аккаунта — пропускается, а не
    перезаписывается: так случайный повторный вставленный список не подменит чужой
    api_hash. Бросает CredentialUploadError, если текст не распарсился вообще —
    в этом случае ничего не сохраняется."""
    parsed = parse_credential_lines(text)
    existing = {row[0] for row in db.query(ApiCredential.api_id).all()}
    existing |= {a.api_id for a in db.query(Account.api_id).filter(Account.api_id.isnot(None)).all()}
    added, skipped, seen = [], [], set()
    for api_id, api_hash, label in parsed:
        if api_id in existing or api_id in seen:
            skipped.append(api_id)
            continue
        seen.add(api_id)
        db.add(ApiCredential(api_id=api_id, api_hash=api_hash, label=label))
        added.append(api_id)
    db.commit()
    return {"added": added, "skipped": skipped}


def assign_api_to_account(db: Session, account_id: int, api_id: int) -> None:
    """Назначает существующему аккаунту конкретный api_id из библиотеки/уже
    существующего пула — например, аккаунту, у которого сейчас нет своего api_id
    (работает на общем из .env). Прокси, сессия и профиль устройства не трогаются.
    Смена api_id у уже авторизованного аккаунта безопасна: auth_key сессии от api_id
    не зависит, воркер сам переподключится с новым значением на ближайшей сверке."""
    account = db.get(Account, account_id)
    if account is None:
        raise OnboardingError("Аккаунт не найден")
    api_hash = validate_pool_choice(db, api_id, exclude_identifier=account.identifier)
    account.api_id, account.api_hash = api_id, api_hash
    db.commit()


def auto_distribute(db: Session) -> dict:
    """Раскидывает аккаунты без своего api_id (работающие на общем из .env) по уже
    существующим пулам, у которых есть свободное место — сначала по частично занятым,
    потом по загруженным про запас (count=0), по кругу, пока не кончится место или
    аккаунты. Не создаёт новых приложений — только использует то, что уже загружено
    или создано через мастер ранее. Аккаунты, которым места не хватило, остаются как
    были: на общем api_id, автоответчик по-прежнему может их обслуживать."""
    limit = get_protection(db)["api_pool_max_accounts"]
    queue = [dict(p) for p in list_api_pools(db) if p["count"] < limit]
    free_accounts = db.query(Account).filter(Account.api_id.is_(None)).order_by(Account.identifier).all()

    assigned: list[tuple[str, int]] = []
    unassigned: list[str] = []
    qi = 0
    for account in free_accounts:
        candidates = [p for p in queue if p["count"] < limit]
        if not candidates:
            unassigned.append(account.identifier)
            continue
        pool = candidates[qi % len(candidates)]
        account.api_id, account.api_hash = pool["api_id"], pool["api_hash"]
        pool["count"] += 1
        assigned.append((account.identifier, pool["api_id"]))
        qi += 1
    db.commit()
    return {"assigned": assigned, "unassigned": unassigned}


def _cleanup_files(state: Onboarding) -> None:
    for suffix in ("", "-journal", ".lock"):
        try:
            Path(str(state.session_path) + suffix).unlink(missing_ok=True)
        except OSError:
            pass


async def _teardown(state: Onboarding, remove_session: bool) -> None:
    if state.creator is not None:
        await asyncio.to_thread(state.creator.close)
        state.creator = None
    if state.client is not None:
        try:
            await state.client.disconnect()
        except Exception:
            pass
        state.client = None
    if state.lock is not None:
        state.lock.release()
        state.lock = None
    if remove_session:
        _cleanup_files(state)


async def cancel(token: str) -> None:
    state = _STATES.pop(token, None)
    if state:
        await _teardown(state, remove_session=state.step != "done")


async def cleanup_expired() -> None:
    now = time.monotonic()
    for token, state in list(_STATES.items()):
        if now - state.created_at > settings.onboarding_ttl_seconds:
            _STATES.pop(token, None)
            await _teardown(state, remove_session=state.step != "done")


async def begin(db: Session, identifier: str, phone: str, raw_proxy: str, manager_name: str,
                lang_code: str = "", pool_api_id: int | None = None) -> Onboarding:
    """Проверяет всё до первого сетевого шага.

    pool_api_id=None (по умолчанию) — новое приложение: запускает браузер для
    my.telegram.org. pool_api_id=<число> — использовать уже существующий api_id/api_hash
    (аккаунт присоединяется к пулу вместо создания нового приложения): шаг my.telegram.org
    пропускается целиком, сразу идёт вход в Telegram. Прокси, сессия и профиль устройства
    в обоих случаях уникальны для этого аккаунта — общим становится только api_id/api_hash."""
    await cleanup_expired()
    identifier = identifier.strip()
    if not identifier:
        raise OnboardingError("Укажите идентификатор аккаунта")
    phone = normalize_phone(phone)
    proxy = validate_new_proxy(db, raw_proxy, exclude_identifier=identifier)

    pool_api_hash = None
    if pool_api_id is not None:
        pool_api_hash = validate_pool_choice(db, pool_api_id, exclude_identifier=identifier)

    ok, text = await asyncio.to_thread(test_proxy, proxy)
    if not ok:
        raise OnboardingError(f"Прокси не прошёл проверку: {text}")

    used = {(a.device_model, a.system_version, a.app_version) for a in db.query(Account).all() if a.device_model}
    lang = (lang_code.strip().lower(), lang_code.strip().lower()) if lang_code.strip() else None
    if lang and "-" not in lang[1]:
        lang = (lang[0], f"{lang[0]}-{lang[0].upper()}")
    profile = device_profile.generate_profile(used, lang)

    settings.sessions_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w.-]", "_", identifier)
    session_path = settings.sessions_dir / f"{safe}-{dt.datetime.now():%Y%m%d%H%M%S%f}.session"
    state = Onboarding(
        token=secrets.token_urlsafe(16), identifier=identifier, phone=phone, proxy=proxy,
        manager_name=manager_name.strip(), lang=(profile["lang_code"], profile["system_lang_code"]),
        profile=profile, session_path=session_path,
    )

    if pool_api_id is not None:
        # api_id/api_hash уже известны — можно сразу входить в Telegram, без my.telegram.org
        state.api_id, state.api_hash = pool_api_id, pool_api_hash
        _STATES[state.token] = state
        try:
            await _send_login_code(state)
        except OnboardingError:
            _STATES.pop(state.token, None)
            await _teardown(state, remove_session=True)
            raise
        return state

    _STATES[state.token] = state
    try:
        state.creator = ApiAppCreator(phone, parse_proxy(proxy), proxy, headless=settings.playwright_headless)
        await asyncio.to_thread(state.creator.call, "start", timeout=150)
    except CreatorError as exc:
        # браузерный путь не сработал — оставляем мастер открытым: api_id/api_hash можно ввести вручную
        state.step = "manual_api"
        state.message = str(exc)
        return state
    state.step = "web_code"
    state.message = ("my.telegram.org отправил код в Telegram менеджера (сообщение от «Telegram»). "
                     "Введите этот код ниже.")
    return state


async def submit_web_code(state: Onboarding, code: str) -> None:
    async with state.busy:
        if state.step != "web_code" or state.creator is None:
            raise OnboardingError("Сейчас код my.telegram.org не ожидается")
        try:
            creds = await asyncio.to_thread(state.creator.call, "submit_code", code, timeout=150)
        except CreatorError as exc:
            state.message = str(exc)
            state.step = "manual_api"
            return
        await _finish_api_step(state, creds["api_id"], creds["api_hash"])


async def submit_manual_api(state: Onboarding, api_id: str, api_hash: str) -> None:
    async with state.busy:
        if state.step not in ("manual_api", "web_code"):
            raise OnboardingError("Сейчас ввод api_id/api_hash не ожидается")
        api_hash = api_hash.strip().lower()
        if not api_id.strip().isdigit() or not re.fullmatch(r"[0-9a-f]{32}", api_hash):
            raise OnboardingError("api_id — число, api_hash — 32 шестнадцатеричных символа")
        await _finish_api_step(state, int(api_id), api_hash)


async def _finish_api_step(state: Onboarding, api_id: int, api_hash: str) -> None:
    state.api_id, state.api_hash = api_id, api_hash
    if state.creator is not None:  # браузер больше не нужен
        await asyncio.to_thread(state.creator.close)
        state.creator = None
    await _send_login_code(state)


def _client_kwargs(state: Onboarding) -> dict:
    return {
        "device_model": state.profile["device_model"],
        "system_version": state.profile["system_version"],
        "app_version": state.profile["app_version"],
        "lang_code": state.profile["lang_code"],
        "system_lang_code": state.profile["system_lang_code"],
        "flood_sleep_threshold": 0,  # FloodWait не «пережидаем» молча — показываем и останавливаемся
    }


async def _send_login_code(state: Onboarding) -> None:
    proxy_tuple = parse_proxy(state.proxy)
    use_ipv6, addr = await asyncio.to_thread(choose_ip_family, proxy_tuple, 2)
    if addr is None:
        raise OnboardingError("Прокси не пропускает Telegram ни по IPv4, ни по IPv6")

    state.lock = SessionLock(str(state.session_path))
    try:
        state.lock.acquire()
    except SessionInUseError as exc:
        raise OnboardingError(str(exc)) from exc

    state.client = TelegramClient(
        str(state.session_path), state.api_id, state.api_hash, proxy=proxy_tuple,
        use_ipv6=use_ipv6, **_client_kwargs(state),
    )
    if use_ipv6:
        state.client.session.set_dc(state.client.session.dc_id, addr, 443)
    try:
        await state.client.connect()
        sent = await state.client.send_code_request(state.phone)
    except FloodWaitError as exc:
        await _fail(state, f"Telegram просит подождать {exc.seconds} с перед новым кодом. Повторите позже.")
        return
    except PhoneNumberInvalidError:
        await _fail(state, "Telegram не принял номер телефона")
        return
    except PhoneNumberBannedError:
        await _fail(state, "Этот номер заблокирован в Telegram")
        return
    state.phone_code_hash = sent.phone_code_hash
    state.step = "login_code"
    state.message = "Введите код входа, который Telegram прислал менеджеру (это второй код, отличается от первого)."


async def _fail(state: Onboarding, message: str) -> None:
    state.step = "failed"
    state.message = message
    await _teardown(state, remove_session=True)


async def submit_login_code(state: Onboarding, code: str) -> None:
    async with state.busy:
        if state.step != "login_code" or state.client is None:
            raise OnboardingError("Сейчас код входа не ожидается")
        try:
            await state.client.sign_in(state.phone, code.strip(), phone_code_hash=state.phone_code_hash)
        except SessionPasswordNeededError:
            state.step = "password"
            state.message = "На аккаунте включён облачный пароль (2FA). Введите его."
            return
        except PhoneCodeInvalidError:
            raise OnboardingError("Код неверный — проверьте и введите ещё раз")
        except PhoneCodeExpiredError:
            await _fail(state, "Код устарел — начните добавление заново")
            return
        except FloodWaitError as exc:
            await _fail(state, f"Telegram просит подождать {exc.seconds} с. Повторите позже.")
            return
        await _complete(state)


async def submit_password(state: Onboarding, password: str) -> None:
    async with state.busy:
        if state.step != "password" or state.client is None:
            raise OnboardingError("Сейчас пароль 2FA не ожидается")
        try:
            await state.client.sign_in(password=password)
        except PasswordHashInvalidError:
            raise OnboardingError("Неверный пароль 2FA")
        except FloodWaitError as exc:
            await _fail(state, f"Telegram просит подождать {exc.seconds} с. Повторите позже.")
            return
        await _complete(state)


async def _complete(state: Onboarding) -> None:
    me = await state.client.get_me()
    real_phone = f"+{me.phone}" if me and me.phone else state.phone
    await state.client.disconnect()
    state.client = None
    if state.lock is not None:
        state.lock.release()
        state.lock = None

    from ..database import SessionLocal  # локально: избегаем циклов при импорте

    with SessionLocal() as db:
        account = db.query(Account).filter_by(identifier=state.identifier).one_or_none()
        old_session = account.session_path if account else None
        if account is None:
            account = Account(identifier=state.identifier)
            db.add(account)
        account.phone = real_phone
        account.manager_name = state.manager_name or account.manager_name
        account.session_path = str(state.session_path)
        account.proxy = state.proxy
        account.api_id, account.api_hash = state.api_id, state.api_hash
        account.device_model = state.profile["device_model"]
        account.system_version = state.profile["system_version"]
        account.app_version = state.profile["app_version"]
        account.lang_code = state.profile["lang_code"]
        account.system_lang_code = state.profile["system_lang_code"]
        account.is_authorized = True
        account.last_error = None
        account.flood_streak, account.flood_last_at, account.paused_until = 0, None, None
        account.enabled = False  # включить вручную после «остывания» новой сессии
        db.commit()
    if old_session and old_session != str(state.session_path):
        for suffix in ("", "-journal", ".lock"):
            try:
                Path(old_session + suffix).unlink(missing_ok=True)
            except OSError:
                logger.warning("Не удалось удалить прежний файл сессии %s%s", old_session, suffix)
    state.step = "done"
    state.message = (f"Аккаунт {state.identifier} добавлен. Автоответчик выключен — включите его на странице "
                     f"«Аккаунты» примерно через 30–60 минут: свежая сессия не должна сразу начинать отвечать.")
