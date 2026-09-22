import contextlib
import datetime as dt
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from telethon.errors import FloodWaitError, PeerFloodError

from app.config import settings
from app.database import SessionLocal, init_db
from app.models import Account, DialogMessage
from app.services.settings_store import set_protection, set_setting
from app.worker import telegram_worker as tw


_COUNTER = 0


def _account(identifier, proxy="socks5://10.0.0.1:1080", **kw) -> Account:
    with SessionLocal() as db:
        acc = Account(identifier=identifier, enabled=True, proxy=proxy, system_prompt="ты ассистент",
                      session_path=str(settings.sessions_dir / f"{identifier}.session"), **kw)
        db.add(acc)
        db.commit()
        db.refresh(acc)
        db.expunge(acc)
        return acc


def _event(chat_id=111, msg_id=10, age_seconds=0, text="привет"):
    return SimpleNamespace(
        is_private=True, out=False, chat_id=chat_id, sender=None, id=msg_id, raw_text=text,
        date=dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=age_seconds),
        get_input_chat=AsyncMock(return_value="peer"), reply=AsyncMock(),
    )


def _save(**over):
    base = {"checking_preset": "custom", "reply_age_enabled": "on", "flood_stop_enabled": "on"}
    with SessionLocal() as db:
        set_protection(db, {**base, **over})


