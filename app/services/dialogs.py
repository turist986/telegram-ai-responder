"""Страница «Диалоги»: с какими собеседниками СЕЙЧАС идёт (или недавно шла) переписка, по всем
аккаунтам — и ручной лимит/пауза для ОДНОГО конкретного диалога поверх общих настроек «Настройки
→ Защита» (см. services/chat_status.py: message_limit_override/pause_minutes_override на ChatStatus)."""
import datetime as dt
from dataclasses import dataclass

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from ..models import Account, AccountBlacklist, ChatStatus, DialogMessage
from .chat_status import set_limit_override
from .settings_store import get_setting, set_setting


class DialogOverrideError(ValueError):
    pass


MAX_MESSAGE_LIMIT = 100_000
MAX_PAUSE_MINUTES = 60 * 24 * 365  # год

# Режим страницы «Диалоги»:
#   selective — как было: у каждого чата свой ручной лимит/пауза (плюс общий лимит из
#               «Настройки → Защита», если он включён там);
#   general   — одно правило на все чаты: бот отвечает на первые N сообщений СОБЕСЕДНИКА в чате,
#               на следующее уже нет — и бросает чат на заданное время или навсегда (тогда чат
#               попадает в «Чёрный список» с пометкой, вернуть можно там же).
MODE_SELECTIVE, MODE_GENERAL = "selective", "general"
ACTION_PAUSE, ACTION_FOREVER = "pause", "forever"
GENERAL_NOTE_PREFIX = "Общее правило «Диалоги»"
_UNITS = {"minutes": 1, "hours": 60, "days": 60 * 24}


@dataclass
class GeneralRule:
    limit: int = 10
    action: str = ACTION_PAUSE
    pause_minutes: int = 60 * 24

    @property
    def pause_display(self) -> tuple[int, str]:
        """(число, единица) для формы: 1440 мин → (1, days), 90 мин → (90, minutes)."""
        for unit in ("days", "hours"):
            if self.pause_minutes % _UNITS[unit] == 0:
                return self.pause_minutes // _UNITS[unit], unit
        return self.pause_minutes, "minutes"


def limit_for(rule: GeneralRule, account) -> int:
    """N для аккаунта: своё, если задано на странице «Диалоги», иначе общее из правила."""
    return account.dialog_limit or rule.limit


def save_account_limits(db: Session, values: dict[int, str]) -> int:
    """{account_id: текст из формы}; пусто — общее число. Бросает DialogOverrideError, ничего не
    сохраняя, если хоть одно значение некорректно. Возвращает число аккаунтов со своим N."""
    parsed: dict[int, int | None] = {}
    for account_id, raw in values.items():
        raw = (raw or "").strip()
        if not raw:
            parsed[account_id] = None
            continue
        if not raw.isdigit() or not 1 <= int(raw) <= MAX_MESSAGE_LIMIT:
            account = db.get(Account, account_id)
            name = account.identifier if account else account_id
            raise DialogOverrideError(f"Аккаунт {name}: число сообщений — целое от 1 до {MAX_MESSAGE_LIMIT} "
                                      f"(или пусто — общее число)")
        parsed[account_id] = int(raw)
    for account in db.query(Account).filter(Account.id.in_(list(parsed))).all():
        account.dialog_limit = parsed[account.id]
    db.commit()
    return sum(1 for v in parsed.values() if v)


def get_mode(db: Session) -> str:
    return MODE_GENERAL if get_setting(db, "dialogs_mode") == MODE_GENERAL else MODE_SELECTIVE


def get_general_rule(db: Session) -> GeneralRule:
    rule = GeneralRule()
    try:
        rule.limit = max(1, int(get_setting(db, "general_dialog_limit") or rule.limit))
        rule.pause_minutes = max(1, int(get_setting(db, "general_dialog_pause_minutes") or rule.pause_minutes))
    except ValueError:
        pass
    if get_setting(db, "general_dialog_action") == ACTION_FOREVER:
        rule.action = ACTION_FOREVER
    return rule


