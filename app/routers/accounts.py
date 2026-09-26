import asyncio
import datetime as dt
import logging
import random
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
    looks_like_proxy_string,
    mask_proxy,
    normalize_proxy,
    parse_proxy,
    proxy_identity,
    split_proxy,
    test_proxy,
)
from ..services import worker_control
from ..services.onboarding import list_api_pools, proxy_in_use
from ..services.session_files import (
    bind_session as _bind_session,
    fresh_session_path as _fresh_session_path,
    finalize_tdata_account as _finalize_tdata_account,
)
from ..services.session_utils import find_tdata_dirs as _find_tdata_dirs
from ..services.session_utils import tdata_to_session
from ..services.settings_store import get_protection

LEGACY_IMPORT_OFF_MSG = quote(
    "Импорт готовых .session-файлов отключён в настройках (ALLOW_LEGACY_SESSION_IMPORT=false в .env). "
    "Включите его или используйте «Добавить аккаунт» (вход по номеру через прокси, собственный api_id)."
)
TDATA_IMPORT_OFF_MSG = quote(
    "Импорт TData отключён в настройках (ALLOW_TDATA_IMPORT=false в .env). "
    "Включите его или используйте «Добавить аккаунт» (вход по номеру через прокси, собственный api_id)."
)
from ..templating import templates

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/accounts")


async def _convert_tdata(proxy_str: str | None, tdir: Path, dest: Path) -> None:
    """Конвертирует одну папку TData в сессию ЧЕРЕЗ прокси аккаунта. Бросает RuntimeError
    с понятным пользователю текстом. Выполняется в потоке: CreateNewSession делает
    реальные сетевые запросы к Telegram, в event loop панели их гонять нельзя."""
    if not proxy_str:
        raise RuntimeError(
            "не задан прокси — импорт TData идёт через прокси аккаунта, иначе в Telegram ушёл бы реальный "
            "IP этой машины. Укажите прокси (поле в форме, столбец «Прокси» в Excel или в таблице ниже)"
        )
    try:
        proxy_tuple = parse_proxy(proxy_str)
    except ProxyConfigError as exc:
        raise RuntimeError(f"некорректный прокси: {exc}") from exc
    await run_in_threadpool(tdata_to_session, tdir, dest, proxy_tuple)


def _natural_key(value: str):
    return (0, int(value), "") if value.isdigit() else (1, 0, value)


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
            "tdata_import": settings.allow_tdata_import,
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
        if proxy.strip():
            proxy_host = ""     # явно вставленная строка главнее полей (они предзаполнены старым прокси)
        elif looks_like_proxy_string(proxy_host, proxy_port, proxy_user, proxy_password):
            proxy, proxy_host = proxy_host, ""      # строку вставили в поле «хост» — разбираем как строку
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
            normalized = normalize_proxy(proxy, default_scheme=(proxy_scheme or "socks5").strip().lower())
        if normalized:
            parse_proxy(normalized)
    except ProxyConfigError as exc:
        return RedirectResponse(f"/accounts?msg={quote(str(exc))}", status_code=303)

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


def _resolve_account_proxy(db: Session, identifier: str, proxy_input: str) -> str:
    """Прокси для импорта: явно введённый в форме (проверяется на формат и уникальность)
    либо уже сохранённый у существующего аккаунта. Бросает RuntimeError с текстом."""
    account = db.query(Account).filter_by(identifier=identifier).one_or_none()
    if proxy_input.strip():
        try:
            normalized = normalize_proxy(proxy_input)
            if normalized:
                parse_proxy(normalized)
        except ProxyConfigError as exc:
            raise RuntimeError(str(exc)) from exc
        owner = proxy_in_use(db, normalized, exclude_identifier=identifier)
        if owner:
            raise RuntimeError(f"этот прокси уже используется аккаунтом {owner} — нужен отдельный на каждый аккаунт")
        return normalized
    if account is not None and account.proxy:
        return account.proxy
    return ""


