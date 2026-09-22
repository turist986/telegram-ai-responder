from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..auth import require_login
from ..config import settings
from ..database import get_db
from ..services.knowledge_base import load_knowledge_base, load_prompt_template, save_text_file
from ..services.llm_providers import PROVIDERS
from ..services.schedule import ScheduleConfigError
from ..services.settings_store import (
    CHECKING_PRESETS,
    ProtectionConfigError,
    get_protection,
    set_protection,
    get_llm_settings,
    get_schedule_settings,
    is_global_enabled,
    is_kb_enabled,
    is_llm_key_set_in_panel,
    is_prompt_enabled,
    set_llm_api_key,
    set_llm_settings,
    set_schedule_settings,
    set_setting,
)
from ..templating import templates

router = APIRouter(prefix="/settings")


@router.get("", response_class=HTMLResponse)
async def settings_page(request: Request, user: str = Depends(require_login), db: Session = Depends(get_db)):
    llm = get_llm_settings(db)
    schedule = get_schedule_settings(db)
    providers = []
    for key, cfg in PROVIDERS.items():
        panel_key = is_llm_key_set_in_panel(db, key)
        env_key = bool(getattr(settings, cfg["key_field"]))
        providers.append({
            "key": key,
            "label": cfg["label"],
            "default_model": cfg["model"],
            "env_var": cfg["env_var"],
            "panel_key_configured": panel_key,
            "env_key_configured": env_key,
            "key_configured": panel_key or env_key,
        })
    context = {
        "request": request,
        "kb_text": load_knowledge_base(settings.knowledge_base_path),
        "prompt_text": load_prompt_template(settings.prompt_template_path),
        "global_enabled": is_global_enabled(db),
        "prompt_enabled": is_prompt_enabled(db),
        "kb_enabled": is_kb_enabled(db),
        "default_manager_name": settings.default_manager_name,
        "default_disclaimer_template": settings.default_disclaimer_template,
        "default_fallback_disclaimer": settings.default_fallback_disclaimer,
        "providers": providers,
        "llm_provider": llm["provider"],
        "llm_model": llm["model"] or "",
        "llm_base_url": llm["base_url"] or "",
        "schedule": schedule,
        "prot": get_protection(db),
        "checking_presets": CHECKING_PRESETS,
        "message": request.query_params.get("msg"),
    }
    return templates.TemplateResponse("settings.html", context)


@router.post("/llm")
async def save_llm(
    provider: str = Form(...),
    model: str = Form(""),
    base_url: str = Form(""),
    user: str = Depends(require_login),
    db: Session = Depends(get_db),
):
    set_llm_settings(db, provider, model, base_url)
    return RedirectResponse("/settings", status_code=303)


@router.post("/llm-key")
async def save_llm_key(
    provider: str = Form(...),
    api_key: str = Form(""),
    user: str = Depends(require_login),
    db: Session = Depends(get_db),
):
    api_key = api_key.strip()
    if not api_key:
        return RedirectResponse("/settings?msg=Вставьте+ключ+перед+сохранением", status_code=303)
    try:
        set_llm_api_key(db, provider, api_key)
    except ValueError as exc:
        return RedirectResponse(f"/settings?msg={exc}", status_code=303)
    return RedirectResponse("/settings?msg=Ключ+сохранён", status_code=303)


@router.post("/llm-key/clear")
async def clear_llm_key(
    provider: str = Form(...),
    user: str = Depends(require_login),
    db: Session = Depends(get_db),
):
    try:
        set_llm_api_key(db, provider, "")
    except ValueError as exc:
        return RedirectResponse(f"/settings?msg={exc}", status_code=303)
    return RedirectResponse(
        "/settings?msg=Ключ+удалён+из+панели+(если+задан+в+.env,+будет+использован+он)", status_code=303
    )


@router.post("/knowledge-base")
async def save_kb(
    content: str = Form(...),
    enabled: str = Form(""),
    user: str = Depends(require_login),
    db: Session = Depends(get_db),
):
    save_text_file(settings.knowledge_base_path, content)
    set_setting(db, "kb_enabled", "true" if enabled else "false")
    return RedirectResponse("/settings", status_code=303)


@router.post("/prompt-template")
async def save_prompt(
    content: str = Form(...),
    enabled: str = Form(""),
    user: str = Depends(require_login),
    db: Session = Depends(get_db),
):
    save_text_file(settings.prompt_template_path, content)
    set_setting(db, "prompt_enabled", "true" if enabled else "false")
    return RedirectResponse("/settings", status_code=303)


@router.post("/schedule")
async def save_schedule(
    work_hours_enabled: str = Form(""),
    work_start: list[str] = Form([]),
    work_end: list[str] = Form([]),
    break_enabled: str = Form(""),
    break_start: list[str] = Form([]),
    break_end: list[str] = Form([]),
    keep_active_dialog_enabled: str = Form(""),
    keep_active_dialog_minutes: str = Form("30"),
    user: str = Depends(require_login),
    db: Session = Depends(get_db),
):
    try:
        set_schedule_settings(
            db,
            work_hours_enabled=bool(work_hours_enabled),
            work_windows=list(zip(work_start, work_end)),
            break_enabled=bool(break_enabled),
            break_windows=list(zip(break_start, break_end)),
            keep_active_dialog_enabled=bool(keep_active_dialog_enabled),
            keep_active_dialog_minutes=keep_active_dialog_minutes,
        )
    except ScheduleConfigError as exc:
        return RedirectResponse(f"/settings?msg={exc}", status_code=303)
    return RedirectResponse("/settings", status_code=303)


@router.post("/protection")
async def save_protection(request: Request, user: str = Depends(require_login), db: Session = Depends(get_db)):
    form = dict((await request.form()).items())
    try:
        set_protection(db, form)
    except ProtectionConfigError as exc:
        return RedirectResponse(f"/settings?msg={exc}#protection", status_code=303)
    return RedirectResponse("/settings?msg=Настройки+защиты+сохранены#protection", status_code=303)


@router.post("/global-toggle")
async def global_toggle(user: str = Depends(require_login), db: Session = Depends(get_db)):
    current = is_global_enabled(db)
    set_setting(db, "global_enabled", "false" if current else "true")
    return RedirectResponse("/settings", status_code=303)
