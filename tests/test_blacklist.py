"""Чёрный список (страница «Чёрный список»): сервисная логика, роут CRUD и безусловная проверка
в воркере — заблокированный чат не получает ответа, пока запись не удалят вручную."""
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import unquote_plus

from fastapi.testclient import TestClient

from app.auth import require_login
from app.database import SessionLocal, init_db
from app.main import app
from app.models import Account, AccountBlacklist, DialogMessage
from app.services.blacklist import (
    BlacklistConfigError, accounts_with_blacklist, add_entry, delete_entry, is_blacklisted, list_blacklist,
)
from app.services.chat_status import establish_status, set_display_name
from app.worker import telegram_worker as tw
from tests.test_worker_logic import WorkerBase, _event, _save


class ServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        with SessionLocal() as db:
            db.query(AccountBlacklist).delete()
            db.query(Account).delete()
            db.commit()
            acc = Account(identifier="bl_acc", enabled=True)
            db.add(acc)
            db.commit()
            self.acc_id = acc.id

    def test_add_validates_and_persists(self):
        with SessionLocal() as db:
            e = add_entry(db, self.acc_id, "  12345  ", "  спамер  ")
            self.assertEqual(e.chat_id, "12345")  # обрезаны пробелы
            self.assertEqual(e.note, "спамер")
            self.assertEqual(len(list_blacklist(db, self.acc_id)), 1)
            self.assertTrue(is_blacklisted(db, self.acc_id, "12345"))

    def test_add_rejects_empty_chat_id(self):
        with SessionLocal() as db:
            with self.assertRaises(BlacklistConfigError):
                add_entry(db, self.acc_id, "   ", "заметка")
            self.assertEqual(list_blacklist(db, self.acc_id), [])

    def test_note_is_optional(self):
        with SessionLocal() as db:
            e = add_entry(db, self.acc_id, "111")
            self.assertIsNone(e.note)

    def test_adding_same_chat_twice_does_not_duplicate(self):
        with SessionLocal() as db:
            add_entry(db, self.acc_id, "111", "первая заметка")
            add_entry(db, self.acc_id, "111", "вторая заметка")
            self.assertEqual(len(list_blacklist(db, self.acc_id)), 1)

    def test_delete_removes_it(self):
        with SessionLocal() as db:
            e = add_entry(db, self.acc_id, "111")
            delete_entry(db, e.id)
            self.assertEqual(list_blacklist(db, self.acc_id), [])
            self.assertFalse(is_blacklisted(db, self.acc_id, "111"))

    def test_delete_nonexistent_id_does_not_raise(self):
        with SessionLocal() as db:
            delete_entry(db, 999999)  # не должно бросать

    def test_add_to_nonexistent_account_is_refused_not_orphaned(self):
        with SessionLocal() as db:
            with self.assertRaises(BlacklistConfigError) as cm:
                add_entry(db, 999999, "111")
        self.assertIn("не существует", str(cm.exception))
        with SessionLocal() as db:
            self.assertEqual(db.query(AccountBlacklist).filter_by(account_id=999999).count(), 0)

    def test_is_blacklisted_scoped_per_account_not_global(self):
        with SessionLocal() as db:
            other = Account(identifier="bl_other", enabled=True)
            db.add(other)
            db.commit()
            add_entry(db, self.acc_id, "111")
            self.assertTrue(is_blacklisted(db, self.acc_id, "111"))
            self.assertFalse(is_blacklisted(db, other.id, "111"))  # тот же chat_id, другой аккаунт

    def test_accounts_with_blacklist_groups_by_account_including_empty(self):
        with SessionLocal() as db:
            db.add(Account(identifier="bl_empty", enabled=True))
            db.commit()
            add_entry(db, self.acc_id, "111")
            rows = accounts_with_blacklist(db)
        by_ident = {a.identifier: es for a, es in rows}
        self.assertEqual(len(by_ident["bl_acc"]), 1)
        self.assertEqual(by_ident["bl_empty"], [])


