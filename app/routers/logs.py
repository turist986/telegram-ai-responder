from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..auth import require_login
from ..database import get_db
from ..models import Account, DialogMessage
from ..templating import templates

router = APIRouter(prefix="/logs")


@router.get("", response_class=HTMLResponse)
async def logs_page(request: Request, user: str = Depends(require_login), db: Session = Depends(get_db)):
    messages = (
        db.query(DialogMessage).order_by(DialogMessage.created_at.desc()).limit(200).all()
    )
    accounts = {a.id: a for a in db.query(Account).all()}
    return templates.TemplateResponse(
        "logs.html", {"request": request, "messages": messages, "accounts": accounts}
    )
