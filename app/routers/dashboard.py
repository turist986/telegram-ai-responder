from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..auth import require_login
from ..database import get_db
from ..models import Account
from ..services.settings_store import is_global_enabled
from ..templating import templates

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, user: str = Depends(require_login), db: Session = Depends(get_db)):
    accounts = db.query(Account).order_by(Account.identifier).all()
    context = {
        "request": request,
        "user": user,
        "accounts": accounts,
        "global_enabled": is_global_enabled(db),
        "total": len(accounts),
        "active": sum(1 for a in accounts if a.enabled),
    }
    return templates.TemplateResponse("dashboard.html", context)
