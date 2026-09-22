import unittest
from unittest.mock import patch
from urllib.parse import unquote_plus

from fastapi.testclient import TestClient

from app.auth import require_login
from app.config import settings
from app.database import SessionLocal, init_db
from app.main import app
from app.models import Account, ApiCredential
from app.services import onboarding as ob
from app.services.settings_store import get_protection, set_protection
from tests.test_onboarding_panel import FakeClient, FakeCreator


def _acc(ident, api_id, api_hash="a" * 32, proxy=None) -> Account:
    return Account(identifier=ident, api_id=api_id, api_hash=api_hash, proxy=proxy)


class PoolLogicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        with SessionLocal() as db:
            db.query(Account).delete()
            db.query(ApiCredential).delete()
            db.commit()
            set_protection(db, {"checking_preset": "custom"})  # сброс к дефолтам, включая лимит пула

    def test_default_limit_and_bounds(self):
        with SessionLocal() as db:
            self.assertEqual(get_protection(db)["api_pool_max_accounts"], 5)
            set_protection(db, {"checking_preset": "custom", "api_pool_max_accounts": "3"})
            self.assertEqual(get_protection(db)["api_pool_max_accounts"], 3)
            from app.services.settings_store import ProtectionConfigError
            with self.assertRaises(ProtectionConfigError):
                set_protection(db, {"checking_preset": "custom", "api_pool_max_accounts": "999"})
            self.assertEqual(get_protection(db)["api_pool_max_accounts"], 3)  # ничего не записалось

    def test_list_pools_groups_by_api_id(self):
        with SessionLocal() as db:
            db.add_all([_acc("m1", 111), _acc("m2", 111), _acc("m3", 222), Account(identifier="legacy")])
            db.commit()
            pools = ob.list_api_pools(db)
        self.assertEqual({p["api_id"]: p["count"] for p in pools}, {111: 2, 222: 1})
        self.assertEqual(sorted(next(p for p in pools if p["api_id"] == 111)["members"]), ["m1", "m2"])

    def test_validate_pool_choice_unknown_and_full(self):
        with SessionLocal() as db:
            with self.assertRaises(ob.OnboardingError):
                ob.validate_pool_choice(db, 999999)
            set_protection(db, {"checking_preset": "custom", "api_pool_max_accounts": "2"})
            db.add_all([_acc("m1", 111), _acc("m2", 111)])
            db.commit()
            with self.assertRaises(ob.OnboardingError) as cm:
                ob.validate_pool_choice(db, 111)
            self.assertIn("лимит 2", str(cm.exception))
            # апдейт СВОЕГО же аккаунта не должен считаться «лишним» местом в пуле
            self.assertEqual(ob.validate_pool_choice(db, 111, exclude_identifier="m1"), "a" * 32)

    async def _begin_pool(self, db, pool_api_id, ident="new1", proxy="socks5://9.9.9.1:1080"):
        with patch.object(ob, "ApiAppCreator") as creator_cls, \
             patch.object(ob, "TelegramClient", FakeClient), \
             patch.object(ob, "test_proxy", lambda p: (True, "ok")), \
             patch.object(ob, "choose_ip_family", lambda proxy, dc: (False, "149.154.167.51")):
            state = await ob.begin(db, ident, "+12223334455", proxy, "Иван", "ru", pool_api_id=pool_api_id)
            creator_cls.assert_not_called()  # для пула браузер my.telegram.org не запускается
            return state

    def test_begin_with_pool_skips_browser_and_goes_to_login(self):
        import asyncio

        with SessionLocal() as db:
            db.add(_acc("owner", 4242, "b" * 32))
            db.commit()
            state = asyncio.run(self._begin_pool(db, 4242))
        self.assertEqual(state.step, "login_code")
        self.assertEqual((state.api_id, state.api_hash), (4242, "b" * 32))
        self.assertIsNone(state.creator)

    def test_begin_rejects_full_pool_before_touching_network(self):
        import asyncio

        with SessionLocal() as db:
            set_protection(db, {"checking_preset": "custom", "api_pool_max_accounts": "1"})
            db.add(_acc("owner", 5555, "c" * 32))
            db.commit()
            with patch.object(ob, "test_proxy") as tp:
                with self.assertRaises(ob.OnboardingError):
                    asyncio.run(ob.begin(db, "n2", "+12223334455", "socks5://9.9.9.2:1080", "", "ru", pool_api_id=5555))
                tp.assert_not_called()  # лимит проверяется раньше сетевой проверки прокси


class CredentialUploadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        with SessionLocal() as db:
            db.query(Account).delete()
            db.query(ApiCredential).delete()
            db.commit()

    def test_parse_formats(self):
        rows = ob.parse_credential_lines(
            "1111111:" + "a" * 32 + "\n"
            "# коммент\n\n"
            "2222222 " + "b" * 32 + "  моя метка\n"
            "3333333:" + "c" * 32 + ":третья"
        )
        self.assertEqual(rows, [
            (1111111, "a" * 32, None),
            (2222222, "b" * 32, "моя метка"),
            (3333333, "c" * 32, "третья"),
        ])

    def test_parse_rejects_bad_lines_with_line_number(self):
        for bad in ("plaintext", "abc:" + "a" * 32, "111:short"):
            with self.assertRaises(ob.CredentialUploadError):
                ob.parse_credential_lines(bad)
        with self.assertRaises(ob.CredentialUploadError):
            ob.parse_credential_lines("   \n  # only comments\n")

    def test_add_credentials_skips_duplicates_against_library_and_accounts(self):
        with SessionLocal() as db:
            db.add(Account(identifier="has-api", api_id=999, api_hash="d" * 32))
            db.commit()
            result = ob.add_credentials(db, f"111:{'a'*32}\n999:{'e'*32}\n111:{'f'*32}")
        self.assertEqual(result["added"], [111])
        self.assertEqual(sorted(result["skipped"]), [111, 999])  # 999 занят аккаунтом, второй 111 — дубль в этой же пачке
        with SessionLocal() as db:
            cred = db.query(ApiCredential).filter_by(api_id=111).one()
            self.assertEqual(cred.api_hash, "a" * 32)  # не перезаписан вторым вхождением

    def test_uploaded_credential_appears_in_pool_listing_with_zero_count(self):
        with SessionLocal() as db:
            ob.add_credentials(db, f"555:{'a'*32}:запасной")
            pools = ob.list_api_pools(db)
        pool = next(p for p in pools if p["api_id"] == 555)
        self.assertEqual((pool["count"], pool["label"], pool["members"]), (0, "запасной", []))


class AssignAndDistributeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        with SessionLocal() as db:
            db.query(Account).delete()
            db.query(ApiCredential).delete()
            db.commit()
            set_protection(db, {"checking_preset": "custom", "api_pool_max_accounts": "2"})

    def test_assign_to_free_account_respects_limit(self):
        with SessionLocal() as db:
            db.add_all([Account(identifier="free1"), Account(identifier="owner", api_id=42, api_hash="a" * 32)])
            db.commit()
            free_id = db.query(Account).filter_by(identifier="free1").one().id
            ob.assign_api_to_account(db, free_id, 42)
            acc = db.get(Account, free_id)
            self.assertEqual((acc.api_id, acc.api_hash), (42, "a" * 32))

            db.add(Account(identifier="free2"))
            db.commit()
            free2_id = db.query(Account).filter_by(identifier="free2").one().id
            with self.assertRaises(ob.OnboardingError):  # лимит 2 уже занят (owner + free1)
                ob.assign_api_to_account(db, free2_id, 42)

    def test_assign_unknown_api_id_fails(self):
        with SessionLocal() as db:
            db.add(Account(identifier="free1"))
            db.commit()
            acc_id = db.query(Account).filter_by(identifier="free1").one().id
            with self.assertRaises(ob.OnboardingError):
                ob.assign_api_to_account(db, acc_id, 999999)

    def test_reassign_same_account_is_idempotent(self):
        with SessionLocal() as db:
            db.add(Account(identifier="a1", api_id=7, api_hash="b" * 32))
            db.commit()
            acc_id = db.query(Account).filter_by(identifier="a1").one().id
            ob.assign_api_to_account(db, acc_id, 7)  # не должно упереться в собственный лимит
            self.assertEqual(db.get(Account, acc_id).api_id, 7)

    def test_auto_distribute_fills_existing_pools_round_robin_and_leaves_rest(self):
        with SessionLocal() as db:
            db.add(Account(identifier="owner1", api_id=10, api_hash="a" * 32))  # 1/2 места
            db.add(Account(identifier="owner2", api_id=20, api_hash="b" * 32))  # 1/2 места
            for i in range(3):
                db.add(Account(identifier=f"free{i}"))
            db.commit()
            result = ob.auto_distribute(db)
        assigned_ids = {ident: api for ident, api in result["assigned"]}
        self.assertEqual(len(assigned_ids), 2)  # ровно 2 свободных места (по одному в каждом пуле)
        self.assertEqual(set(assigned_ids.values()), {10, 20})
        self.assertEqual(len(result["unassigned"]), 1)  # третьему свободному места не хватило
        with SessionLocal() as db:
            counts = {}
            for a in db.query(Account).filter(Account.api_id.isnot(None)).all():
                counts[a.api_id] = counts.get(a.api_id, 0) + 1
            self.assertEqual(counts, {10: 2, 20: 2})  # оба пула теперь заполнены ровно до лимита

    def test_auto_distribute_uses_uploaded_unused_credentials(self):
        with SessionLocal() as db:
            ob.add_credentials(db, f"111:{'a'*32}")
            db.add(Account(identifier="free1"))
            db.commit()
            result = ob.auto_distribute(db)
        self.assertEqual(result["assigned"], [("free1", 111)])

    def test_auto_distribute_noop_without_pools(self):
        with SessionLocal() as db:
            db.add(Account(identifier="free1"))
            db.commit()
            result = ob.auto_distribute(db)
        self.assertEqual(result["assigned"], [])
        self.assertEqual(result["unassigned"], ["free1"])


