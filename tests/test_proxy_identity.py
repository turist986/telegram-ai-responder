"""Уникальность прокси: сравнивается вся строка (хост, порт, логин, пароль), а не один host:port.

У мобильных/резидентных пулов хост и порт общие, а прокси различаются логином — раньше
такие аккаунты ложно признавались «одним прокси»."""
import unittest

from app.database import SessionLocal, init_db
from app.models import Account
from app.services import onboarding as ob
from app.services.proxy import normalize_proxy, proxy_identity

# формат из жалобы: логин:пароль@хост:порт
POOL_A = "5gD5H9P7dp:NTp9MKOynI@mobpool.proxy.market:10000"
POOL_B = "Zx81QpLmT2:Rr4kVb77Ad@mobpool.proxy.market:10000"


class IdentityTests(unittest.TestCase):
    def test_same_host_port_different_login_are_different_proxies(self):
        self.assertNotEqual(proxy_identity(POOL_A), proxy_identity(POOL_B))

    def test_same_proxy_in_any_format_is_equal(self):
        forms = [
            POOL_A,
            "socks5://" + POOL_A,
            "http://" + POOL_A,                                     # схема не важна
            "mobpool.proxy.market:10000:5gD5H9P7dp:NTp9MKOynI",
            "5gD5H9P7dp:NTp9MKOynI:mobpool.proxy.market:10000",
            "SOCKS5://5gD5H9P7dp:NTp9MKOynI@MobPool.Proxy.Market:10000",   # регистр хоста
            normalize_proxy(POOL_A),
        ]
        ids = {proxy_identity(f) for f in forms}
        self.assertEqual(len(ids), 1)

    def test_different_password_or_port_differs(self):
        base = proxy_identity(POOL_A)
        self.assertNotEqual(base, proxy_identity("5gD5H9P7dp:OTHERPASS@mobpool.proxy.market:10000"))
        self.assertNotEqual(base, proxy_identity("5gD5H9P7dp:NTp9MKOynI@mobpool.proxy.market:10001"))

    def test_proxy_without_login_still_compares_by_host_and_port(self):
        self.assertEqual(proxy_identity("10.1.1.1:1080"), proxy_identity("socks5://10.1.1.1:1080"))
        self.assertNotEqual(proxy_identity("10.1.1.1:1080"), proxy_identity("10.1.1.1:1081"))
        self.assertNotEqual(proxy_identity("10.1.1.1:1080"), proxy_identity("u:p@10.1.1.1:1080"))

    def test_special_characters_in_password(self):
        a = normalize_proxy("u:p@ss/w:rd#1@host.example:1080")
        self.assertEqual(proxy_identity(a), proxy_identity(a))
        self.assertEqual(proxy_identity(a)[3], "p@ss/w:rd#1")


class InUseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        with SessionLocal() as db:
            db.query(Account).delete()
            db.commit()

    def test_pool_accounts_with_different_logins_can_coexist(self):
        with SessionLocal() as db:
            db.add(Account(identifier="roman", proxy=normalize_proxy(POOL_A)))
            db.commit()
            self.assertIsNone(ob.proxy_in_use(db, normalize_proxy(POOL_B)))
            # validate_new_proxy — то, что вызывает форма «Добавить аккаунт менеджера»
            self.assertEqual(ob.validate_new_proxy(db, POOL_B), normalize_proxy(POOL_B))

    def test_exact_same_proxy_is_still_refused_and_names_owner(self):
        with SessionLocal() as db:
            db.add(Account(identifier="roman", proxy=normalize_proxy(POOL_A)))
            db.commit()
            self.assertEqual(ob.proxy_in_use(db, normalize_proxy(POOL_A)), "roman")
            with self.assertRaises(ob.OnboardingError) as cm:
                ob.validate_new_proxy(db, "mobpool.proxy.market:10000:5gD5H9P7dp:NTp9MKOynI")   # другой формат
            self.assertIn("roman", str(cm.exception))

    def test_own_proxy_is_excluded(self):
        with SessionLocal() as db:
            db.add(Account(identifier="roman", proxy=normalize_proxy(POOL_A)))
            db.commit()
            self.assertIsNone(ob.proxy_in_use(db, normalize_proxy(POOL_A), exclude_identifier="roman"))


if __name__ == "__main__":
    unittest.main()
