"""Создание собственного Telegram-приложения (api_id / api_hash) для аккаунта менеджера
через my.telegram.org в headless-браузере Playwright.

Ход: телефон → my.telegram.org присылает код В TELEGRAM менеджера (не SMS) → менеджер
вводит его в панели → сервер входит на сайт, открывает /apps и создаёт приложение (или
берёт уже существующее) → из страницы читаются api_id и api_hash.

Весь трафик браузера идёт через прокси аккаунта. SOCKS5 с логином/паролем Chromium не
поддерживает, поэтому поднимается локальный ретранслятор (local_socks_relay).

Playwright — синхронный API в ОДНОМ выделенном потоке (объекты Playwright привязаны к
потоку, создавшему их); из async-кода вызывать через asyncio.to_thread(creator.call, ...).

CAPTCHA и защиту от ботов не обходим: если сайт отказал — метод бросает CreatorError с
текстом, а менеджер/админ видит скриншот и может ввести api_id/api_hash вручную."""
import queue
import random
import re
import string
import threading
from urllib.parse import unquote, urlparse

from .local_socks_relay import LocalSocksRelay

AUTH_URL = "https://my.telegram.org/auth"
APPS_URL = "https://my.telegram.org/apps"

_ID_RE = re.compile(r"api_id\s*:?\s*(\d{4,10})", re.I)
_HASH_RE = re.compile(r"api_hash\s*:?\s*([0-9a-f]{32})", re.I)
# в разметке значения лежат в соседних элементах: <label>App api_id:</label> ... <span>123</span>
# или в value="..." — берём значение из текстового узла/атрибута, а не «любое число рядом»
_ID_HTML_RE = re.compile(r"api_id.{0,400}?(?:>\s*|value=[\"'])(\d{4,10})\s*(?:<|[\"'])", re.I | re.S)
_HASH_HTML_RE = re.compile(r"api_hash.{0,400}?(?:>\s*|value=[\"'])([0-9a-f]{32})\s*(?:<|[\"'])", re.I | re.S)


class CreatorError(RuntimeError):
    pass


def parse_credentials(*texts: str) -> tuple[int, str] | None:
    """Достаёт (api_id, api_hash) из текста и/или HTML страницы. None, если приложения нет."""
    for text in texts:
        if not text:
            continue
        m_id = _ID_RE.search(text) or _ID_HTML_RE.search(text)
        m_hash = _HASH_RE.search(text) or _HASH_HTML_RE.search(text)
        if m_id and m_hash:
            return int(m_id.group(1)), m_hash.group(1).lower()
    return None


def _short_name() -> str:
    # 5–32 символов, латиница/цифры, начинается с буквы
    return random.choice(string.ascii_lowercase) + "".join(random.choices(string.ascii_lowercase + string.digits, k=9))


