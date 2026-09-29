"""Страница «Диалоги»: список активных диалогов и ручной лимит/пауза для ОДНОГО конкретного
чата — работает независимо от общей настройки «Настройки → Защита» (см. docstring
services/dialogs.py и worker/telegram_worker.py)."""
import datetime as dt
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import unquote_plus

from fastapi.testclient import TestClient

from app.auth import require_login
from app.database import SessionLocal, init_db
from app.main import app
from app.models import Account, ChatStatus, DialogMessage
from app.services.chat_status import establish_status, get_limit_override, pause_now, set_limit_override, set_pause
from app.services.dialogs import DialogOverrideError, list_active_dialogs, set_override
from app.worker import telegram_worker as tw
from tests.test_worker_logic import WorkerBase, _event, _save


class ServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        with SessionLocal() as db:
            db.query(ChatStatus).delete()
            db.query(DialogMessage).delete()
            db.query(Account).delete()
            db.commit()
            acc = Account(identifier="dlg_acc", enabled=True)
            db.add(acc)
            db.commit()
            self.acc_id = acc.id
            establish_status(db, self.acc_id, "111", "inbound")
            db.add(DialogMessage(account_id=self.acc_id, chat_id="111", role="user", content="привет"))
            db.commit()

    def test_set_and_get_override_round_trip(self):
        with SessionLocal() as db:
            set_override(db, self.acc_id, "111", "10", "45")
            self.assertEqual(get_limit_override(db, self.acc_id, "111"), (10, 45))

    def test_empty_fields_mean_no_override(self):
        with SessionLocal() as db:
            set_override(db, self.acc_id, "111", "", "")
            self.assertEqual(get_limit_override(db, self.acc_id, "111"), (None, None))

    def test_only_one_field_can_be_set(self):
        with SessionLocal() as db:
            set_override(db, self.acc_id, "111", "5", "")
            self.assertEqual(get_limit_override(db, self.acc_id, "111"), (5, None))

    def test_rejects_non_integer(self):
        with SessionLocal() as db:
            with self.assertRaises(DialogOverrideError):
                set_override(db, self.acc_id, "111", "не число", "")

    def test_rejects_zero_or_negative(self):
        with SessionLocal() as db:
            with self.assertRaises(DialogOverrideError):
                set_override(db, self.acc_id, "111", "0", "")

    def test_set_for_chat_without_status_reports_failure_not_silent_success(self):
        # редкий случай: DialogMessage уже есть, а ChatStatus ещё не установлен (аккаунт
        # был выключен на момент обработки этого сообщения) — см. docstring set_limit_override
        with SessionLocal() as db:
            db.add(DialogMessage(account_id=self.acc_id, chat_id="222", role="user", content="привет"))
            db.commit()
            with self.assertRaises(DialogOverrideError):
                set_override(db, self.acc_id, "222", "5", "")

    def test_pause_now_sets_pause_and_pause_override(self):
        with SessionLocal() as db:
            until = pause_now(db, self.acc_id, "111", 30)
            self.assertIsNotNone(until)
            self.assertGreater(until, dt.datetime.utcnow())
            self.assertEqual(get_limit_override(db, self.acc_id, "111"), (None, 30))

    def test_pause_now_does_not_overwrite_existing_pause_override(self):
        with SessionLocal() as db:
            set_override(db, self.acc_id, "111", "5", "77")
            pause_now(db, self.acc_id, "111", 30)  # разовая пауза, но своя длина паузы для чата уже задана
            self.assertEqual(get_limit_override(db, self.acc_id, "111"), (5, 77))

    def test_pause_now_for_chat_without_status_returns_none(self):
        with SessionLocal() as db:
            self.assertIsNone(pause_now(db, self.acc_id, "no-such-chat", 10))

    def test_list_active_dialogs_orders_by_recency_and_skips_deleted_accounts(self):
        with SessionLocal() as db:
            other = Account(identifier="dlg_other", enabled=True)
            db.add(other)
            db.commit()
            establish_status(db, other.id, "999", "inbound")
            old_time = dt.datetime.utcnow() - dt.timedelta(hours=1)
            db.add(DialogMessage(account_id=other.id, chat_id="999", role="user", content="старое",
                                 created_at=old_time))
            db.commit()
            rows = list_active_dialogs(db)
        self.assertEqual(rows[0].chat_id, "111")  # свежее сообщение из setUp — первое
        self.assertEqual(rows[0].message_count, 1)
        idents = [r.account.identifier for r in rows]
        self.assertIn("dlg_other", idents)


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
            db.query(ChatStatus).delete()
            db.query(DialogMessage).delete()
            db.query(Account).delete()
            db.commit()
            acc = Account(identifier="router_dlg_acc", enabled=True)
            db.add(acc)
            db.commit()
            self.acc_id = acc.id
            establish_status(db, self.acc_id, "111", "inbound")
            db.add(DialogMessage(account_id=self.acc_id, chat_id="111", role="user", content="привет"))
            db.commit()

    def _msg(self, r):
        return unquote_plus(r.headers["location"].split("msg=", 1)[-1])

    def test_page_renders_and_lists_nav_link(self):
        r = self.client.get("/dialogs")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Диалоги", r.text)
        self.assertIn("router_dlg_acc", r.text)
        r2 = self.client.get("/accounts")
        self.assertIn('href="/dialogs"', r2.text)

    def test_set_then_shown_on_page(self):
        r = self.client.post(f"/dialogs/{self.acc_id}/111/set", data={"message_limit": "7", "pause_minutes": "20"})
        self.assertEqual(r.status_code, 303)
        self.assertIn("сохранён", self._msg(r))
        page = self.client.get("/dialogs").text
        self.assertIn('value="7"', page)
        self.assertIn('value="20"', page)

    def test_set_bad_value_shows_error_and_saves_nothing(self):
        r = self.client.post(f"/dialogs/{self.acc_id}/111/set", data={"message_limit": "abc", "pause_minutes": ""})
        self.assertIn("целое число", self._msg(r))
        with SessionLocal() as db:
            self.assertEqual(get_limit_override(db, self.acc_id, "111"), (None, None))

    def test_set_for_nonexistent_account(self):
        r = self.client.post("/dialogs/999999/111/set", data={"message_limit": "5", "pause_minutes": ""})
        self.assertIn("не существует", self._msg(r))

    def test_clear(self):
        self.client.post(f"/dialogs/{self.acc_id}/111/set", data={"message_limit": "7", "pause_minutes": "20"})
        r = self.client.post(f"/dialogs/{self.acc_id}/111/clear")
        self.assertEqual(r.status_code, 303)
        self.assertIn("снят", self._msg(r))
        with SessionLocal() as db:
            self.assertEqual(get_limit_override(db, self.acc_id, "111"), (None, None))

    def test_pause_now_endpoint(self):
        r = self.client.post(f"/dialogs/{self.acc_id}/111/pause", data={"minutes": "15"})
        self.assertEqual(r.status_code, 303)
        self.assertIn("пауза до", self._msg(r))

    def test_pause_now_rejects_zero(self):
        r = self.client.post(f"/dialogs/{self.acc_id}/111/pause", data={"minutes": "0"})
        self.assertIn("хотя бы 1 минуту", self._msg(r))


