import asyncio
import datetime as dt
import logging
import random
import time
from pathlib import Path

from telethon import TelegramClient, events
from telethon.tl import functions
from telethon.errors import (
    AuthKeyDuplicatedError,
    AuthKeyUnregisteredError,
    FloodWaitError,
    PeerFloodError,
    RPCError,
    SessionExpiredError,
    SessionRevokedError,
    UserDeactivatedBanError,
    UserDeactivatedError,
)

from ..config import settings
from ..database import SessionLocal
from ..models import Account, DialogMessage
from ..services.device_profile import client_kwargs as profile_client_kwargs
from ..services.disclaimer import build_disclaimer
from ..services.flood_guard import compute_pause, next_streak
from ..services.knowledge_base import load_knowledge_base, load_prompt_template
from ..services.llm_client import LLMError, generate_reply
from ..services.chat_status import INBOUND, OUTBOUND_MANUAL
from ..services.chat_status import establish_status as establish_chat_status
from ..services.chat_status import get_status as get_chat_status
from ..services.pacing import SessionPacer, jittered, typing_seconds
from ..services.proxy import ProxyConfigError, choose_ip_family, parse_proxy
from ..services.schedule import is_within_work_hours
from ..services.session_lock import SessionInUseError, SessionLock
from ..services.settings_store import (
    get_llm_settings,
    get_protection,
    get_schedule_settings,
    is_global_enabled,
    is_kb_enabled,
    is_prompt_enabled,
)

logger = logging.getLogger(__name__)

# Необратимые для сессии ошибки: повторять запросы бессмысленно и только ухудшает
# положение (лишний трафик с мёртвым ключом), аккаунт нужно снять с работы.
FATAL_ERRORS = (
    AuthKeyDuplicatedError,
    AuthKeyUnregisteredError,
    SessionRevokedError,
    SessionExpiredError,
    UserDeactivatedError,
    UserDeactivatedBanError,
)

# Сигналы «Telegram просит притормозить»: аккаунт целиком уходит на паузу (автостоп).
FLOOD_ERRORS = (FloodWaitError, PeerFloodError)

_FATAL_TEXT = {
    AuthKeyDuplicatedError: "Ключ сессии аннулирован Telegram (использован одновременно с двух мест). Загрузите новую TData/сессию.",
    AuthKeyUnregisteredError: "Ключ сессии больше не действителен (вход завершён/сессия удалена). Загрузите новую TData/сессию.",
    SessionRevokedError: "Сессия завершена владельцем аккаунта. Загрузите новую TData/сессию.",
    SessionExpiredError: "Сессия истекла. Загрузите новую TData/сессию.",
    UserDeactivatedError: "Аккаунт деактивирован Telegram.",
    UserDeactivatedBanError: "Аккаунт заблокирован Telegram.",
}


def fatal_text(exc: BaseException) -> str:
    for cls, text in _FATAL_TEXT.items():
        if isinstance(exc, cls):
            return text
    return str(exc)


TELEGRAM_SERVICE_ID = "777000"


class NotAuthorizedError(RuntimeError):
    pass


def _session_dc_id(session_path: str) -> int:
    """dc_id из файла сессии без подключения к Telegram."""
    import sqlite3

    try:
        with sqlite3.connect(session_path) as con:
            row = con.execute("SELECT dc_id FROM sessions").fetchone()
        return int(row[0]) if row else 2
    except (sqlite3.Error, ValueError, TypeError):
        return 2


def _dialog_recently_active(db, account_id: int, chat_id: str, minutes: int) -> bool:
    """True, если ИИ уже отвечал в этом чате за последние `minutes` минут —
    используется, чтобы не обрывать диалог с клиентом на полуслове ровно в
    момент окончания рабочих часов/начала перерыва."""
    last_reply = (
        db.query(DialogMessage)
        .filter_by(account_id=account_id, chat_id=chat_id, role="assistant")
        .order_by(DialogMessage.created_at.desc())
        .first()
    )
    if last_reply is None:
        return False
    return dt.datetime.utcnow() - last_reply.created_at <= dt.timedelta(minutes=minutes)


def account_identity(account: Account) -> tuple:
    """Всё, что определяет «личность» подключения: смена любого поля — перезапуск клиента."""
    return (
        account.session_path, account.proxy, account.api_id, account.api_hash,
        account.device_model, account.system_version, account.app_version,
        account.lang_code, account.system_lang_code,
    )


