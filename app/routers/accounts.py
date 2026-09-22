import datetime as dt
import logging
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..auth import require_login
from ..config import settings
from ..database import get_db
from ..models import Account
from ..services.excel_loader import sync_accounts_from_excel
from ..services.proxy import (
    ProxyConfigError,
    build_proxy_url,
    mask_proxy,
    normalize_proxy,
    parse_proxy,
    split_proxy,
    test_proxy,
)
from ..services import worker_control
from ..services.onboarding import list_api_pools, proxy_in_use
from ..services.session_utils import tdata_to_session
from ..services.settings_store import get_protection

LEGACY_IMPORT_OFF_MSG = quote(
    "Импорт готовых .session/TData отключён: используйте «Добавить аккаунт» (вход через прокси, "
    "собственный api_id). Причины: чужой api_id/IP у готовой сессии и риск AUTH_KEY_DUPLICATED "
    "при общем ключе с Telegram Desktop. Включить принудительно: ALLOW_LEGACY_SESSION_IMPORT=true в .env."
)
from ..templating import templates

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/accounts")


def _fresh_session_path(identifier: str) -> Path:
    """Каждая загрузка пишет в НОВЫЙ файл: перезапись файла, который прямо сейчас держит
    подключённый воркер, портит сессию, а воркер не заметит смены (путь тот же)."""
    settings.sessions_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w.-]", "_", identifier)
    return settings.sessions_dir / f"{safe}-{dt.datetime.now():%Y%m%d%H%M%S%f}.session"


def _discard_session_file(path: str) -> None:
    for suffix in ("", "-journal", ".lock"):
        try:
            Path(path + suffix).unlink(missing_ok=True)
        except OSError:
            logger.warning("Не удалось удалить старый файл сессии %s%s (занят) — удалите вручную", path, suffix)


def _bind_session(account: Account, dest: Path) -> None:
    old = account.session_path
    account.session_path = str(dest)
    account.is_authorized = False
    account.last_error = None
    if old and old != str(dest):
        _discard_session_file(old)


def _natural_key(value: str):
    return (0, int(value), "") if value.isdigit() else (1, 0, value)


def _find_tdata_dirs(root: Path) -> list[Path]:
    dirs = {p.parent for name in ("key_datas", "key_data") for p in root.rglob(name) if p.is_file()}
    return sorted(dirs, key=lambda p: str(p))


@router.get("", response_class=HTMLResponse)
async def accounts_page(request: Request, user: str = Depends(require_login), db: Session = Depends(get_db)):
    accounts = db.query(Account).order_by(Account.identifier).all()
    pool_counts: dict[int, int] = {}
    for a in accounts:
        if a.api_id:
            pool_counts[a.api_id] = pool_counts.get(a.api_id, 0) + 1
    return templates.TemplateResponse(
        "accounts.html",
        {
            "request": request,
            "accounts": accounts,
            "pool_counts": pool_counts,
            "api_pools": list_api_pools(db),
            "pool_limit": get_protection(db)["api_pool_max_accounts"],
            "message": request.query_params.get("msg"),
            "worker_running": worker_control.is_running(),
            "mask_proxy": mask_proxy,
            "split_proxy": split_proxy,
            "now": dt.datetime.utcnow(),
            "legacy_import": settings.allow_legacy_session_import,
        },
    )


@router.post("/release-accounts")
async def release_accounts(user: str = Depends(require_login)):
    """Освобождает все аккаунты для Telegram Desktop: останавливает воркер (закрывает
    все подключения к Telegram и снимает блокировки сессий). Сам сайт продолжает
    работать — он к Telegram не подключается, ключами не пользуется и потому
    на аккаунты не влияет. Вернуть автоответчик: кнопка «Запустить воркер»."""
    if not worker_control.is_running():
        return RedirectResponse("/accounts?msg=Воркер+уже+остановлен+—+аккаунты+свободны", status_code=303)
    # stop_worker() ждёт завершения процесса — выносим в поток, чтобы не подвесить сайт
    await run_in_threadpool(worker_control.stop_worker)
    return RedirectResponse(
        "/accounts?msg=Аккаунты+отключены+от+Telegram.+Можно+заходить+с+Desktop.+"
        "Не+запускайте+воркер,+пока+Desktop+открыт",
        status_code=303,
    )


