"""Единая точка правды о том, ГДЕ Playwright хранит браузеры — используется и приложением
(app/config.py при старте), и автономным scripts/install_browser_deps.py.

Почему это отдельно от app.config.Settings: этот модуль должен работать ещё ДО того, как
.env гарантированно заполнен (Settings() иначе упадёт на отсутствующих TELEGRAM_API_ID и
т.п.) — сюда обращается в том числе install_browser_deps.py на самом первом запуске.

Суть проблемы, которую это решает: по умолчанию Playwright ставит браузеры в профиль ТОЙ
учётной записи Windows, из-под которой запущен `playwright install` — обычно это
C:\\Users\\<кто ставил>\\AppData\\Local\\ms-playwright. Сайт же в проде обычно работает как
фоновая задача Планировщика под NT AUTHORITY\\SYSTEM (см. deploy/windows/install-services.ps1),
у которой СВОЙ, отдельный профиль — браузер, поставленный интерактивно под Administrator,
для SYSTEM просто не существует ("Executable doesn't exist"), и мастер добавления аккаунта
(создание api_id/api_hash через my.telegram.org) падает с ошибкой. Фиксированный путь ВНУТРИ
проекта не зависит от того, какая учётная запись ставит зависимости и какая потом запускает
сайт — и переезжает вместе с проектом при переносе на другую машину."""
import os
from pathlib import Path

# .../app/services/browser_deps.py -> .../  (корень проекта). Намеренно не через app.config —
# см. пояснение выше.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_ENV_FILE = _PROJECT_ROOT / ".env"
_VAR = "PLAYWRIGHT_BROWSERS_PATH"
_DEFAULT = _PROJECT_ROOT / "data" / "ms-playwright"


def _env_file_value() -> str | None:
    if not _ENV_FILE.is_file():
        return None
    try:
        text = _ENV_FILE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == _VAR:
            return value.strip().strip('"').strip("'") or None
    return None


def browsers_path() -> Path:
    """Путь, где ДОЛЖНЫ лежать браузеры: реальная переменная окружения важнее .env (совпадает
    с тем, как читает настройки app.config.Settings), иначе — .env, иначе — путь внутри проекта."""
    override = os.environ.get(_VAR) or _env_file_value()
    return Path(override) if override else _DEFAULT


def ensure_env_set() -> Path:
    """Выставляет PLAYWRIGHT_BROWSERS_PATH в os.environ, чтобы сам Playwright (он читает
    только реальную переменную окружения, не .env) увидел тот же путь, что и остальной код.
    Идемпотентно; безопасно звать несколько раз. Возвращает выставленный путь."""
    path = browsers_path()
    os.environ[_VAR] = str(path)
    return path


def playwright_available() -> tuple[bool, str]:
    """(готов ли мастер «своё приложение через браузер», подсказка если нет). Не запускает
    сам браузер — только проверяет наличие исполняемого файла по расчётному пути, поэтому
    быстро и безопасно вызывать при каждом показе страницы добавления аккаунта."""
    ensure_env_set()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False, ("не установлен пакет playwright. Выполните: "
                       "venv\\Scripts\\python.exe scripts\\install_browser_deps.py")
    try:
        with sync_playwright() as pw:
            exe = Path(pw.chromium.executable_path)
    except Exception as exc:  # noqa: BLE001 — сюда же попадёт что угодно нештатное от драйвера Playwright
        return False, f"Playwright не запускается ({type(exc).__name__}: {exc})"
    if not exe.is_file():
        return False, (f"Chromium для Playwright не найден по пути {exe}. Выполните: "
                       f"venv\\Scripts\\python.exe scripts\\install_browser_deps.py")
    return True, ""