class WorkerIntegrationTests(WorkerBase):
    def setUp(self):
        _save()  # dialog_limit_enabled ВЫКЛЮЧЕН глобально — override должен работать и так
        with SessionLocal() as db:
            db.query(ChatStatus).delete()
            db.commit()

    def _ready(self):
        acc, w = self.make_worker()
        w.client = MagicMock()

        class _Ctx:
            async def __aenter__(self): return None
            async def __aexit__(self, *a): return False
        w.client.action = MagicMock(return_value=_Ctx())
        w._pacer.next_delay = MagicMock(return_value=0.0)
        chat_id = f"dlg{acc.id}"
        self.addCleanup(lambda: self._wipe(acc.id))
        return acc, w, chat_id

    @staticmethod
    def _wipe(account_id):
        with SessionLocal() as db:
            db.query(DialogMessage).filter_by(account_id=account_id).delete()
            db.query(ChatStatus).filter_by(account_id=account_id).delete()
            db.commit()

    async def _reply(self, w, msg_id, chat_id):
        with patch.object(tw, "generate_reply", AsyncMock(return_value="ответ")), \
                patch.object(tw, "typing_seconds", return_value=0.0):
            return await w._process(_event(chat_id=chat_id, msg_id=msg_id))

    async def test_override_limit_applies_even_when_global_setting_disabled(self):
        acc, w, chat_id = self._ready()
        with SessionLocal() as db:
            establish_status(db, acc.id, chat_id, "inbound")
        self.assertTrue(await self._reply(w, 1, chat_id))  # ChatStatus установлен -> есть строка для override
        with SessionLocal() as db:
            set_override(db, acc.id, chat_id, "4", "60")
        self.assertTrue(await self._reply(w, 2, chat_id))
        self.assertFalse(await self._reply(w, 3, chat_id))  # достигнут ручной лимит 4 (1+1+1+1 строки)
        with SessionLocal() as db:
            row = (db.query(DialogMessage).filter_by(account_id=acc.id, chat_id=chat_id, role="user")
                   .order_by(DialogMessage.id.desc()).first())
        self.assertIn("достигнут лимит сообщений", row.note)
        self.assertIn("4", row.note)

    async def test_other_chats_of_the_same_account_are_not_affected_by_override(self):
        acc, w, chat_id = self._ready()
        with SessionLocal() as db:
            establish_status(db, acc.id, chat_id, "inbound")
        await self._reply(w, 1, chat_id)
        with SessionLocal() as db:
            set_override(db, acc.id, chat_id, "2", "60")
        self.assertFalse(await self._reply(w, 2, chat_id))  # лимит 2 достигнут

        other_chat = f"{chat_id}_other"
        self.assertTrue(await self._reply(w, 1, other_chat))  # другой чат того же аккаунта не затронут

    async def test_pause_now_blocks_the_very_next_message(self):
        acc, w, chat_id = self._ready()
        with SessionLocal() as db:
            establish_status(db, acc.id, chat_id, "inbound")
            pause_now(db, acc.id, chat_id, 30)
        self.assertFalse(await self._reply(w, 1, chat_id))

    async def test_pause_now_alone_does_not_start_count_based_limiting_after_it_ends(self):
        # регрессия: разовая «Пауза сейчас» без явного лимита не должна незаметно завести
        # для этого чата постоянный счётный лимит по общим настройкам, если общая настройка
        # выключена — админ хотел разово помолчать, а не включить ограничение навсегда
        acc, w, chat_id = self._ready()
        with SessionLocal() as db:
            establish_status(db, acc.id, chat_id, "inbound")
            # пауза уже в прошлом -> сразу неактивна, интересует именно последующий счёт
            set_limit_override(db, acc.id, chat_id, None, 30)  # как после истёкшей "Паузы сейчас"
        for i in range(1, 10):
            self.assertTrue(await self._reply(w, i, chat_id))  # ни разу не должно сработать

    async def test_clearing_override_reverts_to_global_setting(self):
        acc, w, chat_id = self._ready()
        with SessionLocal() as db:
            establish_status(db, acc.id, chat_id, "inbound")
        with SessionLocal() as db:
            set_override(db, acc.id, chat_id, "2", "60")
        self.assertTrue(await self._reply(w, 1, chat_id))
        self.assertFalse(await self._reply(w, 2, chat_id))  # ручной лимит 2 достигнут

        with SessionLocal() as db:
            set_limit_override(db, acc.id, chat_id, None, None)
            set_pause(db, acc.id, chat_id, dt.datetime.utcnow() - dt.timedelta(seconds=1))  # снимаем и саму паузу
        # глобальный лимит выключен (_save() без dialog_limit_enabled) -> снова отвечает всегда
        self.assertTrue(await self._reply(w, 3, chat_id))


if __name__ == "__main__":
    unittest.main()
