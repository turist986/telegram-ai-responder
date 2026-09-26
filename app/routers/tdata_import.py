import logging
import re
import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from ..auth import require_login
from ..config import settings
from ..database import get_db
from ..services import tdata_batch as tb
from ..services.archive_utils import detect_kind
from ..services.session_utils import tdata_available
from ..templating import templates

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/accounts/import-tdata")


def _form(request: Request, error: str = "", values: dict | None = None):
    available, hint = tdata_available()
    active = tb.active_job()
    return templates.TemplateResponse(
        "tdata_import.html",
        {
            "request": request, "error": error, "v": values or {},
            "enabled": settings.allow_tdata_import, "available": available, "hint": hint,
            "active_token": active.token if active else None,
        },
    )


def _save_uploads(files: list[UploadFile], target: Path) -> list[tuple[str, Path]]:
    target.mkdir(parents=True, exist_ok=True)
    saved = []
    for n, f in enumerate(files):
        name = Path(f.filename or f"archive{n}").name
        safe = re.sub(r"[^\w.\-]", "_", name)
        path = target / f"{n}_{safe}"
        with path.open("wb") as out:
            shutil.copyfileobj(f.file, out)
        saved.append((name, path))
    return saved


@router.get("", response_class=HTMLResponse)
async def import_page(request: Request, user: str = Depends(require_login)):
    return _form(request)


@router.post("")
async def import_start(
    request: Request,
    archives: list[UploadFile] = File(default=[]),
    proxies: str = Form(""),
    identifiers: str = Form(""),
    replace_existing: str = Form(""),
    archive_password: str = Form(""),
    tdata_passcode: str = Form(""),
    cloud_password: str = Form(""),
    user: str = Depends(require_login),
    db: Session = Depends(get_db),
):
    values = {"proxies": proxies, "identifiers": identifiers, "replace_existing": bool(replace_existing)}
    if not settings.allow_tdata_import:
        return _form(request, "Импорт TData отключён в настройках (ALLOW_TDATA_IMPORT=false в .env)", values)
    available, hint = tdata_available()
    if not available:
        return _form(request, f"Импорт TData сейчас недоступен: {hint}", values)
    try:
        tb.ensure_no_active_job()
    except tb.TDataBatchError as exc:
        return _form(request, str(exc), values)

    files = [f for f in archives if f.filename]
    if not files:
        return _form(request, "Выберите или перетащите хотя бы один архив (.zip или .rar)", values)

    workdir = Path(tempfile.mkdtemp(prefix="tdata_import_"))
    try:
        saved = await run_in_threadpool(_save_uploads, files, workdir / "up")
        for name, path in saved:
            if detect_kind(path) is None:
                raise tb.TDataBatchError(f"{name}: это не .zip и не .rar (7z и другие форматы не поддерживаются)")
        job = await run_in_threadpool(
            tb.prepare_batch, db, saved, workdir / "x",
            proxies_text=proxies, identifiers_text=identifiers,
            replace_existing=bool(replace_existing), archive_password=archive_password or None,
        )
        shutil.rmtree(workdir / "up", ignore_errors=True)  # исходные архивы больше не нужны
        job.workdir = workdir
        tb.start_job(job, passcode=tdata_passcode or None, cloud_password=cloud_password or None)
    except tb.TDataBatchError as exc:
        shutil.rmtree(workdir, ignore_errors=True)
        return _form(request, str(exc), values)
    except Exception:
        shutil.rmtree(workdir, ignore_errors=True)
        logger.exception("TData batch preparation failed")
        return _form(request, "Внутренняя ошибка при подготовке импорта — подробности в логе сервера", values)
    return RedirectResponse(f"/accounts/import-tdata/{job.token}", status_code=303)


@router.get("/{token}", response_class=HTMLResponse)
async def job_page(request: Request, token: str, user: str = Depends(require_login)):
    job = tb.get_job(token)
    if job is None:
        return RedirectResponse("/accounts/import-tdata", status_code=303)
    return templates.TemplateResponse("tdata_job.html", {"request": request, "token": token})


@router.get("/{token}/status")
async def job_status(token: str, user: str = Depends(require_login)):
    job = tb.get_job(token)
    if job is None:
        return JSONResponse({"expired": True})
    return JSONResponse(tb.job_view(job), headers={"Cache-Control": "no-store"})
