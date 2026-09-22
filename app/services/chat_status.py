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