class RouterTests(unittest.TestCase):
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
            db.query(AccountBlacklist).delete()
            db.query(Account).delete()
            db.commit()
            acc = Account(identifier="router_bl_acc", enabled=True)
            db.add(acc)
            db.commit()
            self.acc_id = acc.id

    def _msg(self, r):
        return unquote_plus(r.headers["location"].split("msg=", 1)[-1])

    def test_page_renders_and_lists_nav_link(self):
        r = self.client.get("/blacklist")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Чёрный список", r.text)
        self.assertIn("router_bl_acc", r.text)
        r2 = self.client.get("/accounts")
        self.assertIn('href="/blacklist"', r2.text)

    def test_display_name_shown_next_to_chat_id(self):
        with SessionLocal() as db:
            establish_status(db, self.acc_id, "12345", "inbound")
            set_display_name(db, self.acc_id, "12345", "@ivan_petrov")
        self.client.post(f"/blacklist/{self.acc_id}/add", data={"chat_id": "12345", "note": ""})
        page = self.client.get("/blacklist").text
        self.assertIn("@ivan_petrov", page)
        self.assertIn("12345", page)

    def test_add_then_shown_on_page(self):
        r = self.client.post(f"/blacklist/{self.acc_id}/add", data={"chat_id": "12345", "note": "спамер"})
        self.assertEqual(r.status_code, 303)
        self.assertIn("добавлен", self._msg(r))
        page = self.client.get("/blacklist").text
        self.assertIn("12345", page)
        self.assertIn("спамер", page)

    def test_add_empty_chat_id_shows_error_and_saves_nothing(self):
        r = self.client.post(f"/blacklist/{self.acc_id}/add", data={"chat_id": "   ", "note": ""})
        self.assertIn("chat_id", self._msg(r))
        with SessionLocal() as db:
            self.assertEqual(list_blacklist(db, self.acc_id), [])

    def test_delete(self):
        with SessionLocal() as db:
            e = add_entry(db, self.acc_id, "111")
            entry_id = e.id
        r = self.client.post(f"/blacklist/{entry_id}/delete")
        self.assertEqual(r.status_code, 303)
        self.assertIn("удалена", self._msg(r))
        with SessionLocal() as db:
            self.assertEqual(list_blacklist(db, self.acc_id), [])

    def test_add_to_deleted_account_shows_error_via_http(self):
        r = self.client.post("/blacklist/999999/add", data={"chat_id": "111", "note": ""})
        self.assertIn("не существует", self._msg(r))

    def test_deleting_account_removes_its_blacklist_too(self):
        with SessionLocal() as db:
            add_entry(db, self.acc_id, "111")
        r = self.client.post(f"/accounts/{self.acc_id}/delete")
        self.assertEqual(r.status_code, 303)
        with SessionLocal() as db:
            self.assertEqual(db.query(AccountBlacklist).filter_by(account_id=self.acc_id).count(), 0)

    def test_special_characters_render_escaped_not_executable(self):
        with SessionLocal() as db:
            add_entry(db, self.acc_id, "111", '<b>жирный</b> "кавычки"')
        html = self.client.get("/blacklist").text
        self.assertNotIn("<b>жирный</b>", html)
        self.assertIn("&lt;b&gt;жирный&lt;/b&gt;", html)

    def test_quick_add_button_present_on_logs_page_for_client_messages(self):
        with SessionLocal() as db:
            db.add(DialogMessage(account_id=self.acc_id, chat_id="555", role="user", content="привет"))
            db.commit()
        html = self.client.get("/logs").text
        self.assertIn(f'/blacklist/{self.acc_id}/add', html)
        self.assertIn('value="555"', html)


class WorkerIntegrationTests(WorkerBase):
    def setUp(self):
        _save()
        with SessionLocal() as db:
            db.query(AccountBlacklist).delete()
            db.commit()

    def _ready(self):
        acc, w = self.make_worker()
        w.client = MagicMock()

        class _Ctx:
            async def __aenter__(self): return None
            async def __aexit__(self, *a): return False
        w.client.action = MagicMock(return_value=_Ctx())
        w._pacer.next_delay = MagicMock(return_value=0.0)
        # DialogMessage/AccountBlacklist висят на числовом account_id — см. такой же фикс в
        # test_niche.py/test_dialog_limit.py (ROWID SQLite переиспользуется после полной
        # очистки accounts в другом тестовом файле).
        self.addCleanup(lambda: self._wipe(acc.id))
        return acc, w

    @staticmethod
    def _wipe(account_id):
        with SessionLocal() as db:
            db.query(DialogMessage).filter_by(account_id=account_id).delete()
            db.query(AccountBlacklist).filter_by(account_id=account_id).delete()
            db.commit()

    async def _reply(self, w, msg_id, chat_id=111):
        with patch.object(tw, "generate_reply", AsyncMock(return_value="ответ")), \
                patch.object(tw, "typing_seconds", return_value=0.0):
            return await w._process(_event(chat_id=chat_id, msg_id=msg_id))

    async def test_blacklisted_chat_gets_no_reply(self):
        acc, w = self._ready()
        with SessionLocal() as db:
            add_entry(db, acc.id, "111")
        self.assertFalse(await self._reply(w, 1))
        with SessionLocal() as db:
            row = (db.query(DialogMessage).filter_by(account_id=acc.id, chat_id="111", role="user")
                   .order_by(DialogMessage.id.desc()).first())
        self.assertIn("чёрном списке", row.note)

    async def test_other_chats_of_the_same_account_are_not_affected(self):
        acc, w = self._ready()
        with SessionLocal() as db:
            add_entry(db, acc.id, "111")
        self.assertFalse(await self._reply(w, 1, chat_id=111))
        self.assertTrue(await self._reply(w, 1, chat_id=222))

    async def test_not_blacklisted_gets_normal_reply(self):
        acc, w = self._ready()
        self.assertTrue(await self._reply(w, 1))

    async def test_removing_from_blacklist_restores_replies(self):
        acc, w = self._ready()
        with SessionLocal() as db:
            entry_id = add_entry(db, acc.id, "111").id
        self.assertFalse(await self._reply(w, 1))
        with SessionLocal() as db:
            delete_entry(db, entry_id)
        self.assertTrue(await self._reply(w, 2))


if __name__ == "__main__":
    unittest.main()
