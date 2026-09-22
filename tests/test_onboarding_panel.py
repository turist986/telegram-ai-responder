import unittest
from types import SimpleNamespace
from urllib.parse import unquote_plus
from unittest.mock import patch

from fastapi.testclient import TestClient
from telethon.errors import PhoneCodeInvalidError, SessionPasswordNeededError

from app.auth import require_login
from app.config import settings
from app.database import SessionLocal, init_db
from app.main import app
from app.models import Account
from app.services import onboarding as ob
from app.services.api_app_creator import CreatorError

PROXY = "socks5://user:pw@10.1.1.1:1080"


class FakeCreator:
    fail_start = False
    fail_code = False

    def __init__(self, phone, upstream, url, headless=True):
        self.closed = False

    def call(self, cmd, *args, timeout=0):
        if cmd == "start":
            if FakeCreator.fail_start:
                raise CreatorError("CAPTCHA")
            return "await_web_code"
        if cmd == "submit_code":
            if FakeCreator.fail_code:
                raise CreatorError("код не принят")
            return {"api_id": 1234567, "api_hash": "a" * 32}
        if cmd == "screenshot":
            return b"\x89PNG"
        return None

    def close(self):
        self.closed = True


class FakeClient:
    instances = []
    needs_password = False
    bad_code = False

    def __init__(self, session, api_id, api_hash, **kw):
        self.session_path, self.api_id, self.api_hash, self.kw = session, api_id, api_hash, kw
        self.session = SimpleNamespace(dc_id=2, set_dc=lambda *a: None)
        self.disconnected = False
        FakeClient.instances.append(self)

    async def connect(self):
        # обязательно: вход идёт через прокси
        assert self.kw["proxy"] is not None

    async def send_code_request(self, phone):
        return SimpleNamespace(phone_code_hash="hash123")

    async def sign_in(self, phone=None, code=None, phone_code_hash=None, password=None):
        if password is None:
            if FakeClient.bad_code:
                raise PhoneCodeInvalidError(request=None)
            if FakeClient.needs_password:
                raise SessionPasswordNeededError(request=None)
        with open(self.session_path, "wb") as f:  # «сессия» появляется на диске после входа
            f.write(b"sess")

    async def get_me(self):
        return SimpleNamespace(phone="12223334455")

    async def disconnect(self):
        self.disconnected = True


def _patches():
    return (
        patch.object(ob, "ApiAppCreator", FakeCreator),
        patch.object(ob, "TelegramClient", FakeClient),
        patch.object(ob, "test_proxy", lambda p: (True, "ok")),
        patch.object(ob, "choose_ip_family", lambda proxy, dc: (False, "149.154.167.51")),
    )


class OnboardingFlowTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        settings.sessions_dir.mkdir(parents=True, exist_ok=True)

    def setUp(self):
        FakeCreator.fail_start = FakeCreator.fail_code = False
        FakeClient.needs_password = FakeClient.bad_code = False
        with SessionLocal() as db:
            db.query(Account).delete()
            db.commit()

    async def _begin(self, db, ident="m1", proxy=PROXY):
        return await ob.begin(db, ident, "+1 (222) 333-44-55", proxy, "Иван", "ru")

    async def test_proxy_is_mandatory_and_unique(self):
        with SessionLocal() as db:
            with self.assertRaises(ob.OnboardingError):
                await ob.begin(db, "x", "+12223334455", "", "", "ru")
            with self.assertRaises(ob.OnboardingError):
                await ob.begin(db, "x", "+12223334455", "garbage", "", "ru")
            with self.assertRaises(ob.OnboardingError):
                await ob.begin(db, "x", "12", PROXY, "", "ru")  # плохой номер
            db.add(Account(identifier="other", proxy="socks5://a:b@10.1.1.1:1080"))
            db.commit()
            with self.assertRaises(ob.OnboardingError) as cm:
                await ob.begin(db, "x", "+12223334455", "socks5://10.1.1.1:1080", "", "ru")
            self.assertIn("other", str(cm.exception))

    async def test_dead_proxy_is_rejected_before_any_network_step(self):
        with SessionLocal() as db, patch.object(ob, "test_proxy", lambda p: (False, "не пропускает Telegram")), \
                patch.object(ob, "ApiAppCreator", FakeCreator):
            with self.assertRaises(ob.OnboardingError):
                await self._begin(db)

    async def test_full_flow_creates_isolated_account(self):
        p1, p2, p3, p4 = _patches()
        with p1, p2, p3, p4, SessionLocal() as db:
            s = await self._begin(db)
            self.assertEqual(s.step, "web_code")
            await ob.submit_web_code(s, "12345")
            self.assertEqual((s.step, s.api_id), ("login_code", 1234567))
            client = FakeClient.instances[-1]
            self.assertEqual(client.api_id, 1234567)             # логин под собственным api_id аккаунта
            self.assertIsNotNone(client.kw["proxy"])             # и через прокси
            self.assertEqual(client.kw["flood_sleep_threshold"], 0)
            self.assertEqual(client.kw["device_model"], s.profile["device_model"])
            await ob.submit_login_code(s, "77777")
            self.assertEqual(s.step, "done")
        with SessionLocal() as db:
            a = db.query(Account).filter_by(identifier="m1").one()
            self.assertEqual((a.api_id, a.api_hash), (1234567, "a" * 32))
            self.assertEqual(a.proxy, PROXY)
            self.assertTrue(a.device_model and a.app_version and a.lang_code == "ru")
            self.assertFalse(a.enabled)                           # включается вручную после «остывания»
            self.assertEqual(a.phone, "+12223334455")
        self.assertTrue(client.disconnected)

    async def test_two_factor_and_wrong_code(self):
        p1, p2, p3, p4 = _patches()
        with p1, p2, p3, p4, SessionLocal() as db:
            FakeClient.needs_password = True
            s = await self._begin(db, "m2", "socks5://10.2.2.2:1080")
            await ob.submit_web_code(s, "1")
            await ob.submit_login_code(s, "2")
            self.assertEqual(s.step, "password")
            await ob.submit_password(s, "cloudpw")
            self.assertEqual(s.step, "done")
            FakeClient.needs_password, FakeClient.bad_code = False, True
            s2 = await self._begin(db, "m3", "socks5://10.3.3.3:1080")
            await ob.submit_web_code(s2, "1")
            with self.assertRaises(ob.OnboardingError):
                await ob.submit_login_code(s2, "0")
            self.assertEqual(s2.step, "login_code")               # можно ввести заново

    async def test_browser_failure_falls_back_to_manual_api(self):
        p1, p2, p3, p4 = _patches()
        with p1, p2, p3, p4, SessionLocal() as db:
            FakeCreator.fail_start = True
            s = await self._begin(db, "m4", "socks5://10.4.4.4:1080")
            self.assertEqual(s.step, "manual_api")
            with self.assertRaises(ob.OnboardingError):
                await ob.submit_manual_api(s, "abc", "zzz")
            await ob.submit_manual_api(s, "7654321", "b" * 32)
            self.assertEqual(s.step, "login_code")

    async def test_cancel_removes_unfinished_session(self):
        p1, p2, p3, p4 = _patches()
        with p1, p2, p3, p4, SessionLocal() as db:
            s = await self._begin(db, "m5", "socks5://10.5.5.5:1080")
            await ob.submit_web_code(s, "1")
            await ob.cancel(s.token)
            self.assertIsNone(ob.get_state(s.token))
            self.assertTrue(FakeClient.instances[-1].disconnected)
            self.assertFalse(s.session_path.exists())


class PanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.dependency_overrides[require_login] = lambda: "admin"
        cls.client = TestClient(app, follow_redirects=False)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        app.dependency_overrides.clear()

    def test_pages_render(self):
        with SessionLocal() as db:
            db.query(Account).delete()
            db.add(Account(identifier="legacy", proxy=None))
            db.commit()
        for url, needle in (("/accounts", "НЕТ ПРОКСИ"), ("/accounts/add", "Прокси (обязателен)"),
                            ("/settings", "Автостоп при FloodWait")):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertIn(needle, r.text, url)

    def test_settings_save_and_error(self):
        ok = self.client.post("/settings/protection", data={"checking_preset": "realistic", "reply_mode": "sticky"})
        self.assertEqual(ok.status_code, 303)
        self.assertIn("сохранены", unquote_plus(ok.headers["location"]))
        bad = self.client.post("/settings/protection", data={"checking_preset": "custom", "poll_interval_seconds": "1"})
        self.assertIn("допустимо", unquote_plus(bad.headers["location"]))

    def test_legacy_imports_blocked_and_proxy_cannot_be_removed(self):
        r = self.client.post("/accounts/upload-session", data={"identifier": "z"}, files={"session_file": ("z.session", b"x")})
        self.assertEqual(r.status_code, 303)
        self.assertIn("отключён", unquote_plus(r.headers["location"]))
        with SessionLocal() as db:
            acc = Account(identifier="hasproxy", proxy="socks5://10.7.7.7:1080")
            db.add(acc)
            db.commit()
            acc_id = acc.id
        r = self.client.post(f"/accounts/{acc_id}/proxy", data={"proxy": ""})
        self.assertEqual(r.status_code, 303)
        with SessionLocal() as db:
            self.assertEqual(db.get(Account, acc_id).proxy, "socks5://10.7.7.7:1080")  # не удалился
        with SessionLocal() as db:
            self.assertFalse(db.query(Account).filter_by(identifier="z").count())

    def test_add_form_rejects_missing_proxy(self):
        r = self.client.post("/accounts/add/start", data={"identifier": "n1", "phone": "+12223334455"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Прокси обязателен", r.text)


if __name__ == "__main__":
    unittest.main()
