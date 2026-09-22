"""Библиотека приложений (api_id/api_hash): пары можно загрузить готовыми (без
браузера my.telegram.org) и назначить конкретному аккаунту или раскидать
автоматически по всем аккаунтам, у которых своего api_id ещё нет."""
import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..auth import require_login
from ..database import get_db
from ..models import Account, ApiCredential
from ..services import onboarding as ob

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/accounts/api")


@router.post("/upload")
async def upload_credentials(text: str = Form(...), user: str = Depends(require_login), db: Session = Depends(get_db)):
    try:
        result = ob.add_credentials(db, text)
    except ob.CredentialUploadError as exc:
        return RedirectResponse(f"/accounts?msg={quote(str(exc))}", status_code=303)
    msg = f"Загружено приложений: {len(result['added'])}"
    if result["skipped"]:
        msg += f"; пропущено (api_id уже есть): {', '.join(map(str, result['skipped']))}"
    return RedirectResponse(f"/accounts?msg={quote(msg)}", status_code=303)


@router.post("/{account_id}/assign")
async def assign_credential(
    account_id: int, api_id: str = Form(...), user: str = Depends(require_login), db: Session = Depends(get_db)
):
    if not api_id.strip().isdigit():
        return RedirectResponse("/accounts?msg=Некорректный+api_id", status_code=303)
    try:
        ob.assign_api_to_account(db, account_id, int(api_id))
    except ob.OnboardingError as exc:
        return RedirectResponse(f"/accounts?msg={quote(str(exc))}", status_code=303)
    return RedirectResponse("/accounts?msg=api_id+назначен+аккаунту", status_code=303)


@router.post("/auto-distribute")
async def auto_distribute(user: str = Depends(require_login), db: Session = Depends(get_db)):
    result = ob.auto_distribute(db)
    parts = []
    if result["assigned"]:
        parts.append("назначено: " + ", ".join(f"{ident} → api_id {api_id}" for ident, api_id in result["assigned"]))
    if result["unassigned"]:
        parts.append("без места (не хватило свободных приложений): " + ", ".join(result["unassigned"]))
    msg = "; ".join(parts) or "Нет аккаунтов без своего api_id — раскидывать нечего"
    return RedirectResponse(f"/accounts?msg={quote(msg)}", status_code=303)


@router.post("/{api_id}/delete")
async def delete_credential(api_id: int, user: str = Depends(require_login), db: Session = Depends(get_db)):
    in_use = db.query(Account).filter(Account.api_id == api_id).count()
    if in_use:
        return RedirectResponse(
            f"/accounts?msg={quote(f'Нельзя удалить: api_id {api_id} используется {in_use} аккаунтами')}",
            status_code=303,
        )
    cred = db.query(ApiCredential).filter_by(api_id=api_id).one_or_none()
    if cred:
        db.delete(cred)
        db.commit()
    return RedirectResponse("/accounts?msg=Приложение+удалено", status_code=303)
