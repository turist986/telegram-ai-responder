"""Сквозной сценарий из жалобы: в пуле мобильных прокси общий хост и порт, у каждого аккаунта свой логин.

Строки — ровно те, что прислал заказчик. Проверяется КАЖДЫЙ вход, через который прокси попадает
в систему: форма добавления (поля / строка / строка в поле «хост»), смена прокси, одиночная и
массовая загрузка TData, Excel."""
import re
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote_plus

import openpyxl
from fastapi.testclient import TestClient

from app.auth import require_login
from app.database import SessionLocal, init_db
from app.main import app
from app.models import Account
from app.routers.accounts import _resolve_account_proxy
from app.services import onboarding as ob
from app.services import tdata_batch as tb
from app.services.excel_loader import sync_accounts_from_excel
from app.services.proxy import normalize_proxy
from tests.test_onboarding_panel import _patches

ROMAN = "socks5://5gD5H9P7dp:NTp9MKOynI@mobpool.proxy.market:10000"
YAROSLAV = "socks5://SgRqQDxXfC:xwDdeO1Ehv@mobpool.proxy.market:10000"


class Scenario(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
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
            db.add(Account(identifier="Роман Дубровин", proxy=ROMAN))
            db.commit()

    # ---- форма «Добавить аккаунт менеджера»
    def _add(self, **data):
        p = _patches()
        with p[0], p[1], p[2], p[3]:
            return self.client.post("/accounts/add/start", data={
                "identifier": "Ярослав Романов", "phone": "+79301044237", **data})

    def test_add_form_all_three_ways_accept_the_second_proxy(self):
        variants = {
            "поля": dict(proxy_scheme="socks5", proxy_host="mobpool.proxy.market", proxy_port="10000",
                         proxy_user="SgRqQDxXfC", proxy_password="xwDdeO1Ehv"),
            "строка": dict(proxy=YAROSLAV),
            "строка без схемы": dict(proxy="SgRqQDxXfC:xwDdeO1Ehv@mobpool.proxy.market:10000"),
            "строка в поле хост": dict(proxy_host="SgRqQDxXfC:xwDdeO1Ehv@mobpool.proxy.market:10000"),
            "host:port:login:pass": dict(proxy="mobpool.proxy.market:10000:SgRqQDxXfC:xwDdeO1Ehv"),
        }
        for name, data in variants.items():
            r = self._add(**data)
            self.assertEqual(r.status_code, 303, f"{name}: {r.text[:400]}")

    def test_add_form_refuses_only_the_exact_first_proxy_and_says_what_matched(self):
        for name, data in {
            "поля": dict(proxy_host="mobpool.proxy.market", proxy_port="10000", proxy_user="5gD5H9P7dp",
                         proxy_password="NTp9MKOynI"),
            "строка": dict(proxy=ROMAN),
            "в поле хост": dict(proxy_host="5gD5H9P7dp:NTp9MKOynI@mobpool.proxy.market:10000"),
        }.items():
            r = self._add(**data)
            self.assertEqual(r.status_code, 200, name)
            alert = re.search(r'<div class="alert alert-danger">(.*?)</div>', r.text, re.S).group(1)
            self.assertIn("целиком совпадает", alert, name)
            self.assertIn("Роман Дубровин", alert, name)
            self.assertIn("5gD5H9P7dp", alert, name)            # видно, какой логин совпал
            self.assertNotIn("NTp9MKOynI", alert, name)         # пароль в сообщение не попадает

    def test_same_login_but_other_password_is_a_different_proxy(self):
        r = self._add(proxy_host="mobpool.proxy.market", proxy_port="10000", proxy_user="5gD5H9P7dp",
                      proxy_password="other-password")
        self.assertEqual(r.status_code, 303)

    # ---- смена прокси у существующего аккаунта
    def test_edit_proxy(self):
        with SessionLocal() as db:
            a = Account(identifier="Ярослав Романов", proxy="socks5://old:pw@old.host:1080")
            db.add(a)
            db.commit()
            aid = a.id
        bad = self.client.post(f"/accounts/{aid}/proxy", data={"proxy": ROMAN})
        self.assertIn("целиком совпадает", unquote_plus(bad.headers["location"]))
        ok = self.client.post(f"/accounts/{aid}/proxy", data={
            "proxy_scheme": "socks5", "proxy_host": "mobpool.proxy.market", "proxy_port": "10000",
            "proxy_user": "SgRqQDxXfC", "proxy_password": "xwDdeO1Ehv"})
        self.assertIn("сохранён", unquote_plus(ok.headers["location"]))
        with SessionLocal() as db:
            self.assertEqual(db.get(Account, aid).proxy, YAROSLAV)

    # ---- одиночная загрузка TData
    def test_single_tdata_proxy_resolution(self):
        with SessionLocal() as db:
            self.assertEqual(_resolve_account_proxy(db, "Ярослав Романов", YAROSLAV), YAROSLAV)
            with self.assertRaises(RuntimeError) as cm:
                _resolve_account_proxy(db, "Ярослав Романов", ROMAN)
            self.assertIn("Роман Дубровин", str(cm.exception))

    # ---- массовая загрузка TData: по строке на аккаунт
    def _archives(self, tmp: Path):
        import io
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("Ярослав/tdata/key_datas", b"x")
            zf.writestr("Андрей/tdata/key_datas", b"x")
        path = tmp / "up_a.zip"
        path.write_bytes(buf.getvalue())
        return [("a.zip", path)]

    def test_bulk_tdata(self):
        second = "SgRqQDxXfC:xwDdeO1Ehv@mobpool.proxy.market:10000"
        third = "http://Third111:pw3@mobpool.proxy.market:10000"
        with tempfile.TemporaryDirectory() as t, SessionLocal() as db:
            tmp = Path(t)
            job = tb.prepare_batch(db, self._archives(tmp), tmp / "w", proxies_text=f"{second}\n{third}")
            self.assertEqual(len(job.items), 2)
            with self.assertRaises(tb.TDataBatchError) as cm:                       # прокси Романа — чужой
                tb.prepare_batch(db, self._archives(tmp), tmp / "w2",
                                 proxies_text=f"{second}\n5gD5H9P7dp:NTp9MKOynI@mobpool.proxy.market:10000")
            self.assertIn("Роман Дубровин", str(cm.exception))
            with self.assertRaises(tb.TDataBatchError) as cm:                       # одна и та же строка дважды
                tb.prepare_batch(db, self._archives(tmp), tmp / "w3", proxies_text=f"{second}\n{second}")
            self.assertIn("один и тот же прокси", str(cm.exception))

    # ---- Excel
    def test_excel_accepts_both_proxies(self):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Аккаунт", "Имя менеджера", "Прокси"])
        ws.append(["Роман Дубровин", "Роман", "5gD5H9P7dp:NTp9MKOynI@mobpool.proxy.market:10000"])
        ws.append(["Ярослав Романов", "Ярослав", "SgRqQDxXfC:xwDdeO1Ehv@mobpool.proxy.market:10000"])
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "m.xlsx"
            wb.save(path)
            with SessionLocal() as db:
                sync_accounts_from_excel(path, db)
        with SessionLocal() as db:
            got = {a.identifier: a.proxy for a in db.query(Account).all()}
        self.assertEqual(got["Роман Дубровин"], normalize_proxy("5gD5H9P7dp:NTp9MKOynI@mobpool.proxy.market:10000"))
        self.assertEqual(got["Ярослав Романов"], YAROSLAV)

    # ---- сообщение
    def test_conflict_text_has_no_password(self):
        text = ob.proxy_conflict_text(ROMAN, "Роман Дубровин")
        self.assertIn("mobpool.proxy.market:10000", text)
        self.assertNotIn("NTp9MKOynI", text)


if __name__ == "__main__":
    unittest.main()
