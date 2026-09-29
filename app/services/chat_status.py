"""Определяет и хранит НАПРАВЛЕНИЕ диалога: кто написал первым.

Правило простое и фиксируется один раз навсегда для каждого чата:
  - первым написал клиент          -> "inbound"         -> автоответчик работает как обычно.
  - первым написал сам менеджер     -> "outbound_manual"  -> чат в исключениях (blacklist),
    вручную (не бот)                                        автоответчик его не касается.

Статус хранится в таблице chat_statuses (SQLite/SQLAlchemy, та же база, что у остальных
данных проекта) с уникальным ключом (account_id, chat_id) — благодаря этому:
  1) статус выставляется РОВНО один раз (повторные попытки записать в уже существующую
     строку ловятся исключением уникальности и просто игнорируются — см. try/except ниже);
  2) статус переживает перезапуск воркера/сервера: при старте мы просто читаем таблицу,
     ничего не нужно восстанавливать из памяти процесса.
"""
import datetime as dt
import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import ChatStatus

logger = logging.getLogger(__name__)

INBOUND = "inbound"
OUTBOUND_MANUAL = "outbound_manual"


def get_status(db: Session, account_id: int, chat_id: str) -> str | None:
    """None значит «для этого чата направление ещё не определено» (не было ни одного
    сообщения) — вызывающий код должен сам решить, к какому статусу его отнести."""
    row = (
        db.query(ChatStatus.status)
        .filter_by(account_id=account_id, chat_id=chat_id)
        .first()
    )
    return row[0] if row else None


def establish_status(db: Session, account_id: int, chat_id: str, status: str) -> str:
    """Пытается зафиксировать статус чата. Если чат кто-то уже классифицировал
    (типичная гонка: входящее и исходящее сообщение обработались почти одновременно
    в двух разных задачах asyncio) — возвращает статус, который победил на самом деле,
    а не тот, что попытались записать мы. Так вызывающая сторона всегда действует по
    актуальному статусу, даже если её собственная попытка записи проиграла гонку."""
    db.add(ChatStatus(account_id=account_id, chat_id=chat_id, status=status))
    try:
        db.commit()
        return status
    except IntegrityError:
        # UniqueConstraint(account_id, chat_id) не дал вставить вторую строку —
        # значит, статус уже определён другим потоком обработки. Читаем, что победило.
        db.rollback()
        existing = get_status(db, account_id, chat_id)
        logger.info(
            "Chat status race for account %s chat %s: tried %s, existing is %s",
            account_id, chat_id, status, existing,
        )
        return existing or status


def is_blacklisted(db: Session, account_id: int, chat_id: str) -> bool:
    return get_status(db, account_id, chat_id) == OUTBOUND_MANUAL


def get_pause_until(db: Session, account_id: int, chat_id: str) -> dt.datetime | None:
    """Пауза «лимит сообщений в чате» (Настройки → Защита) — только для ЭТОГО чата, не для
    всего аккаунта (в отличие от автостопа при FloodWait, см. AccountWorker._register_flood)."""
    row = db.query(ChatStatus.paused_until).filter_by(account_id=account_id, chat_id=chat_id).first()
    return row[0] if row else None


def set_pause(db: Session, account_id: int, chat_id: str, until: dt.datetime) -> None:
    """Требует, чтобы строка ChatStatus для этого чата уже существовала (см. establish_status) —
    вызывается уже ПОСЛЕ того, как направление чата определено."""
    db.query(ChatStatus).filter_by(account_id=account_id, chat_id=chat_id).update({"paused_until": until})
    db.commit()


def set_display_name(db: Session, account_id: int, chat_id: str, display_name: str | None) -> None:
    """Имя собеседника из Telegram — только для отображения на страницах «Логи»/«Диалоги»/
    «Чёрный список», чтобы chat_id можно было опознать глазами. display_name=None (Telethon не
    отдал данные отправителя на этом сообщении) — ничего не делаем, а не затираем уже известное
    имя пустотой. Требует существующую строку ChatStatus (см. establish_status) — молча
    ничего не делает, если её ещё нет (сообщение залогировано раньше, чем статус установлен)."""
    if not display_name:
        return
    db.query(ChatStatus).filter_by(account_id=account_id, chat_id=chat_id).update({"display_name": display_name})
    db.commit()


def get_limit_override(db: Session, account_id: int, chat_id: str) -> tuple[int | None, int | None]:
    """Ручной лимит/пауза для ОДНОГО диалога (страница «Диалоги») — (лимит, пауза в минутах),
    любое из них может быть None (значит используется общая настройка). (None, None), если для
    этого чата ручных значений вообще не задавали."""
    row = (
        db.query(ChatStatus.message_limit_override, ChatStatus.pause_minutes_override)
        .filter_by(account_id=account_id, chat_id=chat_id)
        .first()
    )
    return (row[0], row[1]) if row else (None, None)


def set_limit_override(
    db: Session, account_id: int, chat_id: str, message_limit: int | None, pause_minutes: int | None
) -> bool:
    """Требует существующую строку ChatStatus (см. establish_status). Обычно она уже есть —
    страница «Диалоги» показывает чаты, где была переписка, а статус выставляется одним из
    первых шагов её обработки (см. _process в telegram_worker.py); но аккаунт мог быть выключен
    ДО этого шага, и тогда сообщение уже залогировано, а статуса ещё нет. Возвращает False в
    этом редком случае — вызывающая сторона должна не выдавать ложный «сохранено»."""
    affected = (
        db.query(ChatStatus)
        .filter_by(account_id=account_id, chat_id=chat_id)
        .update({"message_limit_override": message_limit, "pause_minutes_override": pause_minutes})
    )
    db.commit()
    return affected > 0


def pause_now(db: Session, account_id: int, chat_id: str, minutes: int) -> dt.datetime | None:
    """Немедленная ручная пауза ЭТОГО диалога на minutes минут от текущего момента —
    страница «Диалоги», кнопка «Пауза сейчас» (не ждёт срабатывания лимита сообщений).

    Заодно выставляет pause_minutes_override (если он ещё не задан) — иначе, если общий
    лимит сообщений в «Настройки → Защита» выключен, воркер вообще не проверял бы
    paused_until для этого чата (см. _process в telegram_worker.py) и эта пауза молча
    ни на что не повлияла бы.

    None, если для этого чата ещё нет строки ChatStatus (см. set_limit_override) — редкий
    случай, когда сообщение уже залогировано, а статус чата ещё не установлен."""
    if get_status(db, account_id, chat_id) is None:
        return None
    until = dt.datetime.utcnow() + dt.timedelta(minutes=minutes)
    set_pause(db, account_id, chat_id, until)
    limit_override, pause_override = get_limit_override(db, account_id, chat_id)
    if pause_override is None:
        set_limit_override(db, account_id, chat_id, limit_override, minutes)
    return until
