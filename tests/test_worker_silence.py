"""«Сообщения в логах есть, а аккаунты не отвечают»: у каждого молчания должна быть видимая
причина (лог воркера + колонка «Что сделано» на странице «Логи»), а исключение или зависание
не должны навсегда глушить чат. Расписание считается в часовом поясе менеджера, а не сервера."""
import asyncio
import datetime as dt
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.database import SessionLocal
from app.models import Account, ChatStatus, DialogMessage
from app.services.schedule import schedule_now
from app.services.settings_store import set_schedule_settings, set_setting
from app.worker import telegram_worker as tw
from tests.test_worker_logic import WorkerBase, _event, _save


def _note_of(account_id, chat_id="111"):
    with SessionLocal() as db:
        row = (db.query(DialogMessage).filter_by(account_id=account_id, chat_id=chat_id, role="user")
               .order_by(DialogMessage.id.desc()).first())
        return row.note if row else None


class _Ctx:
    async def __aenter__(self): return None
    async def __aexit__(self, *a): return False


class SilenceReasonTests(WorkerBase):
    def setUp(self):
        _save()
        with SessionLocal() as db:
            set_setting(db, "global_enabled", "true")
            set_schedule_settings(db, False, [("09:00", "21:00")], False, [("13:00", "14:00")], False, "30")

    def _ready(self):
        acc, w = self.make_worker()
        w.client = MagicMock()
        w.client.action = MagicMock(return_value=_Ctx())
        w.client.get_messages = AsyncMock(return_value=[])  # чат новый -> первым написал клиент
        w.client.send_read_acknowledge = AsyncMock(return_value=True)
        w._pacer.next_delay = MagicMock(return_value=0.0)
        return acc, w

    async def _run(self, w, ev, reply="ответ"):
        with patch.object(tw, "generate_reply", AsyncMock(return_value=reply)), \
                patch.object(tw, "typing_seconds", return_value=0.0):
            return await w._process(ev)

    async def test_replied_message_is_marked_replied(self):
        acc, w = self._ready()
        self.assertTrue(await self._run(w, _event()))
        self.assertEqual(_note_of(acc.id), "отвечено")

    async def test_disabled_account_says_so(self):
        acc, w = self._ready()
        with SessionLocal() as db:
            db.get(Account, acc.id).enabled = False
            db.commit()
        self.assertFalse(await self._run(w, _event()))
        self.assertIn("выключен", _note_of(acc.id))

    async def test_chat_started_manually_says_so(self):
        acc, w = self._ready()
        with SessionLocal() as db:
            db.query(ChatStatus).filter_by(account_id=acc.id).delete()   # id аккаунта мог быть занят прежним тестом
            db.add(ChatStatus(account_id=acc.id, chat_id="111", status="outbound_manual"))
            db.commit()
        self.assertFalse(await self._run(w, _event()))
        self.assertIn("начал сам сотрудник", _note_of(acc.id))

    async def test_outside_working_hours_names_time_zone_and_the_fix(self):
        acc, w = self._ready()
        with SessionLocal() as db:
            set_schedule_settings(db, True, [("09:00", "21:00")], False, [], False, "30")
        # у VPS 02:00, а у менеджера в Москве полдень — окно 09–21 по серверному времени закрыто
        with patch.object(tw, "schedule_now", lambda tz: (dt.datetime(2026, 9, 27, 2, 0), "время сервера")):
            self.assertFalse(await self._run(w, _event()) or False)
        note = _note_of(acc.id)
        self.assertIn("вне рабочего времени", note)
        self.assertIn("02:00", note)
        self.assertIn("SCHEDULE_TIMEZONE", note)

    async def test_same_schedule_passes_when_manager_time_zone_is_used(self):
        acc, w = self._ready()
        with SessionLocal() as db:
            set_schedule_settings(db, True, [("09:00", "21:00")], False, [], False, "30")
        with patch.object(tw, "schedule_now", lambda tz: (dt.datetime(2026, 9, 27, 12, 0), "Europe/Moscow")):
            self.assertTrue(await self._run(w, _event()))
        self.assertEqual(_note_of(acc.id), "отвечено")

    async def test_llm_error_reason_is_attached_to_the_message(self):
        from app.services.llm_client import LLMError, http_error_text

        acc, w = self._ready()
        with patch.object(tw, "generate_reply", AsyncMock(side_effect=LLMError(http_error_text("DeepSeek", 401)))):
            await w._process(_event())
        self.assertIn("401", _note_of(acc.id))

    async def test_very_first_message_of_a_chat_with_id_zero_still_gets_a_reply(self):
        # найдено 100-аккаунтным нагрузочным тестом: _replied_upto по умолчанию 0 на чат без
        # истории — «ничего ещё не отвечено» неотличимо от «последний id, на который ответили,
        # это 0», и самое первое сообщение чата с event.id == 0 молча считалось «уже отвечено»
        # ещё в _on_message() (проверка ДО _process()) и пропускалось НАВСЕГДА (id больше не
        # растёт назад, повторно не придёт) — нужно гнать через _on_message(), не _process()
        # напрямую, иначе проверка, где и есть баг, вообще не вызывается
        acc, w = self._ready()
        with patch.object(tw, "generate_reply", AsyncMock(return_value="ответ")), \
                patch.object(tw, "typing_seconds", return_value=0.0):
            await w._on_message(_event(msg_id=0))
        self.assertEqual(_note_of(acc.id), "отвечено")

    async def test_paused_message_says_when_it_ends(self):
        acc, w = self._ready()
        w._paused_until = dt.datetime.utcnow() + dt.timedelta(minutes=10)
        await w._on_message(_event())
        self.assertIn("пауза автостопа", _note_of(acc.id))