@router.post("/upload-tdata")
async def upload_tdata(
    identifier: str = Form(...),
    proxy: str = Form(""),
    tdata_zip: UploadFile = File(...),
    user: str = Depends(require_login),
    db: Session = Depends(get_db),
):
    """Принимает .zip с папкой TData и создаёт из неё НОВЫЙ отдельный сеанс (CreateNewSession),
    не делящий ключ с Telegram Desktop. Всё общение с Telegram идёт через прокси аккаунта."""
    if not settings.allow_tdata_import:
        return RedirectResponse(f"/accounts?msg={TDATA_IMPORT_OFF_MSG}", status_code=303)
    identifier = identifier.strip()
    if not identifier:
        return RedirectResponse("/accounts?msg=Укажите+идентификатор+аккаунта", status_code=303)
    settings.sessions_dir.mkdir(parents=True, exist_ok=True)
    dest = _fresh_session_path(identifier)

    try:
        proxy_str = _resolve_account_proxy(db, identifier, proxy)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            zip_path = tmp_path / "tdata.zip"
            with zip_path.open("wb") as f:
                shutil.copyfileobj(tdata_zip.file, f)
            try:
                with zipfile.ZipFile(zip_path) as zf:
                    zf.extractall(tmp_path / "tdata")
            except zipfile.BadZipFile as exc:
                raise RuntimeError(f"архив повреждён или это не .zip: {exc}") from exc
            await _convert_tdata(proxy_str, tmp_path / "tdata", dest)
    except RuntimeError as exc:
        logger.warning("TData import failed for %s: %s", identifier, exc)
        return RedirectResponse(f"/accounts?msg={quote(f'{identifier}: {exc}')}", status_code=303)

    _finalize_tdata_account(db, identifier, dest, proxy_str)
    return RedirectResponse(
        f"/accounts?msg={quote('TData импортирована в новый отдельный сеанс. Автоответчик выключен — включите через 30–60 минут.')}",
        status_code=303,
    )


@router.post("/upload")
async def upload_all(
    identifier: str = Form(""),
    proxy: str = Form(""),
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
    if has_auth:
        # TData (.zip) и готовые .session включаются независимыми флагами
        is_zip = auth_file.filename.lower().endswith(".zip")
        if is_zip and not settings.allow_tdata_import:
            return RedirectResponse(f"/accounts?msg={TDATA_IMPORT_OFF_MSG}", status_code=303)
        if not is_zip and not settings.allow_legacy_session_import:
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
                    targets = list(zip(free, tdata_dirs))

                    # Проверяем ВСЁ до первой конвертации: у каждого аккаунта должен быть свой
                    # прокси (из столбца «Прокси» в Excel), и они не должны повторяться —
                    # иначе часть аккаунтов импортировалась бы, а часть нет, и в Telegram
                    # успел бы уйти запрос без прокси.
                    seen: dict[tuple, str] = {}
                    for ident, _tdir in targets:
                        acc = db.query(Account).filter_by(identifier=ident).one()
                        if not acc.proxy:
                            return RedirectResponse(
                                f"/accounts?msg={quote(f'У аккаунта {ident} не задан прокси — заполните столбец «Прокси» в Excel и загрузите таблицу снова. Ничего не импортировано.')}",
                                status_code=303,
                            )
                        key = proxy_identity(acc.proxy)
                        if key in seen:
                            return RedirectResponse(
                                f"/accounts?msg={quote(f'Аккаунты {seen[key]} и {ident} используют один прокси — нужен отдельный на каждый. Ничего не импортировано.')}",
                                status_code=303,
                            )
                        seen[key] = ident

                    done, failed = [], []
                    for n, (ident, tdir) in enumerate(targets):
                        if n:  # не «залпом»: заходы с паузой, как и запуск воркера
                            await asyncio.sleep(random.uniform(settings.start_stagger_min_seconds,
                                                               settings.start_stagger_max_seconds))
                        acc = db.query(Account).filter_by(identifier=ident).one()
                        dest_i = _fresh_session_path(ident)
                        try:
                            await _convert_tdata(acc.proxy, tdir, dest_i)
                        except RuntimeError as exc:
                            logger.warning("TData import failed for %s: %s", ident, exc)
                            failed.append(f"{ident}: {exc}")
                            continue
                        _finalize_tdata_account(db, ident, dest_i, acc.proxy)
                        done.append(ident)
                    if done:
                        parts.append(f"Импортировано (новые отдельные сеансы, автоответчик ВЫКЛЮЧЕН — включите через 30–60 минут): {', '.join(done)}")
                    if failed:
                        parts.append("Ошибки: " + "; ".join(failed))
                    return RedirectResponse(f"/accounts?msg={quote('; '.join(parts))}", status_code=303)

                if not identifier:
                    return RedirectResponse("/accounts?msg=Укажите+идентификатор+аккаунта", status_code=303)
                dest = _fresh_session_path(identifier)
                try:
                    proxy_str = _resolve_account_proxy(db, identifier, proxy)
                    await _convert_tdata(proxy_str, tmp_path / "tdata", dest)
                except RuntimeError as exc:
                    logger.warning("TData import failed for %s: %s", identifier, exc)
                    return RedirectResponse(f"/accounts?msg={quote(f'{identifier}: {exc}')}", status_code=303)
                _finalize_tdata_account(db, identifier, dest, proxy_str)
            parts.append("TData импортирована в новый отдельный сеанс (автоответчик выключен — включите через 30–60 минут)")
            return RedirectResponse(f"/accounts?msg={quote('; '.join(parts))}", status_code=303)
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
