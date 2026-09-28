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
import time
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


# Файл браузера может существовать и при этом не запускаться: например, сайт работает как
# служба Windows (Планировщик заданий, учётка NT AUTHORITY\SYSTEM) без рабочего стола —
# Chromium (даже headless) там иногда не может создать служебное окно и падает с ошибкой вида
# "Failed to register the window class for a message-only window" / "run out of resources".
# playwright_available() этого не ловит (он не запускает браузер, только проверяет файл), а
# каждая попытка запуска — это реальный процесс chrome-headless-shell.exe, который стоит
# ресурсов даже при мгновенном падении. Чтобы менеджер, кликающий повторно, не плодил такие
# попытки одну за другой, неудачный ЗАПУСК запоминается на время — дальше сразу отдаём ручной
# ввод api_id с той же причиной, без нового процесса.
_LAUNCH_FAILURE_COOLDOWN = 300  # 5 минут
_launch_failure: dict = {}


def record_launch_failure(message: str) -> None:
    _launch_failure["until"] = time.monotonic() + _LAUNCH_FAILURE_COOLDOWN
    _launch_failure["message"] = message


def clear_launch_failure() -> None:
    _launch_failure.clear()


def recent_launch_failure() -> str | None:
    """Текст недавней ошибки запуска, если она была меньше _LAUNCH_FAILURE_COOLDOWN назад."""
    if _launch_failure.get("until", 0) > time.monotonic():
        return _launch_failure.get("message")
    return None


_WINDOWS_SERVICE_HINTS = (
    "message-only window", "message_window.cc", "run out of resources",
    "0x36b7", "sxs_key_not_found",
)


def describe_launch_error(exc: BaseException) -> str:
    """Текст ошибки запуска Chromium: отличает «сайт работает как служба Windows без
    рабочего стола» (частая причина именно этого падения) от прочих случаев."""
    text = f"{type(exc).__name__}: {exc}"
    if any(h in text.lower() for h in _WINDOWS_SERVICE_HINTS):
        return (
            "Chromium не может создать своё окно — это типично, когда сайт работает как служба "
            "Windows (Планировщик заданий, учётка NT AUTHORITY\\SYSTEM) без рабочего стола, а не "
            "проблема с самим Chromium. Варианты: (1) выбрать существующий пул api_id или ввести "
            "api_id/api_hash вручную (создайте приложение на my.telegram.org с прокси этого "
            "аккаунта) — работает всегда, независимо от режима запуска сайта; (2) запускать веб-"
            "панель не от SYSTEM, а от обычной учётной записи с активным рабочим столом (в "
            "Планировщике заданий — тип входа «Обычный», не «Служба», и активная RDP-сессия "
            "этого пользователя на сервере)."
        )
    return (
        f"Не удалось запустить Chromium ({text}). Выполните на сервере: "
        f"venv\\Scripts\\python.exe scripts\\install_browser_deps.py"
    )


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


# Проверка выше запускает драйвер Playwright (~1 с на обычной машине, заметно дольше на
# нагруженном VPS) — делать это при КАЖДОМ открытии страницы «Добавить аккаунт» значит тормозить
# панель, которая и так один процесс. Страницы и мастер берут результат отсюда; скрипты установки и
# тесты вызывают playwright_available() напрямую и всегда получают свежий ответ.
_AVAILABILITY_TTL = 30
_availability_cache: dict = {}


def playwright_available_cached() -> tuple[bool, str]:
    key = str(browsers_path())
    hit = _availability_cache.get(key)
    now = time.monotonic()
    if hit and now - hit[0] < _AVAILABILITY_TTL:
        return hit[1]
    result = playwright_available()
    _availability_cache.clear()
    _availability_cache[key] = (now, result)
    return result
