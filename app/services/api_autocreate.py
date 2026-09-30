"""Автоматическое создание собственного приложения (api_id / api_hash) для аккаунта, у
которого УЖЕ есть рабочая сессия (импорт TData или ранее добавленный аккаунт).

То же, что делает мастер «Добавить аккаунт» (api_app_creator: my.telegram.org в headless-
браузере через прокси аккаунта), только код входа на сайт не вводит человек: my.telegram.org
присылает его в чат «Telegram» (777000), и мы читаем его сами через сессию аккаунта.

Сессия при этом подключается тем же приложением, устройством и прокси, с которыми она
создана, — как обычный запуск воркера. Если сайт отказал (CAPTCHA, «too many tries»), аккаунт
остаётся как был: работает на прежнем api_id, ничего не ломается."""
import asyncio
import datetime as dt
import logging
import re
import time

from telethon import TelegramClient

from . import device_profile
from .api_app_creator import ApiAppCreator, CreatorError
from .proxy import ProxyConfigError, parse_proxy

logger = logging.getLogger(__name__)

SERVICE_CHAT_ID = 777000
CODE_WAIT_SECONDS = 120
# «This is your login code:\nAbCdEf12GhI» / «Ваш код для входа:\n…» — код веб-входа буквенно-
# цифровой и длиннее обычного 5-значного кода входа в Telegram, с которым его не спутать.
_CODE_RE = re.compile(r"(?:code|код)[^\n:]{0,40}:\s*([A-Za-z0-9_\-]{6,32})\b", re.I)


class AutoCreateError(RuntimeError):
    """Текст безопасно показать пользователю."""


def parse_web_code(text: str) -> str | None:
    if not text:
        return None
    m = _CODE_RE.search(text)
    return m.group(1) if m else None


def _client(account_like: dict, session_path: str, proxy_tuple) -> TelegramClient:
    client = TelegramClient(
        session_path, int(account_like["api_id"]), account_like["api_hash"], proxy=proxy_tuple,
        flood_sleep_threshold=0,
        **{k: account_like[k] for k in device_profile.PROFILE_FIELDS if account_like.get(k)},
    )
    if device_profile.is_official_desktop(account_like["api_id"]):
        client._init_request.lang_pack = device_profile.OFFICIAL_DESKTOP_LANG_PACK
    return client


async def _wait_for_code(client: TelegramClient, since: dt.datetime) -> str:
    deadline = time.monotonic() + CODE_WAIT_SECONDS
    while time.monotonic() < deadline:
        await asyncio.sleep(3)
        for msg in await client.get_messages(SERVICE_CHAT_ID, limit=5):
            if msg.date and msg.date >= since:
                code = parse_web_code(msg.message or "")
                if code:
                    return code
    raise AutoCreateError("код от my.telegram.org не пришёл в Telegram за 2 минуты")


async def create_own_app(session_path: str, proxy_str: str, current: dict) -> dict:
    """current — с чем сессия работает сейчас: api_id, api_hash и поля профиля устройства.
    Возвращает {"api_id", "api_hash", "phone"}. Бросает AutoCreateError."""
    if not proxy_str:
        raise AutoCreateError("у аккаунта не задан прокси — my.telegram.org открывается только через прокси аккаунта")
    try:
        proxy_tuple = parse_proxy(proxy_str)
    except ProxyConfigError as exc:
        raise AutoCreateError(f"некорректный прокси: {exc}") from exc
    if not current.get("api_id") or not current.get("api_hash"):
        raise AutoCreateError("неизвестно, каким приложением создана сессия")

    client = _client(current, session_path, proxy_tuple)
    creator: ApiAppCreator | None = None
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise AutoCreateError("сессия не авторизована — сначала добавьте аккаунт заново")
        me = await client.get_me()
        if not me or not me.phone:
            raise AutoCreateError("Telegram не сообщил номер телефона аккаунта")
        phone = f"+{me.phone}"
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=5)
        creator = ApiAppCreator(phone, proxy_tuple, proxy_str)
        await asyncio.to_thread(creator.call, "start", timeout=150)
        code = await _wait_for_code(client, since)
        creds = await asyncio.to_thread(creator.call, "submit_code", code, timeout=150)
        return {"api_id": int(creds["api_id"]), "api_hash": creds["api_hash"], "phone": phone}
    except CreatorError as exc:
        raise AutoCreateError(str(exc)) from exc
    except AutoCreateError:
        raise
    except Exception as exc:  # noqa: BLE001 — любая сетевая/Telethon-ошибка → понятный текст
        logger.exception("API app auto-creation failed")
        raise AutoCreateError(f"{type(exc).__name__}: {exc}") from exc
    finally:
        if creator is not None:
            await asyncio.to_thread(creator.close)
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass


def current_api(account) -> dict:
    """С каким приложением/устройством сессия аккаунта работает сейчас (как её запустит воркер)."""
    from ..config import settings

    data = {"api_id": account.api_id or settings.telegram_api_id,
            "api_hash": account.api_hash or settings.telegram_api_hash}
    data.update({k: getattr(account, k) for k in device_profile.PROFILE_FIELDS})
    return data


def apply_own_app(db, account, creds: dict) -> None:
    """Сохраняет созданное приложение. Профиль устройства — свой, «неофициальный»: собственное
    приложение не должно выдавать себя за Telegram Desktop (его сигнатура привязана к api_id 2040)."""
    was_official = device_profile.is_official_desktop(account.api_id)
    account.api_id, account.api_hash = creds["api_id"], creds["api_hash"]
    if creds.get("phone"):
        account.phone = creds["phone"]
    if not account.device_model or was_official:
        device_profile.apply_profile(account, device_profile.generate_profile(device_profile.used_profiles(db, account.id)))
    db.commit()
