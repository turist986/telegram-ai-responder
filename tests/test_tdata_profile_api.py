"""Импорт TData: профиль устройства и api_id сохраняются у аккаунта, 2FA по аккаунтам, пулы,
автосоздание приложения; удаление аккаунтов из панели и ручное задание профиля.
К Telegram, my.telegram.org и реальным TData не обращаемся — всё подменено заглушками."""
import datetime as dt
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.auth import require_login
from app.config import settings
from app.database import SessionLocal
from app.main import app
from app.models import Account, ApiCredential, ChatStatus, DialogMessage
from app.services import api_autocreate
from app.services import tdata_batch as tb
from app.services.device_profile import OFFICIAL_DESKTOP_API_ID

from tests.test_tdata_batch import P, Base, zip_bytes


class PasswordParsingTests(unittest.TestCase):
    IDS = ["1", "2", "3"]

    def test_identifier_colon_password_in_any_order(self):
        got = tb.parse_cloud_passwords("3:pw three\n1 : a:b:c\n", self.IDS)
        self.assertEqual(got, {"3": "pw three", "1": "a:b:c"})

    def test_positional_lines_and_dash_means_none(self):
        self.assertEqual(tb.parse_cloud_passwords("p1\n-\np3", self.IDS), {"1": "p1", "3": "p3"})

    def test_wrong_count_is_an_error_and_empty_is_empty(self):
        with self.assertRaises(tb.TDataBatchError):
            tb.parse_cloud_passwords("only one", self.IDS)
        self.assertEqual(tb.parse_cloud_passwords("  \n", self.IDS), {})


class WebCodeTests(unittest.TestCase):
    def test_parses_login_code_from_service_message(self):
        en = "Web login code. Dear Ivan, we received a request...\nThis is your login code:\nCTAsmfLKm8g\n\nDo not give"
        self.assertEqual(api_autocreate.parse_web_code(en), "CTAsmfLKm8g")
        self.assertEqual(api_autocreate.parse_web_code("Ваш код для входа:\nAbC123xyZ9"), "AbC123xyZ9")
        self.assertIsNone(api_autocreate.parse_web_code("Login code: 12345. Do not give"))  # обычный код входа — не наш


class ImportProfileTests(Base, unittest.IsolatedAsyncioTestCase):
    async def test_desktop_mode_saves_api_and_device_profile_and_per_account_2fa(self):
        job = self.prepare([self.archive("a.zip", "1/tdata/key_datas", "2/tdata/key_datas")],
                           proxies_text=f"{P[1]}\n{P[2]}", api_mode="desktop")
        await tb.run_job(job, cloud_password="COMMON", cloud_passwords={"2": "SECOND"})
        self.assertEqual([c["cloud"] for c in self.calls], ["COMMON", "SECOND"])
        self.assertTrue(all(c["api"] is None for c in self.calls))
        with SessionLocal() as db:
            for a in db.query(Account).all():
                self.assertEqual(a.api_id, OFFICIAL_DESKTOP_API_ID)
                self.assertEqual((a.device_model, a.app_version, a.lang_code), ("XPS L701X", "3.4.3 x64", "ru"))
                self.assertEqual(a.phone, "+100")

    async def test_pool_mode_logs_in_with_pool_api_and_own_profile(self):
        with SessionLocal() as db:
            db.query(ApiCredential).delete()
            db.add(ApiCredential(api_id=111111, api_hash="a" * 32))
            db.commit()
        self.addCleanup(self._drop_creds)
        job = self.prepare([self.archive("a.zip", "1/tdata/key_datas", "2/tdata/key_datas")],
                           proxies_text=f"{P[1]}\n{P[2]}", api_mode="pool")
        await tb.run_job(job)
        self.assertEqual({c["api"]["api_id"] for c in self.calls}, {111111})
        with SessionLocal() as db:
            accs = db.query(Account).all()
            self.assertTrue(all(a.api_id == 111111 and a.device_model for a in accs))

    def test_pool_mode_without_capacity_is_refused_before_telegram(self):
        self._drop_creds()
        with self.assertRaises(tb.TDataBatchError) as cm:
            self.prepare([self.archive("a.zip", "1/tdata/key_datas")], proxies_text=P[1], api_mode="pool")
        self.assertIn("свободно мест", str(cm.exception))

    async def test_auto_mode_creates_own_app_and_switches_profile(self):
        async def fake_create(session_path, proxy_str, current):
            self.assertEqual(current["api_id"], OFFICIAL_DESKTOP_API_ID)       # подключаемся тем же клиентом
            return {"api_id": 555555, "api_hash": "b" * 32, "phone": "+100"}

        with patch("app.services.tdata_batch.create_own_app", fake_create), \
                patch("app.services.browser_deps.playwright_available_cached", lambda: (True, "")):
            job = self.prepare([self.archive("a.zip", "1/tdata/key_datas")], proxies_text=P[1], api_mode="auto")
            await tb.run_job(job)
        self.assertEqual(job.items[0].status, "ok")
        self.assertIn("555555", job.items[0].message)
        with SessionLocal() as db:
            a = db.query(Account).one()
            self.assertEqual(a.api_id, 555555)
            self.assertNotEqual(a.app_version, "3.4.3 x64")                    # своё приложение — свой профиль

    async def test_auto_mode_failure_keeps_account_working_on_desktop_api(self):
        async def failing(*_a):
            raise api_autocreate.AutoCreateError("CAPTCHA")

        with patch("app.services.tdata_batch.create_own_app", failing), \
                patch("app.services.browser_deps.playwright_available_cached", lambda: (True, "")):
            job = self.prepare([self.archive("a.zip", "1/tdata/key_datas")], proxies_text=P[1], api_mode="auto")
            await tb.run_job(job)
        self.assertEqual(job.items[0].status, "ok")
        self.assertIn("CAPTCHA", job.items[0].message)
        with SessionLocal() as db:
            self.assertEqual(db.query(Account).one().api_id, OFFICIAL_DESKTOP_API_ID)

    def test_auto_mode_needs_browser(self):
        with patch("app.services.browser_deps.playwright_available_cached", lambda: (False, "нет Chromium")):
            with self.assertRaises(tb.TDataBatchError) as cm:
                self.prepare([self.archive("a.zip", "1/tdata/key_datas")], proxies_text=P[1], api_mode="auto")
        self.assertIn("нет Chromium", str(cm.exception))

    @staticmethod
    def _drop_creds():
        with SessionLocal() as db:
            db.query(ApiCredential).delete()
            db.commit()


