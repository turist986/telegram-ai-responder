import asyncio
import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from ..auth import require_login
from ..database import get_db
from ..services import onboarding as ob
from ..services.api_app_creator import CreatorError
from ..services.settings_store import get_protection
from ..templating import templates

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/accounts/add")


def _page(request: Request, state: ob.Onboarding, error: str = ""):
    return templates.TemplateResponse(
        "add_wizard.html", {"request": request, "s": state, "error": error}
    )


def _form(request: Request, db: Session, error: str = "", values: dict | None = None):
    return templates.TemplateResponse(
        "add_account.html",
        {
            "request": request, "error": error, "v": values or {},
            "pools": ob.list_api_pools(db), "pool_limit": get_protection(db)["api_pool_max_accounts"],
        },
    )


async def _get_or_redirect(token: str):
    await ob.cleanup_expired()
    return ob.get_state(token)


@router.get("", response_class=HTMLResponse)
async def add_form(request: Request, user: str = Depends(require_login), db: Session = Depends(get_db)):
    return _form(request, db)


@router.post("/start")
async def add_start(
    request: Request,
    identifier: str = Form(""),
    phone: str = Form(""),
    manager_name: str = Form(""),
    lang_code: str = Form("ru"),
    proxy_scheme: str = Form("socks5"),
    proxy_host: str = Form(""),
    proxy_port: str = Form(""),
    proxy_user: str = Form(""),
    proxy_password: str = Form(""),
    proxy: str = Form(""),
    pool_api_id: str = Form(""),
    user: str = Depends(require_login),
    db: Session = Depends(get_db),
):
    from ..services.proxy import ProxyConfigError, build_proxy_url

    values = {"identifier": identifier, "phone": phone, "manager_name": manager_name, "lang_code": lang_code,
              "proxy_scheme": proxy_scheme, "proxy_host": proxy_host, "proxy_port": proxy_port,
              "proxy_user": proxy_user, "proxy": proxy, "pool_api_id": pool_api_id}
    try:
        if proxy_host.strip():
            raw_proxy = build_proxy_url(proxy_scheme, proxy_host, proxy_port, proxy_user.strip(), proxy_password)
        else:
            raw_proxy = proxy
        pool_id = None
        if pool_api_id.strip():
            if not pool_api_id.strip().isdigit():
                raise ob.OnboardingError("Некорректный выбор пула api_id")
            pool_id = int(pool_api_id.strip())
        state = await ob.begin(db, identifier, phone, raw_proxy, manager_name, lang_code, pool_api_id=pool_id)
    except (ob.OnboardingError, ProxyConfigError) as exc:
        return _form(request, db, str(exc), values)
    return RedirectResponse(f"/accounts/add/{state.token}", status_code=303)


@router.get("/{token}", response_class=HTMLResponse)
async def wizard(request: Request, token: str, user: str = Depends(require_login)):
    state = await _get_or_redirect(token)
    if state is None:
        return RedirectResponse("/accounts?msg=Сеанс+добавления+завершён+или+истёк", status_code=303)
    return _page(request, state)


@router.get("/{token}/status")
async def status(token: str, user: str = Depends(require_login)):
    state = await _get_or_redirect(token)
    if state is None:
        return JSONResponse({"step": "expired", "message": "Сеанс истёк"})
    return JSONResponse({"step": state.step, "message": state.message})


@router.get("/{token}/screenshot.png")
async def screenshot(token: str, user: str = Depends(require_login)):
    """Кадр headless-браузера для «трансляции» в панель. Виден только вошедшему администратору."""
    state = ob.get_state(token)
    if state is None or state.creator is None:
        return Response(status_code=204)
    try:
        png = await asyncio.to_thread(state.creator.call, "screenshot", timeout=20)
    except CreatorError:
        return Response(status_code=204)
    if not png:
        return Response(status_code=204)
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})


async def _action(request: Request, token: str, coro_factory):
    state = await _get_or_redirect(token)
    if state is None:
        return RedirectResponse("/accounts?msg=Сеанс+добавления+завершён+или+истёк", status_code=303)
    error = ""
    try:
        await coro_factory(state)
    except ob.OnboardingError as exc:
        error = str(exc)
    except Exception as exc:  # noqa: BLE001 — показываем пользователю, а не роняем страницу
        logger.exception("Onboarding step failed")
        error = f"{type(exc).__name__}: {exc}"
    if state.step == "done" and not error:
        ob._STATES.pop(token, None)
        return RedirectResponse(f"/accounts?msg={state.message}", status_code=303)
    return _page(request, state, error)


@router.post("/{token}/web-code")
async def web_code(request: Request, token: str, code: str = Form(""), user: str = Depends(require_login)):
    return await _action(request, token, lambda s: ob.submit_web_code(s, code))


@router.post("/{token}/manual-api")
async def manual_api(request: Request, token: str, api_id: str = Form(""), api_hash: str = Form(""),
                     user: str = Depends(require_login)):
    return await _action(request, token, lambda s: ob.submit_manual_api(s, api_id, api_hash))


@router.post("/{token}/login-code")
async def login_code(request: Request, token: str, code: str = Form(""), user: str = Depends(require_login)):
    return await _action(request, token, lambda s: ob.submit_login_code(s, code))


@router.post("/{token}/password")
async def password(request: Request, token: str, password: str = Form(""), user: str = Depends(require_login)):
    return await _action(request, token, lambda s: ob.submit_password(s, password))


@router.post("/{token}/cancel")
async def cancel(token: str, user: str = Depends(require_login)):
    await ob.cancel(token)
    return RedirectResponse("/accounts?msg=Добавление+отменено", status_code=303)
