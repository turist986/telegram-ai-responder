"""Ставит Chromium для Playwright (мастер «Добавить аккаунт» → создание api_id/api_hash на
my.telegram.org) в ФИКСИРОВАННЫЙ путь внутри проекта, а не в профиль того, кто запустил
установку.

Почему это важно: сайт в проде обычно работает как фоновая задача Планировщика под
NT AUTHORITY\\SYSTEM (см. deploy/windows/install-services.ps1), а зависимости чаще ставят
интерактивно, под другой учётной записью (например Administrator) — у каждой учётной записи
Windows свой профиль (C:\\Users\\<имя>\\AppData\\Local), и браузер, поставленный в один
профиль, для другой учётной записи попросту не существует: "Executable doesn't exist".
Фиксированный путь внутри проекта (data/ms-playwright) от учётной записи не зависит — ставит
одна, запускает другая, работает одинаково. Подробности и переопределение через .env
(PLAYWRIGHT_BROWSERS_PATH=...) — app/services/browser_deps.py.

Запуск:  python scripts/install_browser_deps.py
(внутри активированного venv, либо: venv\\Scripts\\python.exe scripts\\install_browser_deps.py)"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.browser_deps import ensure_env_set, playwright_available  # noqa: E402


def main() -> int:
    path = ensure_env_set()
    print(f"[*] Папка браузеров Playwright: {path}")
    print("[1/1] Chromium для Playwright (мастер добавления аккаунта, my.telegram.org)...")
    code = subprocess.call([sys.executable, "-m", "playwright", "install", "chromium"])
    if code:
        print("[!] Не удалось установить Chromium (см. сообщения выше).")
        return code

    ok, hint = playwright_available()
    print("OK: мастер «своё приложение через браузер» доступен" if ok else f"НЕ РАБОТАЕТ: {hint}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
