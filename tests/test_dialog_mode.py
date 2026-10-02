"""Страница «Диалоги»: режим «Общая» (одно правило: после N сообщений собеседника чат брошен на
время или навсегда) и «Выборочная» (как было: ручной лимит/пауза у каждого чата)."""
import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.auth import require_login
from app.database import SessionLocal
from app.main import app
from app.models import AccountBlacklist, ChatStatus, DialogMessage
from app.services import dialogs as dg
from app.services.settings_store import set_setting
from app.worker import telegram_worker as tw

from tests.test_worker_logic import WorkerBase, _event, _save


class _Ctx:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *a):
        return False


def _cleanup(account_id: int, keep_mode: bool = False):
    with SessionLocal() as db:
        for model in (DialogMessage, ChatStatus, AccountBlacklist):
            db.query(model).filter_by(account_id=account_id).delete()
        db.commit()
        if not keep_mode:
            set_setting(db, "dialogs_mode", "selective")


class GeneralModeWorkerTests(WorkerBase):
    def _worker(self):
        acc, w = self.make_worker()
        # номера аккаунтов в тестовой SQLite переиспользуются — чистим чужую историю и до, и после
        _cleanup(acc.id, keep_mode=True)
        self.addCleanup(_cleanup, acc.id)
        w.client = MagicMock()
        w.client.action = MagicMock(return_value=_Ctx())
        w.client.get_messages = AsyncMock(return_value=[])
        w.client.send_read_acknowledge = AsyncMock()
        w._pacer.next_delay = MagicMock(return_value=0.0)
        return acc, w

    async def _send(self, w, n_from, n_to):
        events = []
        with patch.object(tw, "generate_reply", AsyncMock(return_value="ответ")), \
                patch.object(tw, "typing_seconds", return_value=0.0):
            for i in range(n_from, n_to + 1):
                ev = _event(msg_id=i, text=f"сообщение {i}")
                await w._on_message(ev)
                events.append(ev)
        return events

    async def test_pause_after_n_user_messages_counts_only_client(self):
        _save()
        with SessionLocal() as db:
            dg.save_mode(db, "general", "3", "pause", "2", "hours")
        acc, w = self._worker()
        events = await self._send(w, 1, 4)
        self.assertEqual([e.respond.await_count for e in events], [1, 1, 1, 0])  # ответы бота не считаются
        with SessionLocal() as db:
            until = db.query(ChatStatus).filter_by(account_id=acc.id).one().paused_until
            self.assertAlmostEqual((until - dt.datetime.utcnow()).total_seconds(), 7200, delta=60)
            self.assertEqual(db.query(AccountBlacklist).filter_by(account_id=acc.id).count(), 0)
        more = await self._send(w, 5, 5)                                         # во время паузы — молчит
        self.assertEqual(more[0].respond.await_count, 0)

    async def test_forever_puts_chat_into_blacklist(self):
        _save()
        with SessionLocal() as db:
            dg.save_mode(db, "general", "2", "forever")
        acc, w = self._worker()
        events = await self._send(w, 1, 4)
        self.assertEqual([e.respond.await_count for e in events], [1, 1, 0, 0])
        with SessionLocal() as db:
            entry = db.query(AccountBlacklist).filter_by(account_id=acc.id, chat_id="111").one()
            self.assertIn("Общее правило", entry.note)

    async def test_selective_mode_ignores_general_rule(self):
        _save()
        with SessionLocal() as db:
            dg.save_mode(db, "general", "1", "forever")
            dg.save_mode(db, "selective")
        acc, w = self._worker()
        events = await self._send(w, 1, 3)
        self.assertEqual([e.respond.await_count for e in events], [1, 1, 1])


class ModeValidationTests(WorkerBase):
    def test_validation_and_pause_display(self):
        with SessionLocal() as db:
            for bad in (("general", "0", "pause", "1", "hours"), ("general", "5", "pause", "", "hours"),
                        ("general", "5", "maybe"), ("weird",), ("general", "5", "pause", "400", "days")):
                with self.assertRaises(dg.DialogOverrideError):
                    dg.save_mode(db, *bad)
            dg.save_mode(db, "general", "7", "pause", "3", "days")
            rule = dg.get_general_rule(db)
            self.assertEqual((rule.limit, rule.pause_minutes, rule.pause_display), (7, 4320, (3, "days")))
            dg.save_mode(db, "selective")
            self.assertEqual(dg.get_mode(db), "selective")


class PageTests(WorkerBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        app.dependency_overrides[require_login] = lambda: "admin"
        cls.client = TestClient(app, follow_redirects=False)

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.clear()

    def test_switch_mode_and_page_shows_matching_controls(self):
        acc, _ = self.make_worker()
        _cleanup(acc.id, keep_mode=True)
        self.addCleanup(_cleanup, acc.id)
        with SessionLocal() as db:
            db.add(DialogMessage(account_id=acc.id, chat_id="555", role="user", content="hi"))
            db.add(ChatStatus(account_id=acc.id, chat_id="555", status="inbound"))
            db.commit()
        html = self.client.get("/dialogs").text
        self.assertIn("Выборочная", html)
        self.assertIn('name="message_limit"', html)                                # выборочный режим — как было
        r = self.client.post("/dialogs/mode", data={"mode": "general", "limit": "5", "action": "forever"})
        self.assertEqual(r.status_code, 303)
        html = self.client.get("/dialogs").text
        self.assertIn("от собеседника: 1 из 5", html)
        self.assertNotIn('name="message_limit"', html)
        r = self.client.post("/dialogs/mode", data={"mode": "general", "limit": "abc", "action": "forever"})
        self.assertIn("msg=", r.headers["location"])
