"""Имя собеседника (ChatStatus.display_name) — только для отображения на страницах «Логи»/
«Диалоги»/«Чёрный список», чтобы голый chat_id можно было опознать глазами; ни на что в логике
автоответчика не влияет (см. docstring app/models.py и _sender_display_name в telegram_worker.py)."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.auth import require_login
from app.database import SessionLocal, init_db
from app.main import app
from app.models import Account, ChatStatus, DialogMessage
from app.services.chat_status import establish_status, get_status, set_display_name
from app.worker import telegram_worker as tw
from app.worker.telegram_worker import _sender_display_name
from tests.test_worker_logic import WorkerBase, _event, _save


def _sender(username=None, first_name=None, last_name=None, bot=False):
    return SimpleNamespace(username=username, first_name=first_name, last_name=last_name, bot=bot)


class ExtractionTests(unittest.TestCase):
    def test_prefers_username(self):
        ev = SimpleNamespace(sender=_sender(username="ivan_petrov", first_name="Иван", last_name="Петров"))
        self.assertEqual(_sender_display_name(ev), "@ivan_petrov")

    def test_falls_back_to_full_name(self):
        ev = SimpleNamespace(sender=_sender(first_name="Иван", last_name="Петров"))
        self.assertEqual(_sender_display_name(ev), "Иван Петров")

    def test_falls_back_to_first_name_only(self):
        ev = SimpleNamespace(sender=_sender(first_name="Иван"))
        self.assertEqual(_sender_display_name(ev), "Иван")

    def test_none_when_no_sender(self):
        ev = SimpleNamespace(sender=None)
        self.assertIsNone(_sender_display_name(ev))

    def test_none_when_sender_has_no_name_at_all(self):
        ev = SimpleNamespace(sender=_sender())
        self.assertIsNone(_sender_display_name(ev))

    def test_missing_sender_attribute_entirely(self):
        ev = SimpleNamespace()  # события без .sender вообще (не должно случаться, но не должно и падать)
        self.assertIsNone(_sender_display_name(ev))


class WorkerIntegrationTests(WorkerBase):
    def setUp(self):
        _save()

    def _ready(self):
        acc, w = self.make_worker()
        w.client = MagicMock()

        class _Ctx:
            async def __aenter__(self): return None
            async def __aexit__(self, *a): return False
        w.client.action = MagicMock(return_value=_Ctx())
        w._pacer.next_delay = MagicMock(return_value=0.0)
        self.addCleanup(lambda: self._wipe(acc.id))
        return acc, w

    @staticmethod
    def _wipe(account_id):
        with SessionLocal() as db:
            db.query(DialogMessage).filter_by(account_id=account_id).delete()
            db.query(ChatStatus).filter_by(account_id=account_id).delete()
            db.commit()

    async def _reply(self, w, msg_id, chat_id=111, sender=None):
        ev = _event(chat_id=chat_id, msg_id=msg_id)
        ev.sender = sender
        with patch.object(tw, "generate_reply", AsyncMock(return_value="ответ")), \
                patch.object(tw, "typing_seconds", return_value=0.0):
            return await w._process(ev)

    async def test_display_name_saved_from_first_message(self):
        acc, w = self._ready()
        await self._reply(w, 1, sender=_sender(username="client1"))
        with SessionLocal() as db:
            row = db.query(ChatStatus).filter_by(account_id=acc.id, chat_id="111").first()
        self.assertEqual(row.display_name, "@client1")

    async def test_missing_sender_does_not_block_reply_or_set_name(self):
        acc, w = self._ready()
        self.assertTrue(await self._reply(w, 1, sender=None))
        with SessionLocal() as db:
            row = db.query(ChatStatus).filter_by(account_id=acc.id, chat_id="111").first()
        self.assertIsNone(row.display_name)
        self.assertEqual(get_status(db, acc.id, "111"), "inbound")  # направление чата всё равно определилось

    async def test_later_message_without_sender_does_not_erase_known_name(self):
        acc, w = self._ready()
        await self._reply(w, 1, sender=_sender(username="client1"))
        await self._reply(w, 2, sender=None)  # догон/повтор без данных отправителя
        with SessionLocal() as db:
            row = db.query(ChatStatus).filter_by(account_id=acc.id, chat_id="111").first()
        self.assertEqual(row.display_name, "@client1")

    async def test_name_updates_if_changed(self):
        acc, w = self._ready()
        await self._reply(w, 1, sender=_sender(username="old_name"))
        await self._reply(w, 2, sender=_sender(username="new_name"))
        with SessionLocal() as db:
            row = db.query(ChatStatus).filter_by(account_id=acc.id, chat_id="111").first()
        self.assertEqual(row.display_name, "@new_name")

    async def test_full_name_used_when_no_username(self):
        acc, w = self._ready()
        await self._reply(w, 1, sender=_sender(first_name="Иван", last_name="Петров"))
        with SessionLocal() as db:
            row = db.query(ChatStatus).filter_by(account_id=acc.id, chat_id="111").first()
        self.assertEqual(row.display_name, "Иван Петров")


class LogsPageRouterTests(unittest.TestCase):
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
            acc = Account(identifier="logs_dn_acc", enabled=True)
            db.add(acc)
            db.commit()
            self.acc_id = acc.id
            establish_status(db, self.acc_id, "555", "inbound")
            db.add(DialogMessage(account_id=self.acc_id, chat_id="555", role="user", content="привет"))
            db.commit()

    def test_known_name_shown_next_to_chat_id(self):
        with SessionLocal() as db:
            set_display_name(db, self.acc_id, "555", "@ivan_petrov")
        page = self.client.get("/logs").text
        self.assertIn("@ivan_petrov", page)
        self.assertIn("555", page)

    def test_unknown_name_falls_back_to_bare_chat_id(self):
        page = self.client.get("/logs").text
        self.assertIn("555", page)


if __name__ == "__main__":
    unittest.main()
