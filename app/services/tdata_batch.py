"""Пакетный импорт аккаунтов из архивов с TData (.zip / .rar).

Работа в два этапа:
  1. prepare_batch — распаковывает архивы, находит папки TData, подбирает идентификаторы
     и прокси и ПРОВЕРЯЕТ ВСЁ до первого обращения к Telegram: если у какого-то аккаунта нет
     прокси или два делят один — не импортируется ничего (иначе часть аккаунтов успела бы
     зайти, а часть нет, и легко получить запрос без прокси);
  2. run_job — фоновая задача: аккаунты входят по одному с паузами (не «залпом»), каждый
     через свой прокси; ошибка одного не отменяет остальные; прогресс виден в панели.

Приложение (api_id) для каждого аккаунта — один из режимов API_MODES:
  auto    — сеанс создаётся под официальным Telegram Desktop, затем через него же
            автоматически создаётся собственное приложение на my.telegram.org (код входа на
            сайт читается из чата «Telegram» самим сеансом) и аккаунт переводится на него;
  pool    — свободные места в уже загруженных пулах api_id: сеанс сразу создаётся под ними;
  desktop — остаётся официальный Telegram Desktop (api_id 2040).
В любом режиме приложение и профиль устройства, с которыми создан сеанс, сохраняются у
аккаунта — воркер подключается тем же клиентом («нет профиля устройства» больше не бывает).

Состояние задач хранится в памяти процесса веб-панели (она работает в одном процессе).
Пароли (локальный код Desktop, облачный 2FA — общий или свой у каждого аккаунта) в задаче
не сохраняются — живут только в аргументах фоновой корутины и исчезают вместе с ней."""
import asyncio
import logging
import random
import re
import secrets
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..config import settings
from ..database import SessionLocal
from ..models import Account
from . import device_profile
from .api_autocreate import AutoCreateError, apply_own_app, create_own_app
from .archive_utils import ArchiveError, extract_tdata_archive
from .onboarding import list_api_pools, proxy_conflict_text, proxy_in_use
from .settings_store import get_protection
from .proxy import ProxyConfigError, mask_proxy, normalize_proxy, parse_proxy, proxy_identity
from .session_files import discard_session_file, finalize_tdata_account, fresh_session_path
from .session_utils import find_tdata_dirs, tdata_to_session

logger = logging.getLogger(__name__)

_IDENT_RE = re.compile(r"^[\w.+\-]{1,64}$")
_SKIP_NAMES = {"tdata", "telegram", "telegram desktop"}
JOB_KEEP_SECONDS = 3600
API_MODES = {
    "auto": "создать своё приложение автоматически",
    "pool": "из загруженных пулов api_id",
    "desktop": "официальный Telegram Desktop (без своего приложения)",
}


class TDataBatchError(ValueError):
    """Ошибка подготовки/запуска импорта; текст безопасно показать пользователю."""


@dataclass
class BatchItem:
    identifier: str
    tdata_dir: Path
    proxy: str
    source: str
    status: str = "queued"   # queued | running | ok | error | skipped
    message: str = ""
    # приложение + профиль, под которыми создавать сеанс (режим pool); None — Telegram Desktop
    api: dict | None = None


@dataclass
class BatchJob:
    token: str
    items: list[BatchItem]
    workdir: Path
    api_mode: str = "desktop"
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self.finished_at is None


_JOBS: dict[str, BatchJob] = {}


