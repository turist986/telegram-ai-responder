from fastapi.templating import Jinja2Templates

from .config import BASE_DIR
from .version import get_version

templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))
templates.env.globals["app_version"] = get_version()
