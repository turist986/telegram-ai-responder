from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..auth import require_login
from ..database import get_db
from ..models import Account
from ..services.chat_status import pause_now, set_limit_override
from ..services.dialogs import (
    MAX_PAUSE_MINUTES, MODE_GENERAL, DialogOverrideError, get_general_rule, get_mode, list_active_dialogs, save_mode,
    set_override,
)
from ..services.settings_store import get_protection
from ..templating import templates

router = APIRouter(prefix="/dialogs")


@router.get("", response_class=HTMLResponse)
async def dialogs_page(request: Request, user: str = Depends(require_login), db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        "dialogs.html",
        {
            "request": request,
            "rows": list_active_dialogs(db),
            "default_limit": get_protection(db)["dialog_message_limit"],
            "default_pause": get_protection(db)["dialog_pause_minutes"],
            "message": request.query_params.get("msg"),
            "mode": get_mode(db),
            "rule": get_general_rule(db),
            "general": get_mode(db) == MODE_GENERAL,
        },
    )


@router.post("/mode")
async def set_dialogs_mode(
    mode: str = Form("selective"),
    limit: str = Form(""),
    action: str = Form(""),
    pause_value: str = Form(""),
    pause_unit: str = Form("minutes"),
    user: str = Depends(require_login),
    db: Session = Depends(get_db),
):
    try:
        save_mode(db, mode, limit, action, pause_value, pause_unit)
    except DialogOverrideError as exc:
        return RedirectResponse(f"/dialogs?msg={quote(str(exc))}", status_code=303)
    text = "Режим: общая настройка — правило сохранено" if mode == MODE_GENERAL else "Режим: выборочная настройка"
    return RedirectResponse(f"/dialogs?msg={quote(text)}", status_code=303)


@router.post("/{account_id}/{chat_id}/set")
async def set_dialog_override(
    account_id: int,
    chat_id: str,
    message_limit: str = Form(""),
    pause_minutes: str = Form(""),
    user: str = Depends(require_login),
    db: Session = Depends(get_db),
):
    if db.get(Account, account_id) is None:
        return RedirectResponse("/dialogs?msg=" + quote("Этот аккаунт уже не существует"), status_code=303)
    try:
        set_override(db, account_id, chat_id, message_limit, pause_minutes)
    except DialogOverrideError as exc:
        return RedirectResponse(f"/dialogs?msg={quote(str(exc))}", status_code=303)
    return RedirectResponse("/dialogs?msg=" + quote(f"Ручной лимит для чата {chat_id} сохранён"), status_code=303)


@router.post("/{account_id}/{chat_id}/clear")
async def clear_dialog_override(
    account_id: int, chat_id: str, user: str = Depends(require_login), db: Session = Depends(get_db)
):
    set_limit_override(db, account_id, chat_id, None, None)
    return RedirectResponse("/dialogs?msg=" + quote(f"Ручной лимит для чата {chat_id} снят — снова общая настройка"),
                             status_code=303)


@router.post("/{account_id}/{chat_id}/pause")
async def pause_dialog_now(
    account_id: int,
    chat_id: str,
    minutes: str = Form(""),
    user: str = Depends(require_login),
    db: Session = Depends(get_db),
):
    # str, а не int: нечисловой ввод давал голую JSON-ошибку 422 вместо сообщения на странице
    if not minutes.strip().isdigit() or not 1 <= int(minutes) <= MAX_PAUSE_MINUTES:
        return RedirectResponse(
            "/dialogs?msg=" + quote(f"Пауза: укажите хотя бы 1 минуту (целое число, не больше {MAX_PAUSE_MINUTES})"),
            status_code=303)
    minutes = int(minutes)
    until = pause_now(db, account_id, chat_id, minutes)
    if until is None:
        return RedirectResponse(
            "/dialogs?msg=" + quote(
                "Для этого чата ещё не определён статус (аккаунт был выключен на момент сообщения) — "
                "попробуйте ещё раз, когда придёт следующее сообщение"
            ),
            status_code=303,
        )
    return RedirectResponse(
        "/dialogs?msg=" + quote(f"Чат {chat_id}: пауза до {until:%H:%M:%S} UTC"), status_code=303
    )