class ApiAppCreator:
    def __init__(self, phone: str, upstream, upstream_url: str, headless: bool = True):
        """upstream — кортеж PySocks (как из proxy.parse_proxy); upstream_url — та же строка
        прокси в виде scheme://user:pass@host:port (для http-прокси Chromium)."""
        self.phone = phone
        self._upstream = upstream
        self._upstream_url = upstream_url
        self._headless = headless
        self._cmds: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name="api-app-creator", daemon=True)
        self._relay: LocalSocksRelay | None = None
        self._pw = self._browser = self._context = self._page = None
        self._thread.start()

    # ---------- публичный блокирующий интерфейс ----------
    def call(self, cmd: str, *args, timeout: float = 120.0):
        reply: queue.Queue = queue.Queue(maxsize=1)
        self._cmds.put((cmd, args, reply))
        try:
            ok, value = reply.get(timeout=timeout)
        except queue.Empty:
            raise CreatorError("Сайт my.telegram.org не ответил вовремя")
        if not ok:
            raise value
        return value

    # ---------- поток Playwright ----------
    def _loop(self) -> None:
        while True:
            cmd, args, reply = self._cmds.get()
            try:
                result = getattr(self, "_cmd_" + cmd)(*args)
                reply.put((True, result))
            except BaseException as exc:  # noqa: BLE001 — любую ошибку отдаём вызывающему
                reply.put((False, exc if isinstance(exc, CreatorError) else CreatorError(f"{type(exc).__name__}: {exc}")))
            if cmd == "close":
                return

    def _proxy_for_chromium(self) -> dict:
        parsed = urlparse(self._upstream_url)
        if parsed.scheme.startswith("socks"):
            self._relay = LocalSocksRelay(self._upstream)
            return {"server": self._relay.url}
        proxy = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
        if parsed.username:
            proxy["username"] = unquote(parsed.username)
            proxy["password"] = unquote(parsed.password or "")
        return proxy

    def _cmd_start(self) -> str:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise CreatorError(
                "Не установлен Playwright: pip install playwright && playwright install chromium"
            ) from exc

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self._headless, proxy=self._proxy_for_chromium())
        major = self._browser.version.split(".")[0]
        self._context = self._browser.new_context(
            user_agent=(f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"),
            locale="en-US",
            viewport={"width": 1100, "height": 760},
        )
        self._page = self._context.new_page()
        self._page.set_default_timeout(45000)
        self._page.goto(AUTH_URL)
        self._page.fill("#my_login_phone", self.phone)
        self._page.click("button:has-text('Next')")
        self._wait_for_code_field_or_error()
        return "await_web_code"

    def _wait_for_code_field_or_error(self) -> None:
        page = self._page
        try:
            page.wait_for_selector("#my_password", state="visible", timeout=30000)
        except Exception:
            self._raise_page_error("Сайт не запросил код подтверждения")

    def _raise_page_error(self, default: str) -> None:
        try:
            body = self._page.inner_text("body")
        except Exception:
            body = ""
        low = body.lower()
        if "too many tries" in low or "try again later" in low:
            raise CreatorError("my.telegram.org: слишком много попыток входа — подождите и повторите позже")
        if "captcha" in low or "robot" in low:
            raise CreatorError("my.telegram.org просит проверку (CAPTCHA) — автоматически не проходим. "
                               "Создайте приложение вручную и введите api_id/api_hash ниже")
        raise CreatorError(default)

    def _cmd_submit_code(self, code: str) -> dict:
        page = self._page
        if page is None:
            raise CreatorError("Сессия браузера не запущена")
        page.fill("#my_password", code.strip())
        page.click("button:has-text('Sign In')")
        try:
            # вошли, когда страница ушла с /auth (сайт перенаправляет в кабинет)
            page.wait_for_function("!location.pathname.startsWith('/auth')", timeout=20000)
        except Exception:
            self._raise_page_error("Код не принят или сайт не пустил в кабинет")
        page.goto(APPS_URL)
        page.wait_for_load_state("domcontentloaded")

        creds = self._read_credentials()
        if creds is None:
            self._create_app()
            creds = self._read_credentials()
        if creds is None:
            self._raise_page_error("Не удалось прочитать api_id/api_hash со страницы приложения")
        api_id, api_hash = creds
        return {"api_id": api_id, "api_hash": api_hash}

    def _read_credentials(self):
        page = self._page
        try:
            text = page.inner_text("body")
        except Exception:
            text = ""
        return parse_credentials(text, page.content())

    def _create_app(self) -> None:
        page = self._page
        try:
            page.wait_for_selector("#app_title", timeout=15000)
        except Exception:
            self._raise_page_error("На странице /apps нет формы создания приложения")
        page.fill("#app_title", "Desk Assistant")
        page.fill("#app_shortname", _short_name())
        for sel in ("input[name='app_platform'][value='desktop']", "#app_platform_desktop"):
            try:
                page.check(sel, timeout=2000)
                break
            except Exception:
                continue
        try:
            page.fill("#app_desc", "Customer support assistant", timeout=2000)
        except Exception:
            pass
        page.click("button:has-text('Create application')")
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(2500)

    def _cmd_screenshot(self):
        if self._page is None:
            return None
        return self._page.screenshot(type="png")

    def _cmd_close(self) -> None:
        for obj in (self._context, self._browser):
            try:
                if obj:
                    obj.close()
            except Exception:
                pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        if self._relay:
            self._relay.close()
        self._page = self._context = self._browser = self._pw = self._relay = None

    def close(self) -> None:
        if self._thread.is_alive():
            try:
                self.call("close", timeout=20)
            except CreatorError:
                pass