class AccountWorker:
    """Управляет одним Telethon-клиентом (один менеджерский аккаунт)."""

    def __init__(self, account: Account):
        self.account_id = account.id
        self.session_path = account.session_path
        self.proxy = account.proxy
        self.identity = account_identity(account)
        # свои api_id/hash аккаунта; у «старых» аккаунтов — общие из .env
        self._api_id = account.api_id or settings.telegram_api_id
        self._api_hash = account.api_hash or settings.telegram_api_hash
        self._own_api = bool(account.api_id and account.api_hash)
        self._client_kwargs = profile_client_kwargs(account)
        self._paused_until: dt.datetime | None = account.paused_until
        self.client: TelegramClient | None = None
        self._run_task: asyncio.Task | None = None
        self._catch_up_task: asyncio.Task | None = None
        self._poll_task: asyncio.Task | None = None
        self._ping_fails = 0
        self.unhealthy = False
        # chat_id -> id последнего обработанного сообщения (ответили ИЛИ осознанно пропустили
        # как устаревшее): одно сообщение может прийти дважды (живое событие + догон).
        self._replied_upto: dict[str, int] = {}
        self.dead = False
        self.disconnected_since: float | None = None
        self._lock = SessionLock(self.session_path)
        self._busy: set[str] = set()
        self._queued: dict[str, object] = {}  # chat_id -> самое свежее сообщение, пришедшее, пока чат занят
        self._logged: set[tuple[str, int]] = set()
        self._pacer = SessionPacer()

    # ------------------------------------------------------------------ запуск/остановка
    async def start(self):
        self._lock.acquire()
        try:
            await self._start_inner()
        except BaseException:
            if self.client:
                try:
                    await self.client.disconnect()
                except Exception:
                    pass
            self._lock.release()
            raise

    async def _start_inner(self):
        try:
            proxy = parse_proxy(self.proxy)
        except ProxyConfigError as exc:
            raise RuntimeError(f"Некорректный прокси: {exc}") from exc
        if proxy is None and settings.require_proxy:
            # последняя линия обороны: ни одного соединения с IP сервера
            raise RuntimeError("Прокси обязателен — аккаунт без прокси не запускается")

        use_ipv6, dc_address = False, None
        if proxy:
            # Часть прокси выходит в сеть только по IPv6 и не соединяет с IPv4-адресами
            # Telegram — выбираем семейство адресов, через которое прокси реально работает.
            dc_id = _session_dc_id(self.session_path)
            use_ipv6, dc_address = await asyncio.to_thread(choose_ip_family, proxy, dc_id)
            if dc_address is None:
                raise ConnectionError("Прокси не пропускает Telegram ни по IPv4, ни по IPv6")

        if not self._own_api:
            logger.warning("Account %s: используется ОБЩИЙ api_id из .env (нет собственного приложения)", self.account_id)

        self.client = TelegramClient(
            self.session_path, self._api_id, self._api_hash, proxy=proxy,
            catch_up=True, use_ipv6=use_ipv6,
            # 0 = FloodWait никогда не «пережидается» внутри Telethon молча: он всегда доходит
            # до нашего автостопа, где пауза считается по настройкам панели.
            flood_sleep_threshold=0,
            base_logger=logging.getLogger(f"tg.acc{self.account_id}"),  # в логе видно, чей это сокет
            **self._client_kwargs,
        )
        if use_ipv6:
            self.client.session.set_dc(self.client.session.dc_id, dc_address, 443)
            logger.info("Account %s: proxy is IPv6-only, connecting to DC%s via IPv6",
                        self.account_id, self.client.session.dc_id)
        self.client.add_event_handler(self._on_message, events.NewMessage(incoming=True))
        self.client.add_event_handler(self._on_outgoing, events.NewMessage(outgoing=True))
        await self.client.connect()

        if not await self.client.is_user_authorized():
            await self.client.disconnect()
            raise NotAuthorizedError(
                "Сессия не авторизована в Telegram. Добавьте аккаунт заново через мастер "
                "«Добавить аккаунт» — сервер не может интерактивно ввести код."
            )

        me = await self.client.get_me()
        with SessionLocal() as db:
            acc = db.get(Account, self.account_id)
            if acc:
                acc.is_authorized = True
                if not (acc.last_error or "").startswith("Автостоп"):
                    acc.last_error = None
                if me and me.phone:
                    acc.phone = f"+{me.phone}"
                db.commit()

        who = f"{me.first_name or ''} {me.last_name or ''}".strip() if me else "?"
        logger.info("Account %s: client started as +%s (%s), tg_id=%s", self.account_id,
                    getattr(me, "phone", "?"), who, getattr(me, "id", "?"))
        self._run_task = asyncio.create_task(self.client.run_until_disconnected())
        self._run_task.add_done_callback(self._on_run_finished)
        self._catch_up_task = asyncio.create_task(self._catch_up_loop())
        self._poll_task = asyncio.create_task(self._poll_updates_loop())

    def _on_run_finished(self, task: asyncio.Task):
        if task.cancelled():
            return
        exc = task.exception()
        if isinstance(exc, FATAL_ERRORS):
            self.mark_dead(fatal_text(exc))

    async def stop(self):
        if self._catch_up_task:
            self._catch_up_task.cancel()
        if self._poll_task:
            self._poll_task.cancel()
        if self.client:
            await self.client.disconnect()
        if self._run_task:
            self._run_task.cancel()
        self._lock.release()
        logger.info("Account %s: client stopped", self.account_id)

    # ------------------------------------------------------------------ автостоп при флуде
    def is_paused(self) -> bool:
        if self._paused_until is None:
            return False
        if dt.datetime.utcnow() < self._paused_until:
            return True
        # пауза закончилась — снимаем отметку и в БД
        self._paused_until = None
        with SessionLocal() as db:
            acc = db.get(Account, self.account_id)
            if acc:
                acc.paused_until = None
                if (acc.last_error or "").startswith("Автостоп"):
                    acc.last_error = None
                db.commit()
        logger.info("Account %s: пауза автостопа закончилась, работа возобновлена", self.account_id)
        return False

    async def _register_flood(self, exc: BaseException, where: str) -> None:
        """Аккаунт целиком уходит на паузу: расчёт по настройкам панели, серия растёт при
        повторах. Клиенты подождут — сохранность аккаунта важнее скорости ответа."""
        peer_flood = isinstance(exc, PeerFloodError)
        wait = getattr(exc, "seconds", 0) or 0
        now = dt.datetime.utcnow()
        with SessionLocal() as db:
            cfg = get_protection(db)
            acc = db.get(Account, self.account_id)
            if acc is None:
                return
            streak = next_streak(acc.flood_streak or 0, acc.flood_last_at, now, cfg["flood_reset_hours"])
            pause = compute_pause(streak, wait, cfg, peer_flood=peer_flood)
            until = now + dt.timedelta(seconds=pause)
            acc.flood_streak, acc.flood_last_at, acc.paused_until = streak, now, until
            acc.last_error = (f"Автостоп: {type(exc).__name__} ({where}), серия {streak}, "
                              f"пауза {int(pause)} с (до {until:%H:%M:%S} UTC)")
            db.commit()
        self._paused_until = until
        logger.warning("Account %s: %s in %s (требует %ss) -> автостоп, серия %s, пауза %.0f с",
                       self.account_id, type(exc).__name__, where, wait, streak, pause)

    # ------------------------------------------------------------------ фоновые проверки
    async def _poll_updates_loop(self):
        while True:
            with SessionLocal() as db:
                cfg = get_protection(db)
            await asyncio.sleep(jittered(cfg["poll_interval_seconds"], cfg["poll_jitter_pct"]))
            if self.is_paused():
                continue  # во время паузы аккаунт ничего не запрашивает
            try:
                await self.client.catch_up()
                # Реальный запрос с ответом: если через прокси/сеть соединение «висит» и
                # обновления не доходят, это будет видно сразу, а не по молчанию аккаунта.
                await asyncio.wait_for(self.client(functions.updates.GetStateRequest()), timeout=25)
                if self._ping_fails:
                    logger.info("Account %s: connection recovered", self.account_id)
                self._ping_fails = 0
            except asyncio.CancelledError:
                raise
            except FATAL_ERRORS as exc:
                self.mark_dead(fatal_text(exc))
                return
            except FLOOD_ERRORS as exc:
                await self._register_flood(exc, "poll")
            except Exception as exc:
                self._ping_fails += 1
                logger.warning("Account %s: connection check failed (%s in a row): %s",
                               self.account_id, self._ping_fails, type(exc).__name__)
                if self._ping_fails >= 4:
                    logger.error("Account %s: connection is unhealthy, client will be restarted", self.account_id)
                    self.unhealthy = True
                    return

    async def _catch_up_loop(self):
        # Разводим аккаунты по времени, чтобы проверки не шли залпом.
        await asyncio.sleep(random.uniform(3, 15))
        while True:
            with SessionLocal() as db:
                cfg = get_protection(db)
            try:
                if not self.is_paused():
                    await self._catch_up_unread()
            except asyncio.CancelledError:
                raise
            except FATAL_ERRORS as exc:
                self.mark_dead(fatal_text(exc))
                return
            except FLOOD_ERRORS as exc:
                await self._register_flood(exc, "catch-up")
            except Exception:
                logger.exception("Account %s: catch-up of unread messages failed", self.account_id)
            await asyncio.sleep(jittered(cfg["catchup_interval_seconds"], cfg["poll_jitter_pct"]))

    async def _catch_up_unread(self):
        """Отвечает на входящие, которые накопились, пока воркер был выключен
        (или которые пропустили live-события). Успешно обработанный чат
        помечается прочитанным, поэтому повторный проход его не трогает."""
        # Только недавние диалоги: полный обход всех чатов каждой минуты × десятки
        # аккаунтов — лишняя нагрузка на API, которая сама по себе привлекает внимание.
        async for dialog in self.client.iter_dialogs(limit=settings.catch_up_dialogs_limit):
            if self.is_paused():
                return
            if not dialog.is_user or not dialog.unread_count:
                continue
            if str(dialog.id) == TELEGRAM_SERVICE_ID or getattr(dialog.entity, "bot", False):
                continue
            last_id = getattr(dialog.message, "id", 0) or 0
            if last_id and last_id <= self._replied_upto.get(str(dialog.id), 0):
                continue  # уже обработано (ответили или пропустили как устаревшее) — без лишних запросов
            unread = [
                m async for m in self.client.iter_messages(dialog.entity, limit=dialog.unread_count)
                if not m.out
            ]
            if not unread:
                continue
            unread.reverse()
            last = unread[-1]
            with SessionLocal() as db:
                for m in unread[:-1]:
                    key = (str(m.chat_id), m.id)
                    if m.raw_text and key not in self._logged:
                        self._logged.add(key)
                        db.add(DialogMessage(
                            account_id=self.account_id, chat_id=str(m.chat_id), role="user", content=m.raw_text
                        ))
                db.commit()
            await self._on_message(last)

    # ------------------------------------------------------------------ входящие
    def _log_incoming(self, event) -> None:
        key = (str(event.chat_id), event.id)
        if key in self._logged:
            return
        self._logged.add(key)
        with SessionLocal() as db:
            db.add(DialogMessage(account_id=self.account_id, chat_id=key[0], role="user",
                                 content=event.raw_text or ""))
            db.commit()

    @staticmethod
    def _age_seconds(event) -> float | None:
        date = getattr(event, "date", None)
        if date is None:
            return None
        if date.tzinfo is None:
            date = date.replace(tzinfo=dt.timezone.utc)
        return (dt.datetime.now(dt.timezone.utc) - date).total_seconds()

    async def _on_message(self, event):
        if not event.is_private or event.out:
            return

        chat_id = str(event.chat_id)
        # Служебный аккаунт Telegram (коды входа, «новый вход в аккаунт») и боты — не собеседники.
        sender = event.sender
        if chat_id == TELEGRAM_SERVICE_ID or (sender is not None and getattr(sender, "bot", False)):
            return
        if event.id <= self._replied_upto.get(chat_id, 0):
            return
        logger.info("Account %s: incoming message from chat %s", self.account_id, chat_id)

        # Сообщение всегда попадает в «Логи» — даже если ответа не будет (пауза, возраст, расписание).
        self._log_incoming(event)

        if self.is_paused():
            return  # автостоп: клиент подождёт, аккаунт важнее; ответим после паузы (если не устареет)

        with SessionLocal() as db:
            cfg = get_protection(db)
        if cfg["reply_age_enabled"]:
            age = self._age_seconds(event)
            if age is not None and age > cfg["reply_age_minutes"] * 60:
                self._replied_upto[chat_id] = max(event.id, self._replied_upto.get(chat_id, 0))
                logger.info("Account %s: сообщение в чате %s старше порога (%d мин) — без ответа",
                            self.account_id, chat_id, cfg["reply_age_minutes"])
                return

        if chat_id in self._busy:
            self._queued[chat_id] = event  # ответим, когда закончим текущий (с учётом медленного темпа)
            return
        self._busy.add(chat_id)
        try:
            while event is not None:
                await self._handle_one(event, chat_id)
                event = self._queued.pop(chat_id, None)
                if event is not None and (self.is_paused() or event.id <= self._replied_upto.get(chat_id, 0)):
                    event = None
        finally:
            self._busy.discard(chat_id)
            self._queued.pop(chat_id, None)

    async def _handle_one(self, event, chat_id: str):
        # Прежде чем отвечать, убеждаемся, кто написал в этом чате первым (см. ниже).
        # Не удалось определить -> не отвечаем сейчас (безопаснее, чем вмешаться в
        # диалог сотрудника); статус не записан, поэтому следующая попытка повторит проверку.
        if await self._resolve_chat_status(event) is None:
            return
        replied = await self._process(event)
        if replied:
            self._replied_upto[chat_id] = max(event.id, self._replied_upto.get(chat_id, 0))
            try:
                await self.client.send_read_acknowledge(await event.get_input_chat())
            except FLOOD_ERRORS as exc:
                await self._register_flood(exc, "read-ack")
            except Exception:
                logger.exception("Account %s: could not mark chat %s as read", self.account_id, chat_id)

    async def _resolve_chat_status(self, event) -> str | None:
        """Возвращает направление чата (inbound / outbound_manual), при необходимости
        определяя его в первый раз.

        Если статус уже записан в БД — берём его (это и есть «сохранение состояния»:
        после перезапуска воркера статусы читаются из таблицы chat_statuses).
        Если чат для нас новый, смотрим НАСТОЯЩУЮ историю в Telegram: самое старое
        сообщение в чате. Это важно на случай, когда сотрудник написал первым давно
        или пока воркер был выключен — иначе ответ клиента ошибочно сочли бы
        началом входящего диалога. Одно обращение к API на чат, результат сохраняется
        навсегда и больше не запрашивается."""
        chat_id = str(event.chat_id)
        with SessionLocal() as db:
            status = get_chat_status(db, self.account_id, chat_id)
        if status is not None:
            return status

        try:
            oldest = await self.client.get_messages(await event.get_input_chat(), limit=1, reverse=True)
        except FLOOD_ERRORS as exc:
            await self._register_flood(exc, "classify")
            return None
        except Exception:
            logger.exception("Account %s: cannot classify chat %s", self.account_id, chat_id)
            return None

        if oldest:
            first_status = OUTBOUND_MANUAL if oldest[0].out else INBOUND
        else:
            first_status = OUTBOUND_MANUAL if event.out else INBOUND

        with SessionLocal() as db:
            status = establish_chat_status(db, self.account_id, chat_id, first_status)
        if status == OUTBOUND_MANUAL:
            logger.info("Account %s: chat %s начат сотрудником вручную — в исключениях", self.account_id, chat_id)
        return status

    async def _on_outgoing(self, event):
        """Ловит сообщения, которые сам сотрудник отправил вручную из настоящего
        Telegram (не через нашего бота) — например, написал клиенту первым сам.

        Направление чата фиксируется РОВНО один раз, по самому первому сообщению в
        нём — неважно, входящему или исходящему (см. app/services/chat_status.py).
        Поэтому здесь достаточно спросить «у этого чата уже есть статус?»:
          - если статуса ещё нет — значит это первое сообщение в чате вообще, и оно
            исходящее -> чат стартовал сотрудник вручную -> заносим в исключения
            (outbound_manual), автоответчик его больше никогда не коснётся;
          - если статус уже есть — ничего не делаем. В частности, когда бот сам
            отвечает клиенту, статус "inbound" уже выставлен в _process() ДО того,
            как этот ответ был отправлен (см. там), поэтому собственные ответы бота
            сюда не попадают как "ручное сообщение" — гоняться за ID своих
            сообщений, чтобы отличить их от человека, не требуется.
        """
        if not event.is_private:
            return
        chat_id = str(event.chat_id)
        if chat_id == TELEGRAM_SERVICE_ID:
            return

        with SessionLocal() as db:
            if get_chat_status(db, self.account_id, chat_id) is None:
                establish_chat_status(db, self.account_id, chat_id, OUTBOUND_MANUAL)
                logger.info(
                    "Account %s: chat %s начат сотрудником вручную — добавлен в исключения "
                    "(автоответчик не будет отвечать в этом диалоге)",
                    self.account_id, chat_id,
                )

    # ------------------------------------------------------------------ генерация и отправка
    async def _process(self, event) -> bool:
        with SessionLocal() as db:
            account = db.get(Account, self.account_id)
            if account is None or not account.enabled or not is_global_enabled(db):
                return False

            chat_id = str(event.chat_id)
            user_text = event.raw_text or ""

            self._log_incoming(event)

            # Направление чата фиксируется по самому первому сообщению в нём. Если
            # статуса ещё нет — это первое сообщение вообще, и оно входящее -> клиент
            # написал первым -> обычный inbound-диалог, автоответчик работает как обычно.
            # Устанавливаем это ДО генерации ответа: тогда, когда бот отправит свой
            # ответ, _on_outgoing() увидит уже выставленный статус "inbound" и не
            # спутает ответ бота с ручным сообщением сотрудника.
            chat_status = get_chat_status(db, account.id, chat_id)
            if chat_status is None:
                chat_status = establish_chat_status(db, account.id, chat_id, INBOUND)

            if chat_status == OUTBOUND_MANUAL:
                # Диалог начал сам сотрудник вручную — он в списке исключений,
                # автоматические сценарии сюда не применяются.
                return False

            schedule = get_schedule_settings(db)
            try:
                work_windows = [(w["start"], w["end"]) for w in schedule["work_windows"]]
                break_windows = [(w["start"], w["end"]) for w in schedule["break_windows"]]
                within_hours = not schedule["work_hours_enabled"] or is_within_work_hours(
                    dt.datetime.now(),
                    work_windows,
                    schedule["break_enabled"],
                    break_windows,
                )
            except Exception:
                # Некорректно сохранённое расписание не должно блокировать ответы —
                # ведём себя как без ограничения по времени и логируем проблему.
                logger.exception("Account %s: invalid schedule settings, ignoring time restriction", self.account_id)
                within_hours = True

            if not within_hours:
                grace = schedule["keep_active_dialog_enabled"] and _dialog_recently_active(
                    db, account.id, chat_id, schedule["keep_active_dialog_minutes"]
                )
                if not grace:
                    # Вне рабочих часов/на перерыве и нет активного диалога, который
                    # можно было бы не обрывать — сообщение уже залогировано выше,
                    # менеджер увидит его на странице «Логи», но ИИ не отвечает.
                    return

            history_rows = (
                db.query(DialogMessage)
                .filter_by(account_id=account.id, chat_id=chat_id)
                .order_by(DialogMessage.created_at.desc())
                .limit(settings.history_limit)
                .all()
            )
            history = [{"role": row.role, "content": row.content} for row in reversed(history_rows)]

            if account.system_prompt:
                system_prompt = account.system_prompt
            else:
                prompt_parts = []
                if is_prompt_enabled(db):
                    prompt_parts.append(load_prompt_template(settings.prompt_template_path))
                if is_kb_enabled(db):
                    kb_text = load_knowledge_base(settings.knowledge_base_path)
                    prompt_parts.append(f"### База знаний компании\n{kb_text}")
                system_prompt = "\n\n".join(prompt_parts) or "Ты — ассистент компании."

            disclaimer = build_disclaimer(account)
            first_reply = (
                db.query(DialogMessage.id)
                .filter_by(account_id=account.id, chat_id=chat_id, role="assistant")
                .first()
                is None
            )
            show_disclaimer = account.disclaimer_every_message is not False or first_reply
            llm_settings = get_llm_settings(db)
            pacing_cfg = get_protection(db)

        # Темп: «время подумать» по сессионной модели (см. services/pacing.py). Нейросеть
        # генерирует ответ параллельно этому ожиданию, поэтому её время входит в задержку,
        # а не прибавляется к ней; «печатает…» показывается только в конце, по длине ответа.
        target_delay = self._pacer.next_delay(pacing_cfg)
        started = time.monotonic()
        gen_task = asyncio.create_task(generate_reply(system_prompt, history, user_text, **llm_settings))
        try:
            reply_text = await gen_task
        except LLMError as exc:
            logger.error("Account %s: LLM error: %s", self.account_id, exc)
            self._record_error(str(exc))
            return
        except asyncio.CancelledError:
            gen_task.cancel()
            raise

        remaining = target_delay - (time.monotonic() - started)
        if remaining > 0:
            await asyncio.sleep(remaining)
        if self.is_paused():
            return False  # за время ожидания другой чат поймал флуд — не отправляем

        full_reply = f"{reply_text}\n\n— {disclaimer}" if show_disclaimer else reply_text
        try:
            input_chat = await event.get_input_chat()
            async with self.client.action(input_chat, "typing"):
                await asyncio.sleep(typing_seconds(len(full_reply), pacing_cfg["typing_cps"]))
            await event.reply(full_reply)
        except FLOOD_ERRORS as exc:
            await self._register_flood(exc, "send")
            return False
        except RPCError as exc:
            logger.error("Account %s: send failed: %s", self.account_id, exc)
            self._on_rpc_error(exc)
            return False

        self._pacer.touch()
        with SessionLocal() as db:
            db.add(
                DialogMessage(
                    account_id=self.account_id, chat_id=chat_id, role="assistant", content=reply_text
                )
            )
            db.commit()
        return True

    def mark_dead(self, message: str):
        """Сессия необратимо испорчена — воркер снимается с аккаунта, пока не загрузят новую."""
        logger.error("Account %s: %s", self.account_id, message)
        self.dead = True
        with SessionLocal() as db:
            acc = db.get(Account, self.account_id)
            if acc:
                acc.is_authorized = False
                acc.last_error = message
                db.commit()

    def _on_rpc_error(self, exc: BaseException):
        if isinstance(exc, FATAL_ERRORS):
            self.mark_dead(fatal_text(exc))
        else:
            self._record_error(str(exc))

    def _record_error(self, message: str):
        with SessionLocal() as db:
            acc = db.get(Account, self.account_id)
            if acc:
                acc.last_error = message[:2000]
                db.commit()