class ApiRouterTests(unittest.TestCase):
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
            db.query(ApiCredential).delete()
            db.commit()
            set_protection(db, {"checking_preset": "custom", "api_pool_max_accounts": "2"})

    def test_upload_assign_and_delete_flow(self):
        r = self.client.post("/accounts/api/upload", data={"text": f"321:{'a'*32}:тест"})
        self.assertIn("Загружено приложений: 1", unquote_plus(r.headers["location"]))

        with SessionLocal() as db:
            db.add(Account(identifier="freeacc"))
            db.commit()
            acc_id = db.query(Account).filter_by(identifier="freeacc").one().id

        r = self.client.post(f"/accounts/api/{acc_id}/assign", data={"api_id": "321"})
        self.assertIn("назначен", unquote_plus(r.headers["location"]))
        with SessionLocal() as db:
            self.assertEqual(db.get(Account, acc_id).api_id, 321)

        # используется — удалить нельзя
        r = self.client.post("/accounts/api/321/delete")
        self.assertIn("используется", unquote_plus(r.headers["location"]))

        r = self.client.post("/accounts/api/upload", data={"text": f"654:{'b'*32}"})
        r = self.client.post("/accounts/api/654/delete")  # свободный — можно удалить
        self.assertIn("удалено", unquote_plus(r.headers["location"]))
        with SessionLocal() as db:
            self.assertIsNone(db.query(ApiCredential).filter_by(api_id=654).one_or_none())

    def test_upload_bad_text_reports_error(self):
        r = self.client.post("/accounts/api/upload", data={"text": "garbage"})
        self.assertIn("Строка 1", unquote_plus(r.headers["location"]))

    def test_assign_non_numeric_api_id(self):
        with SessionLocal() as db:
            db.add(Account(identifier="freeacc2"))
            db.commit()
            acc_id = db.query(Account).filter_by(identifier="freeacc2").one().id
        r = self.client.post(f"/accounts/api/{acc_id}/assign", data={"api_id": "xx"})
        self.assertIn("Некорректный", unquote_plus(r.headers["location"]))

    def test_auto_distribute_endpoint(self):
        with SessionLocal() as db:
            db.add(Account(identifier="owner", api_id=99, api_hash="c" * 32))
            db.add(Account(identifier="free1"))
            db.commit()
        r = self.client.post("/accounts/api/auto-distribute")
        self.assertIn("назначено", unquote_plus(r.headers["location"]))


class PoolPanelTests(unittest.TestCase):
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
            db.query(ApiCredential).delete()
            db.commit()
            set_protection(db, {"checking_preset": "custom", "api_pool_max_accounts": "2"})
            db.add_all([_acc("p1", 7777, "d" * 32), _acc("p2", 7777, "d" * 32)])  # пул уже полон (лимит 2)
            db.commit()

    def test_form_shows_pool_disabled_when_full(self):
        r = self.client.get("/accounts/add")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Пул 7777", r.text)
        self.assertIn("заполнен", r.text)

    def test_start_with_full_pool_returns_error(self):
        r = self.client.post("/accounts/add/start", data={
            "identifier": "p3", "phone": "+12223334455", "pool_api_id": "7777",
            "proxy": "socks5://9.9.9.3:1080",
        })
        self.assertEqual(r.status_code, 200)
        self.assertIn("лимит 2", r.text)

    def test_start_with_bad_pool_value(self):
        r = self.client.post("/accounts/add/start", data={
            "identifier": "p4", "phone": "+12223334455", "pool_api_id": "not-a-number",
            "proxy": "socks5://9.9.9.4:1080",
        })
        self.assertEqual(r.status_code, 200)
        self.assertIn("Некорректный выбор пула", r.text)

    def test_accounts_page_shows_pool_size(self):
        r = self.client.get("/accounts")
        self.assertIn("пул из 2", r.text)

    def test_accounts_page_shows_upload_card_and_assign_form_for_free_account(self):
        with SessionLocal() as db:
            db.add(Account(identifier="freeacc"))
            db.commit()
        r = self.client.get("/accounts")
        self.assertIn("Приложения (api_id): загрузка и распределение", r.text)
        self.assertIn("общий api_id (назначить)", r.text)
        self.assertIn("заполнен", r.text)           # пул 7777 уже на лимите (2/2) — вариант выбора отмечен как полный
        self.assertIn("Раскидать аккаунты без своего api_id", r.text)

    def test_settings_page_and_save_pool_limit(self):
        r = self.client.get("/settings")
        self.assertIn("Максимум аккаунтов на один api_id", r.text)
        ok = self.client.post("/settings/protection", data={"checking_preset": "custom", "api_pool_max_accounts": "8"})
        self.assertEqual(ok.status_code, 303)
        with SessionLocal() as db:
            self.assertEqual(get_protection(db)["api_pool_max_accounts"], 8)


if __name__ == "__main__":
    unittest.main()