def _nat_key(path: Path):
    """Естественный порядок: 2 раньше 10."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(path))]


def _derive_identifier(root: Path, tdir: Path, archive_stem: str) -> str:
    """Идентификатор из раскладки архива: ближайшая к tdata папка («1» из «1/tdata»), а если
    tdata лежит прямо в корне архива — имя самого архива (удобно, когда файл назван номером)."""
    rel = [p for p in tdir.relative_to(root).parts if p.lower() not in _SKIP_NAMES]
    name = rel[-1] if rel else archive_stem
    name = re.sub(r"[^\w.+\-]", "_", name).strip("._") or re.sub(r"[^\w.+\-]", "_", archive_stem)
    return name[:64] or "account"


def prepare_batch(
    db: Session,
    archives: list[tuple[str, Path]],
    workdir: Path,
    *,
    proxies_text: str = "",
    identifiers_text: str = "",
    replace_existing: bool = False,
    archive_password: str | None = None,
    api_mode: str = "desktop",
) -> BatchJob:
    """Распаковывает архивы (name, path), находит TData, подбирает идентификаторы и прокси и
    проверяет всё. Бросает TDataBatchError. Ничего не пишет в БД и не ходит в Telegram."""
    if not archives:
        raise TDataBatchError("Не выбрано ни одного архива")
    if api_mode not in API_MODES:
        raise TDataBatchError("Неизвестный режим выбора api_id")
    if api_mode == "auto":
        from .browser_deps import playwright_available_cached

        ok, hint = playwright_available_cached()
        if not ok:
            raise TDataBatchError(f"Автосоздание api_id недоступно: {hint} — или выберите другой режим api_id")

    found: list[tuple[str, Path, Path, str]] = []  # (имя архива, корень, папка TData, имя без расширения)
    for i, (name, path) in enumerate(archives):
        root = workdir / f"a{i}"
        try:
            extract_tdata_archive(path, root, archive_password)
        except ArchiveError as exc:
            raise TDataBatchError(f"{name}: {exc}") from exc
        dirs = sorted(find_tdata_dirs(root), key=_nat_key)
        if not dirs:
            raise TDataBatchError(f"{name}: не найдено ни одной папки TData (в ней должен быть файл key_datas)")
        stem = Path(name).stem
        found.extend((name, root, d, stem) for d in dirs)

    # --- идентификаторы
    given = [ln.strip() for ln in identifiers_text.splitlines() if ln.strip()]
    if given:
        if len(given) != len(found):
            raise TDataBatchError(f"Идентификаторов указано {len(given)}, а аккаунтов в архивах {len(found)} — "
                                  f"нужен ровно один на аккаунт (или оставьте поле пустым — возьмутся из имён папок)")
        idents = given
    else:
        idents = [_derive_identifier(root, d, stem) for _, root, d, stem in found]
    bad = [i for i in idents if not _IDENT_RE.match(i)]
    if bad:
        raise TDataBatchError(f"Недопустимые идентификаторы (буквы/цифры и . _ + -, до 64 символов): {', '.join(bad[:5])}")
    dupes = sorted({i for i in idents if idents.count(i) > 1})
    if dupes:
        raise TDataBatchError(f"Повторяются идентификаторы: {', '.join(dupes)}. Задайте их вручную в поле «Идентификаторы» "
                              f"или переименуйте папки в архиве")

    existing = {a.identifier: a for a in db.query(Account).filter(Account.identifier.in_(idents)).all()}
    skipped = {i for i in idents if i in existing and existing[i].session_path and not replace_existing}

    # --- прокси: по строке на аккаунт (в порядке аккаунтов) либо уже сохранённые у аккаунтов
    lines = [ln.strip() for ln in proxies_text.splitlines() if ln.strip()]
    if lines and len(lines) != len(found):
        raise TDataBatchError(f"Прокси указано {len(lines)}, а аккаунтов {len(found)} — нужен ровно один прокси на аккаунт, "
                              f"по строке, в порядке: {', '.join(idents)}")
    proxies: list[str] = []
    missing: list[str] = []
    for n, ident in enumerate(idents):
        if ident in skipped:
            proxies.append("")
            continue
        if lines:
            try:
                normalized = normalize_proxy(lines[n])
                if not normalized:
                    raise ProxyConfigError("пустой прокси")
                parse_proxy(normalized)
            except ProxyConfigError as exc:
                raise TDataBatchError(f"Прокси для {ident} (строка {n + 1}): {exc}") from exc
        else:
            normalized = existing[ident].proxy if ident in existing and existing[ident].proxy else ""
            if not normalized:
                missing.append(ident)
        proxies.append(normalized)
    if missing:
        raise TDataBatchError("Не задан прокси для: " + ", ".join(missing) +
                              ". Впишите прокси в поле (по строке на аккаунт, в порядке: " + ", ".join(idents) +
                              ") — импорт идёт через прокси аккаунта, иначе в Telegram ушёл бы реальный IP этой машины")

    seen: dict[tuple, str] = {}
    for ident, proxy in zip(idents, proxies):
        if not proxy:
            continue
        key = proxy_identity(proxy)
        if key in seen:
            raise TDataBatchError(f"Аккаунты {seen[key]} и {ident} используют один и тот же прокси (совпадают хост, порт, логин и пароль) — нужен отдельный на каждый")
        seen[key] = ident
        owner = proxy_in_use(db, proxy, exclude_identifier=ident)
        if owner:
            raise TDataBatchError(f"Аккаунт {ident}: {proxy_conflict_text(proxy, owner)}")

    items = []
    for (name, root, d, _stem), ident, proxy in zip(found, idents, proxies):
        source = f"{name} / {d.relative_to(root).as_posix() or '.'}"
        item = BatchItem(identifier=ident, tdata_dir=d, proxy=proxy, source=source)
        if ident in skipped:
            item.status, item.message = "skipped", "уже есть сессия — пропущен (включите «Заменить существующие», чтобы перезаписать)"
        items.append(item)
    if api_mode == "pool":
        _assign_pools(db, [i for i in items if i.status == "queued"])
    return BatchJob(token=secrets.token_urlsafe(12), items=items, workdir=workdir, api_mode=api_mode)


def _assign_pools(db: Session, items: list[BatchItem]) -> None:
    """Раздаёт аккаунтам места в пулах api_id (как «Раскидать по пулам»), каждому — свой
    профиль устройства. Места не хватает — ошибка до первого обращения к Telegram."""
    limit = get_protection(db)["api_pool_max_accounts"]
    replacing = {i.identifier for i in items}
    pools = []
    for p in list_api_pools(db):
        if device_profile.is_official_desktop(p["api_id"]):
            continue
        # аккаунты, которые сейчас перезаписываются, место в своём пуле освобождают
        count = sum(1 for m in p["members"] if m not in replacing)
        if count < limit:
            pools.append({"api_id": p["api_id"], "api_hash": p["api_hash"], "free": limit - count})
    capacity = sum(p["free"] for p in pools)
    if capacity < len(items):
        raise TDataBatchError(f"В пулах api_id свободно мест: {capacity}, а аккаунтов для импорта {len(items)}. "
                              f"Загрузите ещё приложения на странице «Аккаунты» или выберите режим «создать автоматически»")
    used = device_profile.used_profiles(db)
    qi = 0
    for item in items:
        candidates = [p for p in pools if p["free"] > 0]
        pool = candidates[qi % len(candidates)]
        pool["free"] -= 1
        qi += 1
        profile = device_profile.generate_profile(used)
        used.add((profile["device_model"], profile["system_version"], profile["app_version"]))
        item.api = {"api_id": pool["api_id"], "api_hash": pool["api_hash"], **profile}


_PW_LINE_RE = re.compile(r"^(\S+?)\s*[:;=\t ]\s*(.+)$")


def parse_cloud_passwords(text: str, identifiers: list[str]) -> dict[str, str]:
    """Облачные пароли (2FA) по аккаунтам. Два формата:
      * «идентификатор:пароль» (или через ; = пробел таб) — по строке, любые аккаунты, в любом порядке;
      * просто пароль по строке на КАЖДЫЙ аккаунт, в том же порядке (прочерк «-» — без пароля).
    Бросает TDataBatchError. Пустой текст — пустой словарь."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return {}
    known = set(identifiers)
    mapped: dict[str, str] = {}
    for ln in lines:
        m = _PW_LINE_RE.match(ln)
        if not (m and m.group(1) in known):
            mapped = {}
            break
        mapped[m.group(1)] = m.group(2)
    if mapped:
        return mapped
    if len(lines) != len(identifiers):
        raise TDataBatchError(
            f"Паролей 2FA указано {len(lines)}, а аккаунтов {len(identifiers)}. Либо по строке на каждый аккаунт "
            f"в порядке: {', '.join(identifiers)} (прочерк «-» — без пароля), либо строки вида «идентификатор:пароль»")
    return {ident: pw for ident, pw in zip(identifiers, lines) if pw != "-"}