class NoPermanentSilenceTests(WorkerBase):
    def setUp(self):
        _save()
        with SessionLocal() as db:
            set_setting(db, "global_enabled", "true")

    async def test_exception_is_reported_and_next_message_is_still_processed(self):
        acc, w = self.make_worker()
        calls = []

        async def flaky(event, chat_id):
            calls.append(event.id)
            if len(calls) == 1:
                raise ValueError("сломалась БД")

        w._handle_one = flaky
        await w._on_message(_event(msg_id=10))
        self.assertIn("внутренняя ошибка ValueError", _note_of(acc.id))
        with SessionLocal() as db:
            self.assertIn("сломалась БД", db.get(Account, acc.id).last_error)
        self.assertNotIn("111", w._busy)                       # чат не остался занятым
        await w._on_message(_event(msg_id=11))
        self.assertEqual(calls, [10, 11])                      # второе сообщение обработано

    async def test_hung_processing_releases_the_chat(self):
        acc, w = self.make_worker()

        async def hang(event, chat_id):
            await asyncio.sleep(60)

        w._handle_one = hang
        with patch.object(tw.AccountWorker, "HANDLE_TIMEOUT_SECONDS", 0.05):
            await w._on_message(_event(msg_id=20))
        self.assertIn("зависла", _note_of(acc.id))
        self.assertNotIn("111", w._busy)
        with SessionLocal() as db:
            self.assertIn("зависла", db.get(Account, acc.id).last_error)


class LogsPageTests(unittest.TestCase):
    def test_logs_page_shows_what_was_done_with_each_client_message(self):
        from fastapi.testclient import TestClient

        from app.auth import require_login
        from app.database import init_db
        from app.main import app

        init_db()
        with SessionLocal() as db:
            acc = Account(identifier="logs_page_acc", enabled=True)
            db.add(acc)
            db.commit()
            acc_id = acc.id
            db.add(DialogMessage(account_id=acc_id, chat_id="42", role="user", content="привет-логи",
                                 note="без ответа: вне рабочего времени по расписанию (сейчас 02:00)"))
            db.add(DialogMessage(account_id=acc_id, chat_id="43", role="user", content="второе", note="отвечено"))
            db.commit()
        try:
            app.dependency_overrides[require_login] = lambda: "admin"
            try:
                with TestClient(app) as client:
                    html = client.get("/logs").text
            finally:
                app.dependency_overrides.pop(require_login, None)
        finally:
            # см. test_database_wal.py — ROWID у SQLite переиспользуется после очистки accounts,
            # висящие DialogMessage сбивают счётчики в других тестовых файлах
            with SessionLocal() as db:
                db.query(DialogMessage).filter_by(account_id=acc_id).delete()
                db.query(Account).filter_by(id=acc_id).delete()
                db.commit()
        self.assertIn("Что сделано", html)
        self.assertIn("вне рабочего времени по расписанию", html)
        self.assertIn("text-success", html)     # «отвечено» — зелёным
        self.assertIn("text-warning", html)     # причина молчания — заметна


class ScheduleTimeZoneTests(unittest.TestCase):
    def test_no_zone_means_server_time(self):
        now, label = schedule_now(None)
        self.assertEqual(label, "время сервера")
        self.assertLess(abs((dt.datetime.now() - now).total_seconds()), 5)

    def test_named_zone_is_used(self):
        try:
            now, label = schedule_now("Europe/Moscow")
        except Exception:
            self.skipTest("нет базы часовых поясов (pip install tzdata)")
        if label != "Europe/Moscow":
            self.skipTest("нет базы часовых поясов (pip install tzdata)")
        expected = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=3)
        self.assertLess(abs((expected - now).total_seconds()), 5)

    def test_unknown_zone_falls_back_instead_of_breaking_replies(self):
        now, label = schedule_now("Mars/Olympus_Mons")
        self.assertEqual(label, "время сервера")


if __name__ == "__main__":
    unittest.main()