@router.post("/worker/toggle")
async def toggle_worker(user: str = Depends(require_login)):
    if worker_control.is_running():
        worker_control.stop_worker()
        msg = "Воркер остановлен"
    else:
        worker_control.start_worker()
        msg = "Воркер запущен"
    return RedirectResponse(f"/accounts?msg={msg}", status_code=303)


@router.post("/{account_id}/toggle")
async def toggle_account(account_id: int, user: str = Depends(require_login), db: Session = Depends(get_db)):
    account = db.get(Account, account_id)
    if account:
        account.enabled = not account.enabled
        db.commit()
    return RedirectResponse("/accounts", status_code=303)


@router.post("/{account_id}/proxy")
async def set_proxy(
    account_id: int,
    proxy: str = Form(""),
    proxy_scheme: str = Form("socks5"),
    proxy_host: str = Form(""),
    proxy_port: str = Form(""),
    proxy_user: str = Form(""),
    proxy_password: str = Form(""),
    user: str = Depends(require_login),
    db: Session = Depends(get_db),
):
    """Прокси можно задать отдельными полями (хост, порт, логин, пароль) либо одной
    строкой в любом распространённом формате."""
    account = db.get(Account, account_id)
    if account is None:
        return RedirectResponse("/accounts", status_code=303)

    try:
        if proxy_host.strip():
            password = proxy_password
            if not password and account.proxy:
                # пароль в форме не показывается — пустое поле означает «оставить прежний»
                old = urlparse(account.proxy)
                old_user = unquote(old.username) if old.username else None
                if old.password and (old.hostname, old_user) == (proxy_host.strip(), proxy_user.strip() or None):
                    password = unquote(old.password)
            normalized = build_proxy_url(proxy_scheme, proxy_host, proxy_port, proxy_user.strip(), password)
        else:
            normalized = normalize_proxy(proxy)
        if normalized:
            parse_proxy(normalized)
    except ProxyConfigError as exc:
        return RedirectResponse(f"/accounts?msg={exc}", status_code=303)

    if not normalized and settings.require_proxy:
        return RedirectResponse("/accounts?msg=Прокси+обязателен+—+удалить+его+нельзя,+только+заменить", status_code=303)
    if normalized:
        owner = proxy_in_use(db, normalized, exclude_identifier=account.identifier)
        if owner:
            return RedirectResponse(
                f"/accounts?msg={quote('Этот прокси уже используется аккаунтом ' + owner + '. Нужен отдельный прокси на каждый аккаунт')}",
                status_code=303,
            )

    account.proxy = normalized or None
    db.commit()
    return RedirectResponse("/accounts?msg=Прокси+сохранён", status_code=303)


@router.post("/{account_id}/proxy-test")
async def check_proxy(account_id: int, user: str = Depends(require_login), db: Session = Depends(get_db)):
    account = db.get(Account, account_id)
    if account is None or not account.proxy:
        return RedirectResponse("/accounts?msg=У+аккаунта+не+задан+прокси", status_code=303)
    ok, text = await run_in_threadpool(test_proxy, account.proxy)
    prefix = "✅ " if ok else "❌ "
    return RedirectResponse(f"/accounts?msg={quote(prefix + text)}", status_code=303)


@router.post("/{account_id}/disclaimer-mode")
async def toggle_disclaimer_mode(account_id: int, user: str = Depends(require_login), db: Session = Depends(get_db)):
    account = db.get(Account, account_id)
    if account:
        account.disclaimer_every_message = account.disclaimer_every_message is False
        db.commit()
    return RedirectResponse("/accounts", status_code=303)


@router.post("/{account_id}/prompt")
async def set_prompt(
    account_id: int,
    system_prompt: str = Form(""),
    user: str = Depends(require_login),
    db: Session = Depends(get_db),
):
    account = db.get(Account, account_id)
    if account:
        account.system_prompt = system_prompt.strip() or None
        db.commit()
    return RedirectResponse("/accounts?msg=Промпт+сохранён", status_code=303)


