"""Страница «Диалоги»: с какими собеседниками СЕЙЧАС идёт (или недавно шла) переписка, по всем
аккаунтам — и ручной лимит/пауза для ОДНОГО конкретного диалога поверх общих настроек «Настройки
→ Защита» (см. services/chat_status.py: message_limit_override/pause_minutes_override на ChatStatus)."""
import datetime as dt
from dataclasses import dataclass

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import Account, ChatStatus, DialogMessage
from .chat_status import set_limit_override


class DialogOverrideError(ValueError):
    pass


@dataclass
class DialogRow:
    account: Account
    chat_id: str
    message_count: int
    last_at: dt.datetime
    status: ChatStatus | None

    @property
    def paused_until(self) -> dt.datetime | None:
        return self.status.paused_until if self.status else None

    @property
    def is_paused(self) -> bool:
        return self.paused_until is not None and self.paused_until > dt.datetime.utcnow()

    @property
    def message_limit_override(self) -> int | None:
        return self.status.message_limit_override if self.status else None

    @property
    def pause_minutes_override(self) -> int | None:
        return self.status.pause_minutes_override if self.status else None

    @property
    def display_name(self) -> str | None:
        return self.status.display_name if self.status else None


def list_active_dialogs(db: Session, limit: int = 100) -> list[DialogRow]:
    """Самые недавно активные диалоги (по последнему сообщению), по всем аккаунтам."""
    agg = (
        db.query(
            DialogMessage.account_id,
            DialogMessage.chat_id,
            func.count(DialogMessage.id).label("message_count"),
            func.max(DialogMessage.created_at).label("last_at"),
        )
        .group_by(DialogMessage.account_id, DialogMessage.chat_id)
        .order_by(func.max(DialogMessage.created_at).desc())
        .limit(limit)
        .all()
    )
    if not agg:
        return []

    account_ids = {row.account_id for row in agg}
    accounts = {a.id: a for a in db.query(Account).filter(Account.id.in_(account_ids)).all()}

    statuses: dict[tuple[int, str], ChatStatus] = {}
    for s in db.query(ChatStatus).filter(ChatStatus.account_id.in_(account_ids)).all():
        statuses[(s.account_id, s.chat_id)] = s

    result = []
    for row in agg:
        account = accounts.get(row.account_id)
        if account is None:  # аккаунт удалили, а сообщения остались — не показываем
            continue
        result.append(DialogRow(
            account=account,
            chat_id=row.chat_id,
            message_count=row.message_count,
            last_at=row.last_at,
            status=statuses.get((row.account_id, row.chat_id)),
        ))
    return result


def set_override(db: Session, account_id: int, chat_id: str, message_limit: str, pause_minutes: str) -> None:
    """Строки из формы -> (int | None, int | None); пустое поле -> None (использовать общую
    настройку). Требует уже существующий ChatStatus — см. get_limit_override/set_limit_override."""
    def _parse(raw: str, label: str) -> int | None:
        raw = (raw or "").strip()
        if not raw:
            return None
        try:
            value = int(raw)
        except ValueError:
            raise DialogOverrideError(f"{label}: введите целое число или оставьте пустым")
        if value < 1:
            raise DialogOverrideError(f"{label}: должно быть не меньше 1")
        return value

    limit = _parse(message_limit, "Лимит сообщений")
    pause = _parse(pause_minutes, "Пауза, мин")
    if not set_limit_override(db, account_id, chat_id, limit, pause):
        raise DialogOverrideError(
            "Для этого чата ещё не определён статус (аккаунт был выключен на момент "
            "сообщения) — попробуйте ещё раз, когда придёт следующее сообщение"
        )