async def run_job(job: BatchJob, *, passcode: str | None = None, cloud_password: str | None = None,
                  cloud_passwords: dict[str, str] | None = None) -> None:
    try:
        first = True
        for item in job.items:
            if item.status != "queued":
                continue
            if not first:  # не «залпом»: заходы с паузой, как и запуск воркера
                await asyncio.sleep(random.uniform(settings.start_stagger_min_seconds, settings.start_stagger_max_seconds))
            first = False
            item.status = "running"
            dest = fresh_session_path(item.identifier)
            try:
                proxy_tuple = parse_proxy(item.proxy)
                password = (cloud_passwords or {}).get(item.identifier) or cloud_password
                used = await run_in_threadpool(tdata_to_session, item.tdata_dir, dest, proxy_tuple,
                                               passcode=passcode, cloud_password=password, api=item.api)
            except RuntimeError as exc:
                logger.warning("TData import failed for %s: %s", item.identifier, exc)
                item.status, item.message = "error", str(exc)
                discard_session_file(str(dest))
                continue
            except Exception as exc:  # noqa: BLE001 — одна поломка не должна останавливать остальные
                logger.exception("TData import crashed for %s", item.identifier)
                item.status, item.message = "error", f"{type(exc).__name__}: {exc}"
                discard_session_file(str(dest))
                continue
            try:
                with SessionLocal() as db:
                    finalize_tdata_account(db, item.identifier, dest, item.proxy, api=used)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Could not save account %s after import", item.identifier)
                item.status, item.message = "error", f"сеанс создан, но сохранить аккаунт не удалось: {exc}"
                continue
            note = ""
            if job.api_mode == "auto":
                item.message = "сеанс создан, создаём своё приложение (api_id) на my.telegram.org…"
                note = await _auto_create(item, dest, used)
            item.status = "ok"
            item.message = "готово" + note + "; автоответчик выключен — включите на странице «Аккаунты» через 30–60 минут"
    finally:
        shutil.rmtree(job.workdir, ignore_errors=True)  # распакованные ключи не должны лежать на диске дольше нужного
        job.finished_at = time.time()