class PanelTests(Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        app.dependency_overrides[require_login] = lambda: "admin"
        cls.client = TestClient(app, follow_redirects=False)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        app.dependency_overrides.clear()

    def _account(self, ident: str, **kw) -> int:
        path = settings.sessions_dir / f"{ident}-test.session"
        path.write_bytes(b"s")
        with SessionLocal() as db:
            a = Account(identifier=ident, session_path=str(path), proxy=kw.pop("proxy", None), enabled=False, **kw)
            db.add(a)
            db.commit()
            return a.id

    def test_delete_one_and_selected_removes_rows_history_and_files(self):
        ids = [self._account(n) for n in ("d1", "d2", "d3")]
        with SessionLocal() as db:
            db.add(DialogMessage(account_id=ids[0], chat_id="5", role="user", content="hi"))
            db.add(ChatStatus(account_id=ids[1], chat_id="5", status="inbound"))
            paths = [db.get(Account, i).session_path for i in ids]
            db.commit()
        r = self.client.post(f"/accounts/{ids[0]}/delete")
        self.assertEqual(r.status_code, 303)
        r = self.client.post("/accounts/delete-selected", data={"ids": [str(ids[1]), str(ids[2])]})
        self.assertIn("2", r.headers["location"])
        with SessionLocal() as db:
            self.assertEqual(db.query(Account).count(), 0)
            self.assertEqual(db.query(DialogMessage).filter(DialogMessage.account_id.in_(ids)).count(), 0)
            self.assertEqual(db.query(ChatStatus).filter(ChatStatus.account_id.in_(ids)).count(), 0)
        self.assertFalse(any(Path(p).exists() for p in paths))

    def test_accounts_page_has_bulk_delete_and_profile_controls(self):
        self._account("p1")
        html = self.client.get("/accounts").text
        self.assertIn("Удалить выбранные", html)
        self.assertIn("нет профиля устройства (задать)", html)
        self.assertIn("создать свой api_id", html)
        self.assertIn("Задать всем без профиля", html)

    def test_set_profile_desktop_generate_manual_and_fill(self):
        aid = self._account("p1")
        self.client.post(f"/accounts/{aid}/device-profile", data={"preset": "desktop"})
        with SessionLocal() as db:
            a = db.get(Account, aid)
            self.assertEqual((a.device_model, a.api_id), ("Desktop", OFFICIAL_DESKTOP_API_ID))
        self.client.post(f"/accounts/{aid}/device-profile",
                         data={"preset": "manual", "device_model": "Mac", "system_version": "macOS 15", "app_version": "1.0"})
        with SessionLocal() as db:
            self.assertEqual(db.get(Account, aid).device_model, "Mac")
        other = self._account("p2")
        self.client.post("/accounts/device-profile/fill", data={"preset": "generate"})
        with SessionLocal() as db:
            self.assertTrue(db.get(Account, other).device_model)
            self.assertIsNone(db.get(Account, other).api_id)            # «сгенерировать» api_id не трогает

    def test_api_autocreate_button_saves_app(self):
        aid = self._account("p1", proxy="socks5://u:p@10.1.1.1:1080", api_id=OFFICIAL_DESKTOP_API_ID,
                            api_hash="b18441a1ff607e10a989891a5462e627", device_model="Desktop",
                            system_version="Windows 10", app_version="3.4.3 x64")

        async def fake_create(session_path, proxy_str, current):
            return {"api_id": 777777, "api_hash": "c" * 32, "phone": "+1"}

        with patch("app.routers.accounts.create_own_app", fake_create):
            r = self.client.post(f"/accounts/{aid}/api-autocreate")
        self.assertIn("777777", r.headers["location"])
        with SessionLocal() as db:
            a = db.get(Account, aid)
            self.assertEqual((a.api_id, a.phone), (777777, "+1"))
            self.assertNotEqual(a.app_version, "3.4.3 x64")

    def test_import_page_offers_api_modes_and_per_account_2fa(self):
        with patch("app.routers.tdata_import.tdata_available", lambda: (True, "")):
            html = self.client.get("/accounts/import-tdata").text
        self.assertIn('name="api_mode"', html)
        self.assertIn('name="cloud_passwords"', html)


if __name__ == "__main__":
    unittest.main()