@router.post("/upload-session")
async def upload_session(
    identifier: str = Form(...),
    session_file: UploadFile = File(...),
    user: str = Depends(require_login),
    db: Session = Depends(get_db),
):
    if not settings.allow_legacy_session_import:
        return RedirectResponse(f"/accounts?msg={LEGACY_IMPORT_OFF_MSG}", status_code=303)
    identifier = identifier.strip()
    settings.sessions_dir.mkdir(parents=True, exist_ok=True)
    dest = _fresh_session_path(identifier)
    with dest.open("wb") as f:
        shutil.copyfileobj(session_file.file, f)

    account = db.query(Account).filter_by(identifier=identifier).one_or_none()
    if account is None:
        account = Account(identifier=identifier, enabled=True)
        db.add(account)
    _bind_session(account, dest)
    account.is_authorized = False  # проверится при следующем запуске воркера
    db.commit()

    return RedirectResponse("/accounts?msg=Сессия+загружена", status_code=303)


@router.post("/upload-tdata")
async def upload_tdata(
    identifier: str = Form(...),
    tdata_zip: UploadFile = File(...),
    user: str = Depends(require_login),
    db: Session = Depends(get_db),
):
    """Принимает .zip с папкой TData и конвертирует её в .session через opentele."""
    if not settings.allow_legacy_session_import:
        return RedirectResponse(f"/accounts?msg={LEGACY_IMPORT_OFF_MSG}", status_code=303)
    identifier = identifier.strip()
    settings.sessions_dir.mkdir(parents=True, exist_ok=True)
    dest = _fresh_session_path(identifier)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        zip_path = tmp_path / "tdata.zip"
        with zip_path.open("wb") as f:
            shutil.copyfileobj(tdata_zip.file, f)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(tmp_path / "tdata")

        try:
            tdata_to_session(tmp_path / "tdata", dest)
        except RuntimeError as exc:
            return RedirectResponse(f"/accounts?msg={exc}", status_code=303)

    account = db.query(Account).filter_by(identifier=identifier).one_or_none()
    if account is None:
        account = Account(identifier=identifier, enabled=True)
        db.add(account)
    _bind_session(account, dest)
    account.is_authorized = False
    db.commit()

    return RedirectResponse("/accounts?msg=TData+сконвертирована+в+сессию", status_code=303)


