from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..auth import require_login
from ..database import get_db
from ..services.blacklist import BlacklistConfigError, accounts_with_blacklist, add_entry, delete_entry
from ..templating import templates

router = APIRouter(prefix="/blacklist")


@router.get("", response_class=HTMLResponse)
async def blacklist_page(request: Request, user: str = Depends(require_login), db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        "blacklist.html",
        {
            "request": request,
            "rows": accounts_with_blacklist(db),
            "message": request.query_params.get("msg"),
        },
    )


@router.post("/{account_id}/add")
async def add(
    account_id: int,
    chat_id: str = Form(...),
    note: str = Form(""),
    user: str = Depends(require_login),
    db: Session = Depends(get_db),
):
    try:
        add_entry(db, account_id, chat_id, note)
    except BlacklistConfigError as exc:
        return RedirectResponse(f"/blacklist?msg={quote(str(exc))}", status_code=303)
    return RedirectResponse("/blacklist?msg=" + quote(f"Чат {chat_id.strip()} добавлен в чёрный список"), status_code=303)


@router.post("/{entry_id}/delete")
async def delete(entry_id: int, user: str = Depends(require_login), db: Session = Depends(get_db)):
    delete_entry(db, entry_id)
    return RedirectResponse("/blacklist?msg=" + quote("Запись удалена из чёрного списка"), status_code=303)