def save_mode(db: Session, mode: str, limit: str = "", action: str = "", pause_value: str = "",
              pause_unit: str = "minutes") -> None:
    """Сохраняет режим; для «Общей» — проверяет и сохраняет правило. Бросает DialogOverrideError."""
    if mode not in (MODE_SELECTIVE, MODE_GENERAL):
        raise DialogOverrideError("Неизвестный режим")
    if mode == MODE_GENERAL:
        if not (limit or "").strip().isdigit() or not 1 <= int(limit) <= MAX_MESSAGE_LIMIT:
            raise DialogOverrideError(f"Количество сообщений: целое число от 1 до {MAX_MESSAGE_LIMIT}")
        if action not in (ACTION_PAUSE, ACTION_FOREVER):
            raise DialogOverrideError("Выберите, что делать с чатом после лимита: на время или навсегда")
        if action == ACTION_PAUSE:
            if pause_unit not in _UNITS or not (pause_value or "").strip().isdigit() or int(pause_value) < 1:
                raise DialogOverrideError("Время паузы: целое число не меньше 1")
            minutes = int(pause_value) * _UNITS[pause_unit]
            if minutes > MAX_PAUSE_MINUTES:
                raise DialogOverrideError("Время паузы: не больше года — для «навсегда» выберите вариант «навсегда»")
            set_setting(db, "general_dialog_pause_minutes", str(minutes))
        set_setting(db, "general_dialog_limit", str(int(limit)))
        set_setting(db, "general_dialog_action", action)
    set_setting(db, "dialogs_mode", mode)


def user_messages_since(db: Session, account_id: int, chat_id: str, since: dt.datetime | None) -> int:
    """Сколько сообщений написал СОБЕСЕДНИК в чате (ответы бота не считаются) — с конца прошлой
    паузы, если она была: иначе после паузы чат сразу же снова «превышал» бы лимит."""
    q = db.query(func.count(DialogMessage.id)).filter(
        DialogMessage.account_id == account_id, DialogMessage.chat_id == chat_id, DialogMessage.role == "user")
    if since is not None:
        q = q.filter(DialogMessage.created_at >= since)
    return q.scalar() or 0


def abandon_forever(db: Session, account_id: int, chat_id: str, limit: int) -> None:
    """«Навсегда»: чат в «Чёрный список» — там видно, почему, и оттуда его можно вернуть."""
    exists = db.query(AccountBlacklist.id).filter_by(account_id=account_id, chat_id=chat_id).first()
    if exists is None:
        db.add(AccountBlacklist(account_id=account_id, chat_id=chat_id,
                                note=f"{GENERAL_NOTE_PREFIX}: собеседник написал больше {limit} сообщений"))
        db.commit()


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

    user_count: int = 0
    blacklisted: bool = False


def list_active_dialogs(db: Session, limit: int = 100) -> list[DialogRow]:
    """Самые недавно активные диалоги (по последнему сообщению), по всем аккаунтам."""
    agg = (
        db.query(
            DialogMessage.account_id,
            DialogMessage.chat_id,
            func.count(DialogMessage.id).label("message_count"),
            func.sum(case((DialogMessage.role == "user", 1), else_=0)).label("user_count"),
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
    blacklisted = {(b.account_id, b.chat_id) for b in
                   db.query(AccountBlacklist).filter(AccountBlacklist.account_id.in_(account_ids)).all()}

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
            user_count=int(row.user_count or 0),
            blacklisted=(row.account_id, row.chat_id) in blacklisted,
        ))
    return result


def set_override(db: Session, account_id: int, chat_id: str, message_limit: str, pause_minutes: str) -> None:
    """Строки из формы -> (int | None, int | None); пустое поле -> None (использовать общую
    настройку). Требует уже существующий ChatStatus — см. get_limit_override/set_limit_override."""
    def _parse(raw: str, label: str, maximum: int) -> int | None:
        raw = (raw or "").strip()
        if not raw:
            return None
        try:
            value = int(raw)
        except ValueError:
            raise DialogOverrideError(f"{label}: введите целое число или оставьте пустым")
        if value < 1:
            raise DialogOverrideError(f"{label}: должно быть не меньше 1")
        if value > maximum:
            raise DialogOverrideError(f"{label}: не больше {maximum}")
        return value

    limit = _parse(message_limit, "Лимит сообщений", MAX_MESSAGE_LIMIT)
    pause = _parse(pause_minutes, "Пауза, мин", MAX_PAUSE_MINUTES)
    if not set_limit_override(db, account_id, chat_id, limit, pause):
        raise DialogOverrideError(
            "Для этого чата ещё не определён статус (аккаунт был выключен на момент "
            "сообщения) — попробуйте ещё раз, когда придёт следующее сообщение"
        )
