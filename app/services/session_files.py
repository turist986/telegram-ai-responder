"""Файлы сессий аккаунтов и привязка к строке Account — общие для загрузки через панель
(routers/accounts.py) и пакетного импорта TData (services/tdata_batch.py)."""
import datetime as dt
import logging
import re
from pathlib import Path

from sqlalchemy.orm import Session

from ..config import settings
from ..models import Account, AccountBlacklist, AccountNiche, ChatStatus, DialogMessage
from .device_profile import apply_profile

logger = logging.getLogger(__name__)


def fresh_session_path(identifier: str) -> Path:
    """Каждая загрузка пишет в НОВЫЙ файл: перезапись файла, который прямо сейчас держит
    подключённый воркер, портит сессию, а воркер не заметит смены (путь тот же)."""
    settings.sessions_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w.-]", "_", identifier)
    return settings.sessions_dir / f"{safe}-{dt.datetime.now():%Y%m%d%H%M%S%f}.session"


def discard_session_file(path: str) -> None:
    for suffix in ("", "-journal", ".lock"):
        try:
            Path(path + suffix).unlink(missing_ok=True)
        except OSError:
            logger.warning("Не удалось удалить старый файл сессии %s%s (занят) — удалите вручную", path, suffix)


def bind_session(account: Account, dest: Path) -> None:
    old = account.session_path
    account.session_path = str(dest)
    account.is_authorized = False
    account.last_error = None
    if old and old != str(dest):
        discard_session_file(old)


def finalize_tdata_account(db: Session, identifier: str, dest: Path, proxy_str: str,
                           api: dict | None = None) -> Account:
    """Создаёт/обновляет аккаунт после успешной конвертации. Аккаунт создаётся ВЫКЛЮЧЕННЫМ:
    только что созданный сеанс не должен сразу начинать отвечать клиентам — включите
    автоответчик вручную через 30–60 минут (как и после добавления через мастер).

    api — что вернул tdata_to_session: приложение и устройство, с которыми создан сеанс.
    Сохраняется у аккаунта, чтобы воркер подключался ТЕМ ЖЕ клиентом, что и при входе."""
    account = db.query(Account).filter_by(identifier=identifier).one_or_none()
    if account is None:
        account = Account(identifier=identifier)
        db.add(account)
    bind_session(account, dest)
    account.proxy = proxy_str
    if api and api.get("api_id"):
        account.api_id, account.api_hash = int(api["api_id"]), api["api_hash"]
        apply_profile(account, api)
        if api.get("phone"):
            account.phone = api["phone"]
    account.flood_streak, account.flood_last_at, account.paused_until = 0, None, None
    account.is_authorized = True  # вход только что выполнен (CreateNewSession)
    account.enabled = False
    account.last_error = None
    db.commit()
    return account


def delete_accounts(db: Session, account_ids: list[int]) -> list[str]:
    """Удаляет аккаунты вместе со всем, что без них бессмысленно (история диалогов, статусы
    чатов, ниши, чёрный список), и их файлами сессий. Возвращает идентификаторы удалённых.
    Если воркер ещё держит файл (Windows), файл останется — воркер сам отключит аккаунт на
    ближайшей сверке (его больше нет в БД), а файл удалится при следующем удалении/вручную."""
    ids = [int(i) for i in account_ids]
    if not ids:
        return []
    accounts = db.query(Account).filter(Account.id.in_(ids)).all()
    found = [a.id for a in accounts]
    for model in (DialogMessage, ChatStatus, AccountNiche, AccountBlacklist):
        db.query(model).filter(model.account_id.in_(found)).delete(synchronize_session=False)
    removed, paths = [], []
    for account in accounts:
        removed.append(account.identifier)
        if account.session_path:
            paths.append(account.session_path)
        db.delete(account)
    db.commit()
    for path in paths:
        discard_session_file(path)
    return removed
