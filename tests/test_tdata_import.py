"""Импорт TData: отдельный сеанс (CreateNewSession) и обязательный прокси.

К настоящему Telegram и к реальным папкам TData тесты не обращаются — opentele и
конвертация подменены заглушками. Реальная конвертация создаёт настоящий сеанс на
живом аккаунте, поэтому автоматически её не гоняем."""
import io
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote_plus

from fastapi.testclient import TestClient

from app.auth import require_login
from app.config import settings
from app.database import SessionLocal, init_db
from app.main import app
from app.models import Account

try:
    from app.services._opentele_compat import patch_opentele_for_new_python

    patch_opentele_for_new_python()
    import opentele.api as _oapi
    import opentele.td as _otd  # noqa: F401

    HAVE_OPENTELE = True
except Exception:  # noqa: BLE001 — opentele опционален
    HAVE_OPENTELE = False


class FakeClient:
    async def disconnect(self):
        pass


class FakeTDesktop:
    calls: list = []
    loaded = True
    error: Exception | None = None

    def __init__(self, base_path=None, **kw):
        self.base_path = base_path

    def isLoaded(self):
        return FakeTDesktop.loaded

    async def ToTelethon(self, session=None, flag=None, api=None, password=None, **kwargs):
        FakeTDesktop.calls.append({"session": session, "flag": flag, "kwargs": kwargs})
        if FakeTDesktop.error:
            raise FakeTDesktop.error
        return FakeClient()


@unittest.skipUnless(HAVE_OPENTELE, "opentele не установлен")
class ConversionTests(unittest.TestCase):
    PROXY = (2, "10.0.0.1", 1080, True, None, None)

    def setUp(self):
        FakeTDesktop.calls, FakeTDesktop.loaded, FakeTDesktop.error = [], True, None
        self.tmp = Path(settings.sessions_dir)
        self.tmp.mkdir(parents=True, exist_ok=True)
        (self.tmp / "td").mkdir(exist_ok=True)
        (self.tmp / "td" / "key_datas").write_bytes(b"x")

    def _run(self, proxy):
        from app.services.session_utils import tdata_to_session

        with patch("opentele.td.TDesktop", FakeTDesktop):
            tdata_to_session(self.tmp / "td", self.tmp / "out.session", proxy)

    def test_creates_new_session_not_desktop_key_and_uses_proxy(self):
        self._run(self.PROXY)
        call = FakeTDesktop.calls[0]
        self.assertIs(call["flag"], _oapi.CreateNewSession)       # отдельный сеанс, а не ключ Desktop
        self.assertIsNot(call["flag"], _oapi.UseCurrentSession)
        self.assertEqual(call["kwargs"]["proxy"], self.PROXY)     # все подключения — через прокси

    def test_without_proxy_refused_before_any_network_call(self):
        with self.assertRaises(RuntimeError) as cm:
            self._run(None)
        self.assertIn("прокси", str(cm.exception).lower())
        self.assertEqual(FakeTDesktop.calls, [])                  # к Telegram даже не подключались

    def test_any_library_error_becomes_readable_runtime_error(self):
        FakeTDesktop.error = ValueError("boom")
        with self.assertRaises(RuntimeError) as cm:
            self._run(self.PROXY)
        self.assertIn("ValueError", str(cm.exception))            # а не голый 500 без причины

    def test_unreadable_tdata(self):
        FakeTDesktop.loaded = False
        with self.assertRaises(RuntimeError):
            self._run(self.PROXY)