@router.post("/upload")
async def upload_all(
    identifier: str = Form(""),
    auth_file: UploadFile | None = File(None),
    excel_file: UploadFile | None = File(None),
    user: str = Depends(require_login),
    db: Session = Depends(get_db),
):
    """Единая загрузка: .session или TData (.zip) и/или Excel-таблица."""
    identifier = identifier.strip()
    parts = []

    has_auth = bool(auth_file and auth_file.filename)
    has_excel = bool(excel_file and excel_file.filename)
    if not has_auth and not has_excel:
        return RedirectResponse("/accounts?msg=Выберите+файл+сессии/TData+и/или+Excel", status_code=303)
    if has_auth and not settings.allow_legacy_session_import:
        return RedirectResponse(f"/accounts?msg={LEGACY_IMPORT_OFF_MSG}", status_code=303)

    if has_excel:
        settings.managers_excel_path.parent.mkdir(parents=True, exist_ok=True)
        with settings.managers_excel_path.open("wb") as f:
            shutil.copyfileobj(excel_file.file, f)
        try:
            result = sync_accounts_from_excel(settings.managers_excel_path, db)
        except ValueError as exc:
            return RedirectResponse(f"/accounts?msg={exc}", status_code=303)
        parts.append(f"Excel: {result['created']} новых, {result['updated']} обновлено")

    if has_auth:
        settings.sessions_dir.mkdir(parents=True, exist_ok=True)
        name = auth_file.filename.lower()

        if name.endswith(".zip"):
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                zip_path = tmp_path / "tdata.zip"
                with zip_path.open("wb") as f:
                    shutil.copyfileobj(auth_file.file, f)
                try:
                    with zipfile.ZipFile(zip_path) as zf:
                        zf.extractall(tmp_path / "tdata")
                except zipfile.BadZipFile as exc:
                    return RedirectResponse(f"/accounts?msg={exc}", status_code=303)

                tdata_dirs = _find_tdata_dirs(tmp_path / "tdata")
                if len(tdata_dirs) > 1:
                    # Архив с несколькими аккаунтами: раздаём TData по идентификаторам
                    # из Excel, у которых ещё нет сессии (в порядке возрастания).
                    free = sorted(
                        (a.identifier for a in db.query(Account).all() if not a.session_path),
                        key=_natural_key,
                    )
                    if len(free) < len(tdata_dirs):
                        return RedirectResponse(
                            f"/accounts?msg=В архиве {len(tdata_dirs)} аккаунтов, а в таблице свободных "
                            f"идентификаторов без сессии только {len(free)}",
                            status_code=303,
                        )
                    for ident, tdir in zip(free, tdata_dirs):
                        dest_i = _fresh_session_path(ident)
                        try:
                            tdata_to_session(tdir, dest_i)
                        except RuntimeError as exc:
                            return RedirectResponse(f"/accounts?msg={ident}: {exc}", status_code=303)
                        acc = db.query(Account).filter_by(identifier=ident).one()
                        _bind_session(acc, dest_i)
                        acc.is_authorized = False
                        db.commit()
                    parts.append(f"TData сконвертирована для {len(tdata_dirs)} аккаунтов ({', '.join(free[:len(tdata_dirs)])})")
                    return RedirectResponse(f"/accounts?msg={'; '.join(parts)}", status_code=303)

                if not identifier:
                    return RedirectResponse("/accounts?msg=Укажите+идентификатор+аккаунта", status_code=303)
                dest = _fresh_session_path(identifier)
                try:
                    tdata_to_session(tmp_path / "tdata", dest)
                except RuntimeError as exc:
                    return RedirectResponse(f"/accounts?msg={exc}", status_code=303)
            parts.append("TData сконвертирована")
        elif name.endswith(".session"):
            if not identifier:
                return RedirectResponse("/accounts?msg=Укажите+идентификатор+аккаунта", status_code=303)
            dest = _fresh_session_path(identifier)
            with dest.open("wb") as f:
                shutil.copyfileobj(auth_file.file, f)
            parts.append("сессия загружена")
        else:
            return RedirectResponse("/accounts?msg=Файл+должен+быть+.session+или+.zip+(TData)", status_code=303)

        account = db.query(Account).filter_by(identifier=identifier).one_or_none()
        if account is None:
            account = Account(identifier=identifier, enabled=True)
            db.add(account)
        _bind_session(account, dest)
        account.is_authorized = False
        db.commit()

    return RedirectResponse(f"/accounts?msg={'; '.join(parts)}", status_code=303)


@router.post("/sync-excel")
async def sync_excel(
    excel_file: UploadFile = File(...),
    user: str = Depends(require_login),
    db: Session = Depends(get_db),
):
    settings.managers_excel_path.parent.mkdir(parents=True, exist_ok=True)
    with settings.managers_excel_path.open("wb") as f:
        shutil.copyfileobj(excel_file.file, f)

    try:
        result = sync_accounts_from_excel(settings.managers_excel_path, db)
    except ValueError as exc:
        return RedirectResponse(f"/accounts?msg={exc}", status_code=303)

    return RedirectResponse(
        f"/accounts?msg=Синхронизировано:+{result['created']}+новых,+{result['updated']}+обновлено",
        status_code=303,
    )


@router.post("/{account_id}/delete")
async def delete_account(account_id: int, user: str = Depends(require_login), db: Session = Depends(get_db)):
    account = db.get(Account, account_id)
    if account:
        if account.session_path and Path(account.session_path).exists():
            try:
                Path(account.session_path).unlink(missing_ok=True)
            except OSError as exc:
                # На Windows файл сессии может быть занят воркером (Telethon
                # держит его открытым, пока клиент подключён) — удаление
                # аккаунта не должно из-за этого падать. Воркер сам закроет
                # клиента на ближайшей сверке (видит, что аккаунта больше
                # нет в БД) и файл можно будет удалить вручную позже.
                logger.warning("Account %s: could not remove session file %s: %s", account_id, account.session_path, exc)
        db.delete(account)
        db.commit()
    return RedirectResponse("/accounts", status_code=303)
