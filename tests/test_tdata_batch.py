"""Пакетный импорт TData: подготовка (проверка всего заранее) и фоновое выполнение.
Конвертация подменена заглушкой — к настоящему Telegram и реальным TData не обращаемся."""
import io
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.auth import require_login
from app.config import settings
from app.database import SessionLocal, init_db
from app.main import app
from app.models import Account
from app.services import tdata_batch as tb

P = {1: "10.1.1.1:1080:u:p", 2: "10.2.2.2:1080:u:p", 3: "10.3.3.3:1080:u:p"}


def zip_bytes(*names: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n in names:
            zf.writestr(n, b"data")
    return buf.getvalue()


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        settings.sessions_dir.mkdir(parents=True, exist_ok=True)

    def setUp(self):
        with SessionLocal() as db:
            db.query(Account).delete()
            db.commit()
        tb._JOBS.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.calls: list[dict] = []
        self.fail_for: set[str] = set()
        outer = self

        def fake_convert(tdir, dest, proxy, *, passcode=None, cloud_password=None):
            outer.calls.append({"dest": Path(dest).name, "proxy": proxy, "passcode": passcode, "cloud": cloud_password})
            if any(k in str(dest) for k in outer.fail_for):
                raise RuntimeError("Сеанс в этой TData недействителен")
            Path(dest).write_bytes(b"session")

        p = patch("app.services.tdata_batch.tdata_to_session", fake_convert)
        p.start()
        self.addCleanup(p.stop)
        old = (settings.start_stagger_min_seconds, settings.start_stagger_max_seconds)
        settings.start_stagger_min_seconds = settings.start_stagger_max_seconds = 0
        self.addCleanup(lambda: (setattr(settings, "start_stagger_min_seconds", old[0]),
                                 setattr(settings, "start_stagger_max_seconds", old[1])))

    def archive(self, name: str, *members: str) -> tuple[str, Path]:
        path = self.tmp / f"up_{name}"
        path.write_bytes(zip_bytes(*members))
        return name, path

    def prepare(self, archives, **kw) -> tb.BatchJob:
        with SessionLocal() as db:
            return tb.prepare_batch(db, archives, self.tmp / "work", **kw)


class PrepareTests(Base):
    def test_identifiers_from_folder_names_in_natural_order_and_proxies_by_position(self):
        job = self.prepare([self.archive("all.zip", "10/tdata/key_datas", "2/tdata/key_datas", "1/tdata/key_datas")],
                           proxies_text="\n".join(P[i] for i in (1, 2, 3)))
        self.assertEqual([i.identifier for i in job.items], ["1", "2", "10"])            # 2 раньше 10
        self.assertEqual([i.proxy.split("@")[-1] for i in job.items], ["10.1.1.1:1080", "10.2.2.2:1080", "10.3.3.3:1080"])

    def test_tdata_in_archive_root_takes_archive_name_and_many_archives_combine(self):
        job = self.prepare([self.archive("79991110001.zip", "tdata/key_datas"),
                            self.archive("79991110002.zip", "Telegram/tdata/key_datas")],
                           proxies_text=f"{P[1]}\n{P[2]}")
        self.assertEqual([i.identifier for i in job.items], ["79991110001", "79991110002"])

    def test_manual_identifiers_and_count_checks(self):
        arch = [self.archive("a.zip", "1/tdata/key_datas", "2/tdata/key_datas")]
        job = self.prepare(arch, proxies_text=f"{P[1]}\n{P[2]}", identifiers_text="mgr_anna\nmgr_petr")
        self.assertEqual([i.identifier for i in job.items], ["mgr_anna", "mgr_petr"])
        with self.assertRaises(tb.TDataBatchError):
            self.prepare(arch, proxies_text=f"{P[1]}\n{P[2]}", identifiers_text="only_one")
        with self.assertRaises(tb.TDataBatchError) as cm:
            self.prepare(arch, proxies_text=P[1])                                      # прокси меньше, чем аккаунтов
        self.assertIn("ровно один прокси", str(cm.exception))
        with self.assertRaises(tb.TDataBatchError):
            self.prepare(arch, proxies_text=f"{P[1]}\n{P[2]}", identifiers_text="bad id!\nok")

    def test_missing_proxy_lists_who_and_nothing_is_prepared(self):
        with SessionLocal() as db:
            db.add(Account(identifier="1", proxy="socks5://10.9.9.1:1080"))            # у «1» прокси уже сохранён
            db.commit()
        with self.assertRaises(tb.TDataBatchError) as cm:
            self.prepare([self.archive("a.zip", "1/tdata/key_datas", "2/tdata/key_datas")])
        self.assertIn("2", str(cm.exception))
        self.assertNotIn("Не задан прокси для: 1", str(cm.exception))

    def test_existing_account_proxy_is_used_when_field_empty(self):
        with SessionLocal() as db:
            db.add(Account(identifier="1", proxy="socks5://10.9.9.1:1080"))
            db.commit()
        job = self.prepare([self.archive("a.zip", "1/tdata/key_datas")])
        self.assertIn("10.9.9.1", job.items[0].proxy)

    def test_duplicate_or_foreign_proxy_rejected(self):
        arch = [self.archive("a.zip", "1/tdata/key_datas", "2/tdata/key_datas")]
        with self.assertRaises(tb.TDataBatchError) as cm:
            self.prepare(arch, proxies_text="10.1.1.1:1080:u:p\n10.1.1.1:1080:x:y")
        self.assertIn("один прокси", str(cm.exception))
        with SessionLocal() as db:
            db.add(Account(identifier="other", proxy="socks5://10.2.2.2:1080"))
            db.commit()
        with self.assertRaises(tb.TDataBatchError) as cm:
            self.prepare(arch, proxies_text=f"{P[1]}\n{P[2]}")
        self.assertIn("other", str(cm.exception))

    def test_existing_session_is_skipped_unless_replace(self):
        with SessionLocal() as db:
            db.add(Account(identifier="1", session_path="/x/1.session", proxy="socks5://10.1.1.1:1080"))
            db.commit()
        arch = [self.archive("a.zip", "1/tdata/key_datas", "2/tdata/key_datas")]
        job = self.prepare(arch, proxies_text=f"{P[1]}\n{P[2]}")
        self.assertEqual([i.status for i in job.items], ["skipped", "queued"])
        job2 = self.prepare(arch, proxies_text=f"{P[1]}\n{P[2]}", replace_existing=True)
        self.assertEqual([i.status for i in job2.items], ["queued", "queued"])

    def test_bad_archive_names_the_file(self):
        bad = self.tmp / "up_bad.zip"
        bad.write_bytes(b"not an archive")
        with self.assertRaises(tb.TDataBatchError) as cm:
            self.prepare([("bad.zip", bad)])
        self.assertIn("bad.zip", str(cm.exception))
        with self.assertRaises(tb.TDataBatchError) as cm:
            self.prepare([self.archive("empty.zip", "readme.txt")])
        self.assertIn("не найдено ни одной папки TData", str(cm.exception))


class RunTests(Base, unittest.IsolatedAsyncioTestCase):
    async def test_all_accounts_imported_disabled_via_own_proxy_and_workdir_cleaned(self):
        job = self.prepare([self.archive("a.zip", "1/tdata/key_datas", "2/tdata/key_datas")],
                           proxies_text=f"{P[1]}\n{P[2]}")
        await tb.run_job(job, passcode="LOCALPASS", cloud_password="CLOUD2FA")
        self.assertEqual([i.status for i in job.items], ["ok", "ok"])
        self.assertEqual({c["proxy"][1] for c in self.calls}, {"10.1.1.1", "10.2.2.2"})
        self.assertTrue(all(c["passcode"] == "LOCALPASS" and c["cloud"] == "CLOUD2FA" for c in self.calls))
        with SessionLocal() as db:
            accs = db.query(Account).order_by(Account.identifier).all()
            self.assertEqual([(a.identifier, a.enabled, a.is_authorized) for a in accs], [("1", False, True), ("2", False, True)])
            self.assertTrue(all(Path(a.session_path).exists() for a in accs))
        self.assertFalse(job.workdir.exists())                                          # ключи с диска убраны
        self.assertFalse(job.running)
        blob = repr(vars(job))
        self.assertNotIn("LOCALPASS", blob)                                             # пароли в задаче не хранятся
        self.assertNotIn("CLOUD2FA", blob)

    async def test_one_failure_does_not_cancel_the_rest(self):
        self.fail_for = {"2-"}
        job = self.prepare([self.archive("a.zip", "1/tdata/key_datas", "2/tdata/key_datas", "3/tdata/key_datas")],
                           proxies_text="\n".join(P[i] for i in (1, 2, 3)))
        await tb.run_job(job)
        self.assertEqual([i.status for i in job.items], ["ok", "error", "ok"])
        self.assertIn("недействителен", job.items[1].message)
        with SessionLocal() as db:
            self.assertEqual({a.identifier for a in db.query(Account).all()}, {"1", "3"})   # битый аккаунт не создан

    async def test_job_view_masks_proxy_password_and_second_job_is_refused(self):
        job = self.prepare([self.archive("a.zip", "1/tdata/key_datas")], proxies_text="10.1.1.1:1080:user:SECRETPW")
        view = tb.job_view(job)
        self.assertNotIn("SECRETPW", repr(view))
        tb.start_job(job)
        with self.assertRaises(tb.TDataBatchError):
            tb.start_job(self.prepare([self.archive("b.zip", "5/tdata/key_datas")], proxies_text=P[2]))
        await job.task


class RouterTests(Base):
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

    def setUp(self):
        super().setUp()
        p = patch("app.routers.tdata_import.tdata_available", lambda: (True, ""))
        p.start()
        self.addCleanup(p.stop)

    def _wait_done(self, location: str) -> dict:
        token = location.rstrip("/").split("/")[-1]
        for _ in range(100):
            data = self.client.get(f"/accounts/import-tdata/{token}/status").json()
            if data.get("done"):
                return data
            time.sleep(0.1)
        self.fail("импорт не завершился")

    def test_page_renders_with_dropzone(self):
        r = self.client.get("/accounts/import-tdata")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Перетащите сюда архивы", r.text)
        self.assertIn(".zip,.rar", r.text)
        self.assertIn("Импорт TData", self.client.get("/accounts").text)                # ссылка со страницы аккаунтов

    def test_bulk_upload_end_to_end(self):
        # два архива за раз: в первом два аккаунта, во втором один
        r = self.client.post(
            "/accounts/import-tdata",
            data={"proxies": "\n".join(P[i] for i in (1, 2, 3)), "tdata_passcode": "", "cloud_password": ""},
            files=[("archives", ("a.zip", zip_bytes("1/tdata/key_datas", "2/tdata/key_datas"))),
                   ("archives", ("b.zip", zip_bytes("3/tdata/key_datas")))],
        )
        self.assertEqual(r.status_code, 303)
        self.assertRegex(r.headers["location"], r"^/accounts/import-tdata/[\w-]+$")
        data = self._wait_done(r.headers["location"])
        self.assertEqual(data["counts"]["ok"], 3)
        self.assertTrue(all("@" not in i["proxy"] for i in data["items"]))               # без логина/пароля
        with SessionLocal() as db:
            self.assertEqual(db.query(Account).filter_by(enabled=False).count(), 3)
        self.assertEqual(self.client.get(r.headers["location"]).status_code, 200)

    def test_validation_errors_are_shown_on_the_form(self):
        r = self.client.post("/accounts/import-tdata", data={"proxies": ""},
                             files=[("archives", ("a.zip", zip_bytes("1/tdata/key_datas")))])
        self.assertEqual(r.status_code, 200)
        self.assertIn("Не задан прокси для: 1", r.text)
        r = self.client.post("/accounts/import-tdata", data={"proxies": P[1]})
        self.assertIn("хотя бы один архив", r.text)
        r = self.client.post("/accounts/import-tdata", data={"proxies": P[1]},
                             files=[("archives", ("x.zip", b"garbage"))])
        self.assertIn("не .zip и не .rar", r.text)
        with SessionLocal() as db:
            self.assertEqual(db.query(Account).count(), 0)

    def test_unavailable_or_disabled(self):
        with patch("app.routers.tdata_import.tdata_available", lambda: (False, "нужен opentele")):
            r = self.client.post("/accounts/import-tdata", data={"proxies": P[1]},
                                 files=[("archives", ("a.zip", zip_bytes("1/tdata/key_datas")))])
            self.assertIn("нужен opentele", r.text)
        with patch.object(settings, "allow_tdata_import", False):
            r = self.client.post("/accounts/import-tdata", data={"proxies": P[1]},
                                 files=[("archives", ("a.zip", zip_bytes("1/tdata/key_datas")))])
            self.assertIn("отключён", r.text)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