async def _auto_create(item: BatchItem, dest: Path, used: dict) -> str:
    """Своё приложение для только что импортированного аккаунта. Неудача не делает импорт
    ошибочным: аккаунт остаётся рабочим на официальном Telegram Desktop."""
    try:
        creds = await create_own_app(str(dest), item.proxy, used)
    except AutoCreateError as exc:
        logger.warning("API app auto-creation failed for %s: %s", item.identifier, exc)
        return (f", но своё приложение не создано ({exc}) — аккаунт работает на API Telegram Desktop; "
                f"создать позже можно кнопкой на странице «Аккаунты»")
    with SessionLocal() as db:
        account = db.query(Account).filter_by(identifier=item.identifier).one()
        apply_own_app(db, account, creds)
    return f", своё приложение api_id {creds['api_id']}"


def active_job() -> BatchJob | None:
    return next((j for j in _JOBS.values() if j.running), None)


def cleanup_jobs() -> None:
    now = time.time()
    for token, job in list(_JOBS.items()):
        if job.finished_at and now - job.finished_at > JOB_KEEP_SECONDS:
            _JOBS.pop(token, None)


def get_job(token: str) -> BatchJob | None:
    cleanup_jobs()
    return _JOBS.get(token)


def ensure_no_active_job() -> None:
    if active_job():
        raise TDataBatchError("Сейчас уже идёт другой импорт — дождитесь его окончания (аккаунты входят по одному с паузами)")


def start_job(job: BatchJob, *, passcode: str | None = None, cloud_password: str | None = None,
              cloud_passwords: dict[str, str] | None = None) -> None:
    """Регистрирует и запускает фоновую задачу. Вызывать из event loop панели."""
    ensure_no_active_job()
    _JOBS[job.token] = job
    job.task = asyncio.create_task(run_job(job, passcode=passcode, cloud_password=cloud_password,
                                           cloud_passwords=cloud_passwords))


def job_view(job: BatchJob) -> dict:
    """JSON для страницы прогресса. Прокси — без логина/пароля."""
    return {
        "done": not job.running,
        "api_mode": API_MODES.get(job.api_mode, job.api_mode),
        "counts": {s: sum(1 for i in job.items if i.status == s) for s in ("queued", "running", "ok", "error", "skipped")},
        "items": [
            {"identifier": i.identifier, "source": i.source, "proxy": mask_proxy(i.proxy) if i.proxy else "—",
             "status": i.status, "message": i.message}
            for i in job.items
        ],
    }
