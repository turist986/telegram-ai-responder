"""Все формы, куда вводится прокси: отдельные поля, строка, строка в поле «хост»,
приоритет строки над предзаполненными полями, схема из списка, уникальность по всей строке."""
import unittest
from urllib.parse import unquote_plus, urlparse

from fastapi.testclient import TestClient

from app.auth import require_login
from app.database import SessionLocal, init_db
from app.main import app
from app.models import Account
from app.services import onboarding as ob
from app.services.proxy import (
    ProxyConfigError, looks_like_proxy_string, normalize_proxy, proxy_from_form,
)
from tests.test_onboarding_panel import _patches

POOL_A = "5gD5H9P7dp:NTp9MKOynI@mobpool.proxy.market:10000"
POOL_B = "Zx81QpLmT2:Rr4kVb77Ad@mobpool.proxy.market:10000"


class HelperTests(unittest.TestCase):
    def test_looks_like_string(self):
        self.assertTrue(looks_like_proxy_string(POOL_A))
        self.assertTrue(looks_like_proxy_string("1.2.3.4:1080"))
        self.assertTrue(looks_like_proxy_string("socks5://1.2.3.4:1080"))
        self.assertFalse(looks_like_proxy_string("1.2.3.4"))                       # обычный хост
        self.assertFalse(looks_like_proxy_string(POOL_A, port="10000"))            # порт задан отдельно
        self.assertFalse(looks_like_proxy_string(""))

    def test_proxy_from_form_priority_and_default_scheme(self):
        # отдельные поля
        self.assertEqual(proxy_from_form("http", "h.example", "8080", "u", "p", ""), "http://u:p@h.example:8080")
        # строка в поле хоста; схема из списка — по умолчанию
        self.assertEqual(proxy_from_form("http", POOL_A, "", "", "", ""),
                         "http://5gD5H9P7dp:NTp9MKOynI@mobpool.proxy.market:10000")
        # строка со своей схемой — схема из строки главнее списка
        self.assertTrue(proxy_from_form("http", "", "", "", "", "socks5://" + POOL_A).startswith("socks5://"))
        # явная строка главнее полей
        self.assertIn("mobpool.proxy.market", proxy_from_form("socks5", "old.host", "1080", "", "", POOL_A))
        self.assertIsNone(proxy_from_form("socks5", "", "", "", "", ""))
        with self.assertRaises(ProxyConfigError):
            proxy_from_form("socks5", "h.example", "", "", "", "")               # порт не указан


class FormTests(unittest.TestCase):
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
            db.commit()

    def _add(self, **data):
        base = {"identifier": "n1", "phone": "+12223334455"}
        p1, p2, p3, p4 = _patches()
        with p1, p2, p3, p4:
            return self.client.post("/accounts/add/start", data={**base, **data})

    def _state_proxy(self, r):
        self.assertEqual(r.status_code, 303, r.text[:300])
        token = r.headers["location"].rsplit("/", 1)[-1]
        return ob.get_state(token).proxy

    # ---- «Добавить аккаунт менеджера»
    def test_add_with_string_field_and_selected_scheme(self):
        r = self._add(proxy=POOL_A, proxy_scheme="http")
        self.assertEqual(urlparse(self._state_proxy(r)).scheme, "http")

    def test_add_with_string_pasted_into_host_field(self):
        r = self._add(proxy_host=POOL_A, proxy_scheme="socks5")
        px = urlparse(self._state_proxy(r))
        self.assertEqual((px.hostname, px.port, px.username), ("mobpool.proxy.market", 10000, "5gD5H9P7dp"))

    def test_add_with_separate_fields(self):
        r = self._add(proxy_host="mobpool.proxy.market", proxy_port="10000", proxy_user="5gD5H9P7dp",
                      proxy_password="NTp9MKOynI")
        self.assertEqual(urlparse(self._state_proxy(r)).username, "5gD5H9P7dp")

    def test_add_same_host_port_different_login_ok_exact_duplicate_refused(self):
        with SessionLocal() as db:
            db.add(Account(identifier="roman", proxy=normalize_proxy(POOL_A)))
            db.commit()
        ok = self._add(identifier="n2", proxy=POOL_B)
        self.assertEqual(ok.status_code, 303)
        for kind, data in (("строка", {"proxy": "mobpool.proxy.market:10000:5gD5H9P7dp:NTp9MKOynI"}),
                           ("хост", {"proxy_host": POOL_A}),
                           ("поля", {"proxy_host": "mobpool.proxy.market", "proxy_port": "10000",
                                     "proxy_user": "5gD5H9P7dp", "proxy_password": "NTp9MKOynI"})):
            r = self._add(identifier="n3", **data)
            self.assertEqual(r.status_code, 200, kind)
            self.assertIn("roman", r.text, kind)

    # ---- смена прокси у существующего аккаунта
    def _account(self, ident, proxy):
        with SessionLocal() as db:
            a = Account(identifier=ident, proxy=proxy)
            db.add(a)
            db.commit()
            return a.id

    def _proxy_of(self, acc_id):
        with SessionLocal() as db:
            return db.get(Account, acc_id).proxy

    def test_edit_pasted_string_wins_over_prefilled_fields(self):
        acc = self._account("a1", "socks5://old:pw@old.host:1080")
        # форма присылает поля, предзаполненные старым прокси, + новую строку
        r = self.client.post(f"/accounts/{acc}/proxy", data={
            "proxy_scheme": "socks5", "proxy_host": "old.host", "proxy_port": "1080",
            "proxy_user": "old", "proxy_password": "", "proxy": POOL_A})
        self.assertIn("сохранён", unquote_plus(r.headers["location"]))
        self.assertIn("mobpool.proxy.market", self._proxy_of(acc))

    def test_edit_string_in_host_field_and_scheme_from_list(self):
        acc = self._account("a2", "socks5://old:pw@old.host:1080")
        self.client.post(f"/accounts/{acc}/proxy", data={"proxy_scheme": "http", "proxy_host": POOL_B})
        stored = urlparse(self._proxy_of(acc))
        self.assertEqual((stored.scheme, stored.hostname, stored.port), ("http", "mobpool.proxy.market", 10000))

    def test_edit_same_host_port_other_login_ok_exact_duplicate_refused(self):
        self._account("owner", normalize_proxy(POOL_A))
        acc = self._account("a3", "socks5://old:pw@old.host:1080")
        bad = self.client.post(f"/accounts/{acc}/proxy", data={"proxy": POOL_A})
        self.assertIn("owner", unquote_plus(bad.headers["location"]))
        self.assertIn("old.host", self._proxy_of(acc))
        ok = self.client.post(f"/accounts/{acc}/proxy", data={"proxy": POOL_B})
        self.assertIn("сохранён", unquote_plus(ok.headers["location"]))
        self.assertIn("Zx81QpLmT2", self._proxy_of(acc))

    def test_edit_keeps_password_when_field_left_empty(self):
        acc = self._account("a4", "socks5://u1:secret@host.example:1080")
        self.client.post(f"/accounts/{acc}/proxy", data={
            "proxy_scheme": "socks5", "proxy_host": "host.example", "proxy_port": "1081", "proxy_user": "u1"})
        self.assertEqual(self._proxy_of(acc), "socks5://u1:secret@host.example:1081")


if __name__ == "__main__":
    unittest.main()