class WorkerManager:
    """Периодически сверяет запущенные Telethon-клиенты с желаемым состоянием в БД
    (тумблеры на панели, наличие .session файла, глобальный выкл/вкл)."""

    def __init__(self):
        self.workers: dict[int, AccountWorker] = {}
        # account_id -> session_path, с которым запуск провалился необратимо; повторяем
        # только после загрузки другой сессии (иначе бесконечные попытки и спам в лог).
        self._burnt: dict[int, str] = {}
        # account_id -> (число подряд неудач, monotonic-время следующей попытки)
        self._retry: dict[int, tuple[int, float]] = {}
        self._stopping = False

    async def run(self):
        logger.info("Worker manager started, reconcile interval=%ss", settings.reconcile_interval_seconds)
        while not self._stopping:
            try:
                await self._reconcile()
            except Exception:
                logger.exception("Reconcile loop error")
            await asyncio.sleep(settings.reconcile_interval_seconds)

    async def _reconcile(self):
        no_proxy: list[int] = []
        with SessionLocal() as db:
            global_on = is_global_enabled(db)
            accounts = db.query(Account).all()
            desired = {}
            for a in accounts:
                if not (a.enabled and global_on and a.session_path and Path(a.session_path).exists()):
                    continue
                if settings.require_proxy and not (a.proxy or "").strip():
                    no_proxy.append(a.id)  # fail-closed: без прокси не подключаемся вообще
                    continue
                desired[a.id] = a
        for account_id in no_proxy:
            self._set_error_once(account_id, "Не запущен: у аккаунта не задан прокси (прокси обязателен). "
                                             "Укажите прокси на странице «Аккаунты».")

        for account_id in list(self.workers.keys()):
            worker = self.workers[account_id]
            if worker.dead:
                self._burnt[account_id] = worker.session_path
                await worker.stop()
                del self.workers[account_id]
                continue
            if worker.unhealthy:
                await worker.stop()
                del self.workers[account_id]
                continue
            account = desired.get(account_id)
            # Telethon после нескольких неудачных попыток переподключения сдаётся и
            # клиент остаётся мёртвым: без этой проверки после сбоя сети/сна компьютера
            # аккаунт молча перестаёт отвечать до ручного перезапуска.
            if worker.client is not None and not worker.client.is_connected():
                if worker.disconnected_since is None:
                    worker.disconnected_since = time.monotonic()
                elif time.monotonic() - worker.disconnected_since > settings.reconnect_grace_seconds:
                    logger.warning("Account %s: no connection for %ss, restarting client", account_id,
                                   settings.reconnect_grace_seconds)
                    await worker.stop()
                    del self.workers[account_id]
                    continue
            else:
                worker.disconnected_since = None
            # Аккаунт выключили/убрали, либо поменяли сессию/прокси/приложение/профиль —
            # старый клиент больше не годится, перезапускаем с нуля.
            needs_restart = account is None or worker.identity != account_identity(account)
            if needs_restart:
                await worker.stop()
                del self.workers[account_id]

        for account_id, account in desired.items():
            if account_id not in self.workers:
                if self._burnt.get(account_id) == account.session_path:
                    continue
                if account.paused_until and account.paused_until > dt.datetime.utcnow():
                    continue  # автостоп ещё действует — даже не подключаемся
                fails, next_try = self._retry.get(account_id, (0, 0.0))
                if time.monotonic() < next_try:
                    continue

                # Режим require_proxy выключен (не рекомендуется): ограничиваем число прямых подключений.
                if not account.proxy:
                    direct = sum(1 for w in self.workers.values() if not w.proxy)
                    if direct >= settings.max_direct_accounts:
                        self._set_error_once(
                            account_id,
                            f"Не запущен: уже {direct} аккаунтов подключено без прокси (лимит "
                            f"{settings.max_direct_accounts}). Укажите прокси для этого аккаунта.",
                        )
                        continue

                self._burnt.pop(account_id, None)
                # «1 аккаунт = 1 воркер»: второго AccountWorker на тот же аккаунт быть не может
                # (защита номер 1 из трёх — см. app/services/session_lock.py)
                assert account_id not in self.workers, f"account {account_id} already has a worker"
                worker = AccountWorker(account)
                try:
                    # Тайм-аут обязателен: зависший запуск одного аккаунта (например, через
                    # нестабильный прокси) иначе блокирует весь цикл сверки — перестают
                    # применяться тумблеры, смена прокси и запуск остальных аккаунтов.
                    await asyncio.wait_for(worker.start(), timeout=settings.start_timeout_seconds)
                    self.workers[account_id] = worker
                    self._retry.pop(account_id, None)
                except (*FATAL_ERRORS, NotAuthorizedError) as exc:
                    self._burnt[account_id] = account.session_path
                    worker.mark_dead(fatal_text(exc) if isinstance(exc, FATAL_ERRORS) else str(exc))
                except FLOOD_ERRORS as exc:
                    await worker._register_flood(exc, "start")
                except Exception as exc:
                    logger.exception("Account %s: failed to start", account_id)
                    fails += 1
                    delay = min(settings.start_retry_base_seconds * 2 ** (fails - 1), settings.start_retry_max_seconds)
                    self._retry[account_id] = (fails, time.monotonic() + delay)
                    text = str(exc)[:1900] or type(exc).__name__
                    if account.proxy and isinstance(exc, (ConnectionError, TypeError, OSError, asyncio.TimeoutError)):
                        endpoint = account.proxy.split("@")[-1]
                        text = (f"Не удалось подключиться к Telegram через прокси {endpoint}: прокси недоступен "
                                f"или не пропускает Telegram. Проверьте/замените прокси. Аккаунт напрямую "
                                f"(без прокси) не подключается. [{type(exc).__name__}]")
                    with SessionLocal() as db:
                        acc = db.get(Account, account_id)
                        if acc:
                            acc.last_error = f"{text} (повтор через {int(delay)} с)"
                            db.commit()

                # Пачку аккаунтов подключаем не разом, а с паузами.
                await asyncio.sleep(
                    random.uniform(settings.start_stagger_min_seconds, settings.start_stagger_max_seconds)
                )

    @staticmethod
    def _set_error_once(account_id: int, message: str):
        with SessionLocal() as db:
            acc = db.get(Account, account_id)
            if acc and acc.last_error != message:
                acc.last_error = message
                db.commit()

    async def shutdown(self):
        self._stopping = True
        for worker in self.workers.values():
            await worker.stop()
