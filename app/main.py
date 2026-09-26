import logging

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from .auth import NotAuthenticated
from .config import BASE_DIR
from .database import init_db
from .routers import accounts, api_credentials, auth_router, dashboard, logs, onboarding, settings_router, tdata_import

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

app = FastAPI(title="AI Auto-Responder Dashboard")

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")


@app.exception_handler(NotAuthenticated)
async def not_authenticated_handler(request, exc):
    return RedirectResponse("/login", status_code=303)


@app.on_event("startup")
async def on_startup():
    init_db()


app.include_router(auth_router.router)
app.include_router(dashboard.router)
app.include_router(onboarding.router)  # раньше accounts: /accounts/add не должен ловиться шаблоном /{id}
app.include_router(api_credentials.router)  # раньше accounts: /accounts/api/... не должен ловиться шаблоном /{id}
app.include_router(tdata_import.router)  # раньше accounts: /accounts/import-tdata не должен ловиться шаблоном /{id}
app.include_router(accounts.router)
app.include_router(settings_router.router)
app.include_router(logs.router)
