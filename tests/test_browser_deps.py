"""services/browser_deps.py: путь к браузерам Playwright должен быть один и тот же независимо
от того, кто его ставил и кто потом запускает процесс — именно рассинхронизация этого пути
(браузер стоит в профиле Administrator, сайт запущен как служба под SYSTEM) роняла мастер
добавления аккаунта с ошибкой при создании api_id.

RealChromiumTests использует НАСТОЯЩИЙ Chromium и пропускается, если его нет — запустите с
переменной PLAYWRIGHT_BROWSERS_PATH, указывающей на уже установленный Chromium (как и для
tests/test_driver_playwright.py), иначе эта часть просто пропустится."""
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import browser_deps as bd

# То же, что уже стояло в окружении на момент импорта app.config (он вызывает
# ensure_env_set(), который идемпотентно переписывает переменную тем же значением) —
# т.е. именно то, что передал в окружение тот, кто запускает тесты.
REAL_CHROMIUM_DIR = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")


def _has_real_chromium() -> bool:
    if not REAL_CHROMIUM_DIR:
        return False
    ok, _ = bd.playwright_available()
    return ok


HAVE_CHROMIUM = _has_real_chromium()


class PathResolutionTests(unittest.TestCase):
    """Без реального Chromium — только логика выбора пути (real env var > .env > дефолт)."""

    def setUp(self):
        self._env_patch = patch.dict(os.environ, {}, clear=False)
        self._env_patch.start()
        os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
        self.addCleanup(self._env_patch.stop)

    def test_default_is_inside_project_not_a_user_profile(self):
        path = bd.browsers_path()
        self.assertEqual(path, bd._PROJECT_ROOT / "data" / "ms-playwright")
        # именно НЕ завязан на конкретного пользователя Windows — в этом весь смысл фикса
        self.assertNotIn("AppData", str(path))

    def test_real_env_var_overrides_default(self):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = r"C:\Users\Administrator\AppData\Local\ms-playwright"
        self.assertEqual(str(bd.browsers_path()), r"C:\Users\Administrator\AppData\Local\ms-playwright")

    def test_env_file_value_used_when_no_real_env_var(self):
        with patch.object(bd, "_env_file_value", lambda: r"D:\shared\ms-playwright"):
            self.assertEqual(str(bd.browsers_path()), r"D:\shared\ms-playwright")

    def test_real_env_var_wins_over_env_file(self):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = r"C:\from\real\env"
        with patch.object(bd, "_env_file_value", lambda: r"D:\from\dotenv"):
            self.assertEqual(str(bd.browsers_path()), r"C:\from\real\env")

    def test_ensure_env_set_writes_to_os_environ(self):
        bd.ensure_env_set()
        self.assertEqual(os.environ["PLAYWRIGHT_BROWSERS_PATH"], str(bd._PROJECT_ROOT / "data" / "ms-playwright"))

    def test_env_var_does_not_break_settings_startup(self):
        # регрессия: pydantic-settings по умолчанию запрещает незнакомые ключи в .env
        # (extra="forbid") — переменная из этого файла должна быть объявлена в Settings,
        # иначе весь сайт падает при старте, стоит только прописать override в .env
        from app.config import Settings

        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = r"C:\custom\ms-playwright"
        Settings()  # не должно бросить pydantic_core.ValidationError

    def test_dotenv_parsing_ignores_comments_and_strips_quotes(self):
        text = '# комментарий\nTELEGRAM_API_ID=1\nPLAYWRIGHT_BROWSERS_PATH="C:\\quoted\\path"\n'
        with patch("pathlib.Path.is_file", return_value=True), \
                patch("pathlib.Path.read_text", return_value=text):
            self.assertEqual(bd._env_file_value(), r"C:\quoted\path")


try:
    import playwright.sync_api  # noqa: F401
    HAVE_PLAYWRIGHT_PKG = True
except ImportError:
    HAVE_PLAYWRIGHT_PKG = False


@unittest.skipUnless(HAVE_PLAYWRIGHT_PKG, "пакет playwright не установлен")
class AsyncioLoopTests(unittest.IsolatedAsyncioTestCase):
    """Регрессия, пойманная только живым тестом через настоящий HTTP-сервер: Playwright Sync
    API прямо запрещает вызывать себя из потока с работающим asyncio event loop ("Please use
    the Async API instead") — при вызове playwright_available() напрямую из async-обработчика
    FastAPI (внутри event loop uvicorn) страница «Добавить аккаунт» вместо понятной причины
    показывала эту техническую ошибку. Не требует настоящего Chromium — проверка происходит до
    поиска исполняемого файла, достаточно самого пакета playwright и работающего event loop."""

    async def test_playwright_available_works_when_awaited_via_to_thread(self):
        # так и вызывается в проде (app/services/onboarding.py, app/routers/onboarding.py) —
        # именно эта обёртка и чинит конфликт с event loop
        import asyncio

        ok, hint = await asyncio.to_thread(bd.playwright_available)
        self.assertNotIn("asyncio", hint.lower())

    async def test_calling_it_directly_inside_the_loop_reproduces_the_original_bug(self):
        # доказывает, что обёртка в to_thread выше не случайна: без неё внутри event loop
        # Playwright действительно отказывается работать — так был найден баг
        ok, hint = bd.playwright_available()
        self.assertFalse(ok)
        self.assertIn("asyncio", hint.lower())


@unittest.skipUnless(HAVE_CHROMIUM, "нужен настоящий Chromium (запустите с PLAYWRIGHT_BROWSERS_PATH=...)")
class RealChromiumTests(unittest.TestCase):
    """Единственный способ по-настоящему проверить фикс: заставить Playwright искать браузер
    по РАЗНЫМ путям и убедиться, что находит только при совпадении — это в точности повторяет
    то, что происходило между учётной записью, ставившей браузер, и учётной записью (SYSTEM),
    под которой сайт его потом искал."""

    def setUp(self):
        self._env_patch = patch.dict(os.environ, {}, clear=False)
        self._env_patch.start()
        self.addCleanup(self._env_patch.stop)

    def test_available_when_path_matches_where_it_was_installed(self):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = REAL_CHROMIUM_DIR
        ok, hint = bd.playwright_available()
        self.assertTrue(ok, hint)
        self.assertEqual(hint, "")

    def test_unavailable_when_path_does_not_match(self):
        # ровно тот баг из жалобы: браузер стоит в одном месте (Administrator), процесс ищет
        # его в другом (профиль SYSTEM) — Playwright не находит исполняемый файл
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(Path(REAL_CHROMIUM_DIR).parent / "nowhere-near-here")
        ok, hint = bd.playwright_available()
        self.assertFalse(ok)
        self.assertIn("scripts\\install_browser_deps.py", hint)

    def test_ensure_env_set_makes_playwright_itself_see_the_same_path(self):
        # не только НАША проверка — сам playwright.sync_api должен резолвить в тот же файл
        from playwright.sync_api import sync_playwright

        with patch.object(bd, "_DEFAULT", Path(REAL_CHROMIUM_DIR)):
            os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
            bd.ensure_env_set()
        with sync_playwright() as p:
            self.assertTrue(Path(p.chromium.executable_path).is_file())


if __name__ == "__main__":
    unittest.main()