def _zip(*names: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n in names:
            zf.writestr(n, b"data")
    return buf.getvalue()


class RouterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        settings.sessions_dir.mkdir(parents=True, exist_ok=True)
        app.dependency_overrides[require_login] = lambda: "admin"
        cls.client = TestClient(app, follow_redirects=False)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        app.dependency_overrides.clear()

    def setUp(self):
        with SessionLocal() as db:
            db.query(Account).delete()
            db.commit()
        self.calls: list = []
        self.fail_for: set[str] = set()
        outer = self

        def fake_convert(tdir, dest, proxy):
            outer.calls.append({"tdir": Path(tdir).name, "dest": Path(dest), "proxy": proxy})
            if any(k in str(dest) for k in outer.fail_for):
                raise RuntimeError("Сеанс в этой TData недействителен")
            Path(dest).write_bytes(b"session")

        self._patch = patch("app.routers.accounts.tdata_to_session", fake_convert)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        # без пауз между аккаунтами в тестах
        self._old = (settings.start_stagger_min_seconds, settings.start_stagger_max_seconds)
        settings.start_stagger_min_seconds = settings.start_stagger_max_seconds = 0
        self.addCleanup(lambda: setattr(settings, "start_stagger_min_seconds", self._old[0]))
        self.addCleanup(lambda: setattr(settings, "start_stagger_max_seconds", self._old[1]))

    def _msg(self, r) -> str:
        return unquote_plus(r.headers["location"])

    def test_single_import_requires_proxy(self):
        r = self.client.post("/accounts/upload-tdata", data={"identifier": "a1"},
                             files={"tdata_zip": ("t.zip", _zip("tdata/key_datas"))})
        self.assertIn("прокси", self._msg(r).lower())
        self.assertEqual(self.calls, [])                          # конвертация не запускалась
        with SessionLocal() as db:
            self.assertEqual(db.query(Account).count(), 0)

    def test_single_import_creates_disabled_account_with_proxy(self):
        r = self.client.post("/accounts/upload-tdata",
                             data={"identifier": "a1", "proxy": "socks5://u:p@10.7.7.7:1080"},
                             files={"tdata_zip": ("t.zip", _zip("tdata/key_datas"))})
        self.assertIn("отдельный сеанс", self._msg(r))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["proxy"][1:3], ("10.7.7.7", 1080))   # в конвертацию ушёл именно прокси аккаунта
        with SessionLocal() as db:
            a = db.query(Account).filter_by(identifier="a1").one()
            self.assertFalse(a.enabled)                           # «остывание» новой сессии
            self.assertTrue(a.is_authorized)
            self.assertIn("10.7.7.7", a.proxy)
            self.assertTrue(Path(a.session_path).exists())

    def test_single_import_rejects_proxy_used_by_another_account(self):
        with SessionLocal() as db:
            db.add(Account(identifier="other", proxy="socks5://x:y@10.8.8.8:1080"))
            db.commit()
        r = self.client.post("/accounts/upload-tdata",
                             data={"identifier": "a2", "proxy": "socks5://10.8.8.8:1080"},
                             files={"tdata_zip": ("t.zip", _zip("tdata/key_datas"))})
        self.assertIn("other", self._msg(r))
        self.assertEqual(self.calls, [])

    def test_conversion_error_is_shown_not_500(self):
        self.fail_for = {"a3"}
        r = self.client.post("/accounts/upload-tdata",
                             data={"identifier": "a3", "proxy": "socks5://10.9.9.9:1080"},
                             files={"tdata_zip": ("t.zip", _zip("tdata/key_datas"))})
        self.assertEqual(r.status_code, 303)
        self.assertIn("недействителен", self._msg(r))
        with SessionLocal() as db:
            self.assertEqual(db.query(Account).count(), 0)        # битая попытка аккаунт не создаёт

    def _bulk_accounts(self, proxies: dict[str, str | None]):
        with SessionLocal() as db:
            for ident, proxy in proxies.items():
                db.add(Account(identifier=ident, proxy=proxy))
            db.commit()

    def _bulk(self):
        return self.client.post("/accounts/upload", data={},
                                files={"auth_file": ("all.zip", _zip("1/tdata/key_datas", "2/tdata/key_datas"))})

    def test_bulk_import_all_accounts_each_via_own_proxy(self):
        self._bulk_accounts({"1": "socks5://10.1.1.1:1080", "2": "socks5://10.2.2.2:1080"})
        r = self._bulk()
        self.assertIn("Импортировано", self._msg(r))
        self.assertEqual({c["proxy"][1] for c in self.calls}, {"10.1.1.1", "10.2.2.2"})
        with SessionLocal() as db:
            accs = db.query(Account).order_by(Account.identifier).all()
            self.assertEqual([a.enabled for a in accs], [False, False])
            self.assertTrue(all(a.session_path for a in accs))

    def test_bulk_missing_proxy_imports_nothing(self):
        self._bulk_accounts({"1": "socks5://10.1.1.1:1080", "2": None})
        r = self._bulk()
        self.assertIn("Ничего не импортировано", self._msg(r))
        self.assertEqual(self.calls, [])                          # ни один аккаунт не ушёл в Telegram

    def test_bulk_shared_proxy_imports_nothing(self):
        self._bulk_accounts({"1": "socks5://10.1.1.1:1080", "2": "socks5://u:p@10.1.1.1:1080"})
        r = self._bulk()
        self.assertIn("один прокси", self._msg(r))
        self.assertEqual(self.calls, [])

    def test_bulk_one_failure_does_not_cancel_the_rest(self):
        self._bulk_accounts({"1": "socks5://10.1.1.1:1080", "2": "socks5://10.2.2.2:1080"})
        self.fail_for = {"2-"}                                    # имя файла сессии начинается с идентификатора
        r = self._bulk()
        msg = self._msg(r)
        self.assertIn("Импортировано", msg)
        self.assertIn("Ошибки", msg)
        with SessionLocal() as db:
            self.assertTrue(db.query(Account).filter_by(identifier="1").one().session_path)
            self.assertFalse(db.query(Account).filter_by(identifier="2").one().session_path)

    def test_import_can_be_switched_off(self):
        with patch.object(settings, "allow_legacy_session_import", False):
            r = self.client.post("/accounts/upload-tdata",
                                 data={"identifier": "z", "proxy": "socks5://10.3.3.3:1080"},
                                 files={"tdata_zip": ("t.zip", _zip("tdata/key_datas"))})
        self.assertIn("отключён", self._msg(r))
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
