"""Проверка драйвера Playwright на ЛОКАЛЬНОЙ странице-имитации my.telegram.org
(настоящий сайт не трогаем). Пропускается, если Playwright или Chromium не установлены."""
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from app.services import api_app_creator as mod

try:
    import playwright.sync_api  # noqa: F401
    HAVE_PW = True
except ImportError:
    HAVE_PW = False

AUTH = """<html><body>
<input type="text" id="my_login_phone"><button type="submit" onclick="
  document.getElementById('code').style.display='block'">Next</button>
<div id="code" style="display:none">
  <input type="text" id="my_password" placeholder="Confirmation code">
  <button type="submit" onclick="if(document.getElementById('my_password').value==='12345'){location.href='/apps'}else{document.body.append('Invalid code')}">Sign In</button>
</div></body></html>"""

FORM = """<html><body><h1>Create new application</h1>
<form method="post" action="/apps/create">
<input id="app_title" name="t"><input id="app_shortname" name="s">
<input type="radio" name="app_platform" value="desktop"><textarea id="app_desc"></textarea>
<button type="submit">Create application</button></form></body></html>"""

DONE = """<html><body><div class="form-group"><label>App api_id:</label><div><span class="uneditable-input">2468013</span></div>
<label>App api_hash:</label><div><span class="uneditable-input">00112233445566778899aabbccddeeff</span></div></div></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    created = False

    def log_message(self, *a):
        pass

    def _send(self, body, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_GET(self):
        if self.path.startswith("/auth"):
            self._send(AUTH)
        elif self.path.startswith("/apps"):
            self._send(DONE if _Handler.created else FORM)
        else:
            self._send("nope", 404)

    def do_POST(self):
        _Handler.created = True
        self.send_response(303)
        self.send_header("Location", "/apps")
        self.end_headers()


@unittest.skipUnless(HAVE_PW, "playwright не установлен")
class DriverTests(unittest.TestCase):
    def test_full_browser_flow_on_mock_site(self):
        srv = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        _Handler.created = False
        old = (mod.AUTH_URL, mod.APPS_URL)
        mod.AUTH_URL, mod.APPS_URL = base + "/auth", base + "/apps"
        creator = mod.ApiAppCreator("+12223334455", None, "socks5://127.0.0.1:9", headless=True)
        try:
            try:
                self.assertEqual(creator.call("start", timeout=90), "await_web_code")
            except mod.CreatorError as exc:
                if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
                    self.skipTest("Chromium не установлен")
                raise
            self.assertTrue(creator.call("screenshot", timeout=30).startswith(b"\x89PNG"))
            creds = creator.call("submit_code", "12345", timeout=90)
            self.assertEqual(creds, {"api_id": 2468013, "api_hash": "00112233445566778899aabbccddeeff"})
        finally:
            creator.close()
            srv.shutdown()
            mod.AUTH_URL, mod.APPS_URL = old


if __name__ == "__main__":
    unittest.main()
