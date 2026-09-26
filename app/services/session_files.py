"""Файлы сессий аккаунтов и привязка к строке Account — общие для загрузки через панель
(routers/accounts.py) и пакетного импорта TData (services/tdata_batch.py)."""
import datetime as dt
import logging
import re
from pathlib import Path

from sqlalchemy.orm import Session

from ..config import settings
from ..models import Account

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


def finalize_tdata_account(db: Session, identifier: str, dest: Path, proxy_str: str) -> Account:
    """Создаёт/обновляет аккаунт после успешной конвертации. Аккаунт создаётся ВЫКЛЮЧЕННЫМ:
    только что созданный сеанс не должен сразу начинать отвечать клиентам — включите
    автоответчик вручную через 30–60 минут (как и после добавления через мастер)."""
    account = db.query(Account).filter_by(identifier=identifier).one_or_none()
    if account is None:
        account = Account(identifier=identifier)
        db.add(account)
    bind_session(account, dest)
    account.proxy = proxy_str
    account.is_authorized = True  # вход только что выполнен (CreateNewSession)
    account.enabled = False
    account.last_error = None
    db.commit()
    return account
