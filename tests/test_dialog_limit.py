"""Лимит сообщений в одном чате (Настройки → Защита): после N сообщений (в обе стороны) в
ЭТОМ чате бот молчит заданное время, остальные диалоги аккаунта не затрагиваются; после паузы
счётчик чата начинается заново, а не сразу же снова упирается в лимит из-за скопившихся за
время паузы сообщений."""
import datetime as dt
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.database import SessionLocal
from app.models import Account, ChatStatus, DialogMessage
from app.services.chat_status import establish_status, get_pause_until, set_pause
from app.worker import telegram_worker as tw
from tests.test_worker_logic import WorkerBase, _event, _save


def _note_of(account_id, chat_id):
    with SessionLocal() as db:
        row = (db.query(DialogMessage).filter_by(account_id=account_id, chat_id=chat_id, role="user")
               .order_by(DialogMessage.id.desc()).first())
        return row.note if row else None


class ChatStatusPauseTests(WorkerBase):
    def test_pause_round_trip(self):
        with SessionLocal() as db:
            # без полной очистки Account: она сбрасывает ROWID SQLite, и переиспользованный
            # id может «унаследовать» чужие DialogMessage/ChatStatus из другого файла тестов
            # (см. такой же фикс в DialogLimitTests._ready)
            acc = Account(identifier="pause_acc", enabled=True)
            db.add(acc)
            db.commit()
            self.addCleanup(lambda: DialogLimitTests._wipe(acc.id))
            chat_id = f"pause{acc.id}"
            establish_status(db, acc.id, chat_id, "inbound")
            self.assertIsNone(get_pause_until(db, acc.id, chat_id))
            until = dt.datetime(2026, 10, 1, 12, 0)
            set_pause(db, acc.id, chat_id, until)
            self.assertEqual(get_pause_until(db, acc.id, chat_id), until)


class DialogLimitTests(WorkerBase):
    def setUp(self):
        _save(dialog_limit_enabled="on", dialog_message_limit="4", dialog_pause_minutes="60")

    def _ready(self):
        acc, w = self.make_worker()
        w.client = MagicMock()

        class _Ctx:
            async def __aenter__(self): return None
            async def __aexit__(self, *a): return False
        w.client.action = MagicMock(return_value=_Ctx())
        w._pacer.next_delay = MagicMock(return_value=0.0)
        # ChatStatus.paused_until и DialogMessage висят на числовом account_id — если не
        # убрать, при переиспользовании этого id (SQLite ROWID — просто max()+1, не настоящий
        # AUTOINCREMENT) чужой свежий аккаунт с тем же id ошибочно «унаследует» уже включённую
        # паузу этого чата (см. такой же фикс в test_niche.py/test_worker_silence.py). Отдельно
        # от этого — chat_id тоже делаем уникальным на основе account_id (не общий "111"),
        # чтобы точный подсчёт сообщений в чате не зависел от чужих строк с тем же chat_id,
        # оставшихся в общей тестовой БД от других файлов при таком же переиспользовании id.
        chat_id = f"limit{acc.id}"
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

    async def test_stops_after_limit_and_pauses_only_this_chat(self):
        acc, w, chat_id = self._ready()
        # лимит 4: 1 входящее + 1 ответ = 2 строки на обмен; после 2 обменов (4 строки) —
        # третий обмен уже не должен получить ответ
        self.assertTrue(await self._reply(w, 1, chat_id))
        self.assertTrue(await self._reply(w, 2, chat_id))
        self.assertFalse(await self._reply(w, 3, chat_id))
        note = _note_of(acc.id, chat_id)
        self.assertIn("достигнут лимит сообщений", note)
        self.assertIn("4", note)

        with SessionLocal() as db:
            paused = get_pause_until(db, acc.id, chat_id)
        self.assertIsNotNone(paused)
        self.assertGreater(paused, dt.datetime.utcnow())

    async def test_disabled_by_default_has_no_limit(self):
        _save()  # без dialog_limit_enabled — выключено
        acc, w, chat_id = self._ready()
        for i in range(1, 8):
            self.assertTrue(await self._reply(w, i, chat_id))  # 7 обменов, лимит бы сработал на 2-м

    async def test_backlog_during_pause_does_not_immediately_re_pause_after_it_ends(self):
        # регрессия: если считать ВСЕ сообщения чата без учёта момента прошлой паузы,
        # сообщения, скопившиеся, пока пауза шла, сразу же снова превышали бы лимит —
        # пауза включалась бы заново, ни разу не дав ответить после её окончания.
        # Времена — явные (не «через N секунд от теста»): пауза на час, начатая 2 часа назад,
        # уже 4 сообщения назад закончилась; 4 «забэклоченных» сообщения пришли за 90 минут
        # до текущего момента — то есть ВНУТРИ той старой паузы (за 30 минут до её конца).
        acc, w, chat_id = self._ready()
        await self._reply(w, 1, chat_id)
        await self._reply(w, 2, chat_id)
        self.assertFalse(await self._reply(w, 3, chat_id))  # лимит сработал, пауза началась (реальным временем)

        # Всю эту историю (сообщения 1-3 и саму паузу) сдвигаем на 3 часа в прошлое разом —
        # тогда получаем непротиворечивую картину: пауза началась 3 часа назад, длилась час
        # (значит закончилась 2 часа назад), а «забэклоченные» сообщения пришли за полчаса до
        # её конца, то есть ВНУТРИ той старой паузы. Реальный «сейчас» (msg8 ниже) оказывается
        # намного позже конца старой паузы, как и должно быть в жизни.
        shift = dt.timedelta(hours=3)
        with SessionLocal() as db:
            # обновляем построчно в Python, а не SQL-выражением в .update() — на SQLite дата
            # хранится строкой, и «колонка минус timedelta» в одном bulk UPDATE не read-modify-write
            for m in db.query(DialogMessage).filter_by(account_id=acc.id, chat_id=chat_id).all():
                m.created_at = m.created_at - shift
            row = db.query(ChatStatus).filter_by(account_id=acc.id, chat_id=chat_id).one()
            old_pause_end = row.paused_until - shift
            row.paused_until = old_pause_end
            backlog_time = old_pause_end - dt.timedelta(minutes=30)
            for i in range(4):
                db.add(DialogMessage(account_id=acc.id, chat_id=chat_id, role="user",
                                     content=f"backlog{i}", created_at=backlog_time))
            db.commit()

        self.assertTrue(await self._reply(w, 8, chat_id))  # отвечает, несмотря на скопившиеся 4 сообщения

    async def test_other_chats_of_the_same_account_are_not_affected(self):
        acc, w, chat_id = self._ready()
        await self._reply(w, 1, chat_id)
        await self._reply(w, 2, chat_id)
        self.assertFalse(await self._reply(w, 3, chat_id))  # этот чат на паузе

        other_chat_id = f"{chat_id}_other"
        self.assertTrue(await self._reply(w, 1, other_chat_id))  # другой чат того же аккаунта — не затронут


if __name__ == "__main__":
    unittest.main()
