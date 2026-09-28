import datetime as dt
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..auth import require_login
from ..database import get_db
from ..services.niche import NicheConfigError, accounts_with_niches, add_niche, delete_niche
from ..templating import templates

router = APIRouter(prefix="/niches")


@router.get("", response_class=HTMLResponse)
async def niches_page(request: Request, user: str = Depends(require_login), db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        "niches.html",
        {
            "request": request,
            "rows": accounts_with_niches(db),
            "message": request.query_params.get("msg"),
            "now": dt.datetime.utcnow(),
        },
    )


@router.post("/{account_id}/add")
async def add(
    account_id: int,
    title: str = Form(...),
    description: str = Form(...),
    active_from: str = Form(...),
    user: str = Depends(require_login),
    db: Session = Depends(get_db),
):
    try:
        add_niche(db, account_id, title, description, active_from)
    except NicheConfigError as exc:
        return RedirectResponse(f"/niches?msg={quote(str(exc))}", status_code=303)
    return RedirectResponse("/niches?msg=" + quote(f"Ниша «{title.strip()}» добавлена"), status_code=303)


@router.post("/{niche_id}/delete")
async def delete(niche_id: int, user: str = Depends(require_login), db: Session = Depends(get_db)):
    delete_niche(db, niche_id)
    return RedirectResponse("/niches?msg=" + quote("Ниша удалена"), status_code=303)