class WorkerBase(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        settings.sessions_dir.mkdir(parents=True, exist_ok=True)

    def make_worker(self, **kw):
        global _COUNTER
        _COUNTER += 1
        acc = _account(f"acc{_COUNTER}", **kw)
        return acc, tw.AccountWorker(acc)


class AgeAndPauseTests(WorkerBase):
    async def test_stale_skipped_but_logged_and_not_retried(self):
        _save(reply_age_minutes="30")
        acc, w = self.make_worker()
        w._handle_one = AsyncMock()
        await w._on_message(_event(age_seconds=3 * 3600))
        w._handle_one.assert_not_awaited()
        self.assertEqual(w._replied_upto["111"], 10)  # догон не будет дёргать этот чат снова
        with SessionLocal() as db:
            self.assertEqual(db.query(DialogMessage).filter_by(account_id=acc.id).count(), 1)

    async def test_fresh_message_processed_and_age_can_be_disabled(self):
        _save(reply_age_minutes="30")
        _, w = self.make_worker()
        w._handle_one = AsyncMock()
        await w._on_message(_event(age_seconds=60))
        w._handle_one.assert_awaited_once()

        _save(reply_age_enabled="")  # порог выключен: отвечаем и на трёхдневное
        _, w2 = self.make_worker()
        w2._handle_one = AsyncMock()
        await w2._on_message(_event(age_seconds=3 * 86400))
        w2._handle_one.assert_awaited_once()

    async def test_paused_account_logs_but_does_not_answer(self):
        _save()
        acc, w = self.make_worker()
        w._paused_until = dt.datetime.utcnow() + dt.timedelta(minutes=10)
        w._handle_one = AsyncMock()
        await w._on_message(_event())
        w._handle_one.assert_not_awaited()
        with SessionLocal() as db:
            self.assertEqual(db.query(DialogMessage).filter_by(account_id=acc.id).count(), 1)


class FloodTests(WorkerBase):
    async def test_flood_escalates_and_persists(self):
        _save(flood_extra_pause_seconds="30", flood_multiplier="2", flood_max_pause_seconds="86400")
        acc, w = self.make_worker()
        await w._register_flood(FloodWaitError(request=None, capture=100), "test")
        with SessionLocal() as db:
            a = db.get(Account, acc.id)
            first = (a.paused_until - dt.datetime.utcnow()).total_seconds()
            self.assertEqual(a.flood_streak, 1)
            self.assertIn("Автостоп", a.last_error)
        await w._register_flood(FloodWaitError(request=None, capture=100), "test")
        with SessionLocal() as db:
            a = db.get(Account, acc.id)
            second = (a.paused_until - dt.datetime.utcnow()).total_seconds()
            self.assertEqual(a.flood_streak, 2)
        self.assertGreaterEqual(first, 129)
        self.assertGreater(second, first * 1.6)  # рост при повторе
        self.assertTrue(w.is_paused())

    async def test_peer_flood_uses_long_pause(self):
        _save(peer_flood_pause_minutes="360", flood_max_pause_seconds="60")
        acc, w = self.make_worker()
        await w._register_flood(PeerFloodError(request=None), "send")
        with SessionLocal() as db:
            secs = (db.get(Account, acc.id).paused_until - dt.datetime.utcnow()).total_seconds()
        self.assertGreaterEqual(secs, 360 * 60 - 5)

    async def test_pause_expiry_clears_state(self):
        _save()
        acc, w = self.make_worker()
        await w._register_flood(FloodWaitError(request=None, capture=1), "t")
        w._paused_until = dt.datetime.utcnow() - dt.timedelta(seconds=1)
        self.assertFalse(w.is_paused())
        with SessionLocal() as db:
            a = db.get(Account, acc.id)
            self.assertIsNone(a.paused_until)
            self.assertIsNone(a.last_error)

    async def test_flood_on_send_pauses_without_retry(self):
        _save(reply_age_enabled="on")
        acc, w = self.make_worker()
        w.client = MagicMock()
        w.client.action = MagicMock(return_value=contextlib.nullcontext())
        # async-контекст поверх обычного nullcontext
        class _Ctx:
            async def __aenter__(self): return None
            async def __aexit__(self, *a): return False
        w.client.action = MagicMock(return_value=_Ctx())
        w._pacer.next_delay = MagicMock(return_value=0.0)
        ev = _event()
        ev.reply = AsyncMock(side_effect=FloodWaitError(request=None, capture=50))
        with patch.object(tw, "generate_reply", AsyncMock(return_value="ответ")), \
             patch.object(tw, "typing_seconds", return_value=0.0):
            result = await w._process(ev)
        self.assertFalse(result)
        self.assertEqual(ev.reply.await_count, 1)  # без повторной отправки
        self.assertTrue(w.is_paused())
        with SessionLocal() as db:  # ответа нет в истории — значит, будет обработано после паузы
            self.assertEqual(db.query(DialogMessage).filter_by(account_id=acc.id, role="assistant").count(), 0)

    async def test_successful_reply_flow(self):
        _save()
        acc, w = self.make_worker()
        w.client = MagicMock()

        class _Ctx:
            async def __aenter__(self): return None
            async def __aexit__(self, *a): return False
        w.client.action = MagicMock(return_value=_Ctx())
        w._pacer.next_delay = MagicMock(return_value=0.0)
        ev = _event()
        with patch.object(tw, "generate_reply", AsyncMock(return_value="ответ")), \
             patch.object(tw, "typing_seconds", return_value=0.0):
            result = await w._process(ev)
        self.assertTrue(result)
        ev.reply.assert_awaited_once()
        w._pacer.next_delay.assert_called_once()


class ReconcileTests(WorkerBase):
    async def test_no_proxy_never_started_and_paused_skipped(self):
        set_setting_ok = True
        with SessionLocal() as db:
            db.query(Account).delete()
            db.commit()
        for ident, proxy, paused in (("np", None, None), ("ok", "socks5://10.9.9.9:1080", None),
                                     ("pz", "socks5://10.9.9.8:1080", dt.datetime.utcnow() + dt.timedelta(hours=1))):
            acc = _account(ident, proxy=proxy, paused_until=paused)
            open(acc.session_path, "wb").close()  # файл сессии должен существовать

        started = []

        class FakeWorker:
            def __init__(self, account):
                self.account, self.dead, self.unhealthy = account, False, False
                self.client, self.disconnected_since = None, None
                self.session_path, self.proxy = account.session_path, account.proxy
                self.identity = tw.account_identity(account)

            async def start(self):
                started.append(self.account.identifier)

            async def stop(self):
                pass

        old = (settings.start_stagger_min_seconds, settings.start_stagger_max_seconds)
        settings.start_stagger_min_seconds = settings.start_stagger_max_seconds = 0
        try:
            with patch.object(tw, "AccountWorker", FakeWorker):
                mgr = tw.WorkerManager()
                await mgr._reconcile()
        finally:
            settings.start_stagger_min_seconds, settings.start_stagger_max_seconds = old
        self.assertEqual(started, ["ok"])
        with SessionLocal() as db:
            err = db.query(Account).filter_by(identifier="np").one().last_error
        self.assertIn("прокси", err.lower())

    async def test_start_without_proxy_is_refused_even_if_called_directly(self):
        _, w = self.make_worker(proxy=None)
        w._lock = MagicMock()
        with self.assertRaises(RuntimeError):
            await w._start_inner()


if __name__ == "__main__":
    unittest.main()
