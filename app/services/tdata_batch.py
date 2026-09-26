"""Пакетный импорт аккаунтов из архивов с TData (.zip / .rar).

Работа в два этапа:
  1. prepare_batch — распаковывает архивы, находит папки TData, подбирает идентификаторы
     и прокси и ПРОВЕРЯЕТ ВСЁ до первого обращения к Telegram: если у какого-то аккаунта нет
     прокси или два делят один — не импортируется ничего (иначе часть аккаунтов успела бы
     зайти, а часть нет, и легко получить запрос без прокси);
  2. run_job — фоновая задача: аккаунты входят по одному с паузами (не «залпом»), каждый
     через свой прокси; ошибка одного не отменяет остальные; прогресс виден в панели.

Состояние задач хранится в памяти процесса веб-панели (она работает в одном процессе).
Пароли (локальный код Desktop, облачный 2FA) в задаче не сохраняются — живут только в
аргументах фоновой корутины и исчезают вместе с ней."""
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
from .archive_utils import ArchiveError, extract_tdata_archive
from .onboarding import proxy_in_use
from .proxy import ProxyConfigError, mask_proxy, normalize_proxy, parse_proxy, proxy_identity
from .session_files import discard_session_file, finalize_tdata_account, fresh_session_path
from .session_utils import find_tdata_dirs, tdata_to_session

logger = logging.getLogger(__name__)

_IDENT_RE = re.compile(r"^[\w.+\-]{1,64}$")
_SKIP_NAMES = {"tdata", "telegram", "telegram desktop"}
JOB_KEEP_SECONDS = 3600


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


@dataclass
class BatchJob:
    token: str
    items: list[BatchItem]
    workdir: Path
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
) -> BatchJob:
    """Распаковывает архивы (name, path), находит TData, подбирает идентификаторы и прокси и
    проверяет всё. Бросает TDataBatchError. Ничего не пишет в БД и не ходит в Telegram."""
    if not archives:
        raise TDataBatchError("Не выбрано ни одного архива")

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
            raise TDataBatchError(f"Аккаунты {seen[key]} и {ident} используют один прокси — нужен отдельный на каждый")
        seen[key] = ident
        owner = proxy_in_use(db, proxy, exclude_identifier=ident)
        if owner:
            raise TDataBatchError(f"Прокси аккаунта {ident} уже используется аккаунтом {owner} — нужен отдельный на каждый")

    items = []
    for (name, root, d, _stem), ident, proxy in zip(found, idents, proxies):
        source = f"{name} / {d.relative_to(root).as_posix() or '.'}"
        item = BatchItem(identifier=ident, tdata_dir=d, proxy=proxy, source=source)
        if ident in skipped:
            item.status, item.message = "skipped", "уже есть сессия — пропущен (включите «Заменить существующие», чтобы перезаписать)"
        items.append(item)
    return BatchJob(token=secrets.token_urlsafe(12), items=items, workdir=workdir)


async def run_job(job: BatchJob, *, passcode: str | None = None, cloud_password: str | None = None) -> None:
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
                await run_in_threadpool(tdata_to_session, item.tdata_dir, dest, proxy_tuple,
                                        passcode=passcode, cloud_password=cloud_password)
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
                    finalize_tdata_account(db, item.identifier, dest, item.proxy)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Could not save account %s after import", item.identifier)
                item.status, item.message = "error", f"сеанс создан, но сохранить аккаунт не удалось: {exc}"
                continue
            item.status = "ok"
            item.message = "готово; автоответчик выключен — включите на странице «Аккаунты» через 30–60 минут"
    finally:
        shutil.rmtree(job.workdir, ignore_errors=True)  # распакованные ключи не должны лежать на диске дольше нужного
        job.finished_at = time.time()


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


def start_job(job: BatchJob, *, passcode: str | None = None, cloud_password: str | None = None) -> None:
    """Регистрирует и запускает фоновую задачу. Вызывать из event loop панели."""
    ensure_no_active_job()
    _JOBS[job.token] = job
    job.task = asyncio.create_task(run_job(job, passcode=passcode, cloud_password=cloud_password))


def job_view(job: BatchJob) -> dict:
    """JSON для страницы прогресса. Прокси — без логина/пароля."""
    return {
        "done": not job.running,
        "counts": {s: sum(1 for i in job.items if i.status == s) for s in ("queued", "running", "ok", "error", "skipped")},
        "items": [
            {"identifier": i.identifier, "source": i.source, "proxy": mask_proxy(i.proxy) if i.proxy else "—",
             "status": i.status, "message": i.message}
            for i in job.items
        ],
    }
