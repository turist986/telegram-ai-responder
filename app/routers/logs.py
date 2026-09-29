from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..auth import require_login
from ..database import get_db
from ..models import Account, ChatStatus, DialogMessage
from ..templating import templates

router = APIRouter(prefix="/logs")


@router.get("", response_class=HTMLResponse)
async def logs_page(request: Request, user: str = Depends(require_login), db: Session = Depends(get_db)):
    messages = (
        db.query(DialogMessage).order_by(DialogMessage.created_at.desc()).limit(200).all()
    )
    accounts = {a.id: a for a in db.query(Account).all()}
    # Имя собеседника (см. ChatStatus.display_name) — только чтобы голый chat_id можно было
    # опознать глазами; ни на что в логике автоответчика не влияет.
    display_names = {
        (s.account_id, s.chat_id): s.display_name
        for s in db.query(ChatStatus).filter(ChatStatus.display_name.isnot(None)).all()
    }
    return templates.TemplateResponse(
        "logs.html", {"request": request, "messages": messages, "accounts": accounts, "display_names": display_names}
    )
