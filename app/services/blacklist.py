"""Чёрный список: конкретные собеседники (по chat_id), с которыми ЭТОТ аккаунт больше не должен
переписываться — автоответчик молчит в этом чате навсегда (пока запись не удалят вручную), в
отличие от лимита сообщений в чате (Настройки → Защита), где пауза временная и снимается сама."""
from sqlalchemy.orm import Session

from ..models import Account, AccountBlacklist


class BlacklistConfigError(ValueError):
    pass


def is_blacklisted(db: Session, account_id: int, chat_id: str) -> bool:
    return (
        db.query(AccountBlacklist)
        .filter_by(account_id=account_id, chat_id=chat_id)
        .first()
        is not None
    )


def list_blacklist(db: Session, account_id: int) -> list[AccountBlacklist]:
    return (
        db.query(AccountBlacklist)
        .filter_by(account_id=account_id)
        .order_by(AccountBlacklist.created_at.desc())
        .all()
    )


def add_entry(db: Session, account_id: int, chat_id: str, note: str = "") -> AccountBlacklist:
    # Без этой проверки форма с устаревшим account_id (аккаунт удалили в другой вкладке, пока
    # эта страница была открыта) молча создавала бы «осиротевшую» запись — см. такую же
    # проверку в services/niche.py.
    if db.get(Account, account_id) is None:
        raise BlacklistConfigError("Этот аккаунт уже не существует — обновите страницу «Чёрный список»")
    chat_id = str(chat_id).strip()
    if not chat_id:
        raise BlacklistConfigError("Укажите chat_id собеседника (виден в «Логи», столбец «Чат»)")
    existing = (
        db.query(AccountBlacklist)
        .filter_by(account_id=account_id, chat_id=chat_id)
        .first()
    )
    if existing is not None:
        return existing  # уже в списке — повторное добавление не ошибка, просто не дублируем
    entry = AccountBlacklist(account_id=account_id, chat_id=chat_id, note=note.strip() or None)
    db.add(entry)
    db.commit()
    return entry


def delete_entry(db: Session, entry_id: int) -> None:
    db.query(AccountBlacklist).filter_by(id=entry_id).delete()
    db.commit()


def accounts_with_blacklist(db: Session) -> list[tuple[Account, list[AccountBlacklist]]]:
    """Все аккаунты вместе со своим чёрным списком (пусто у кого не заведено) — для страницы
    «Чёрный список», см. accounts_with_niches в services/niche.py."""
    accounts = db.query(Account).order_by(Account.identifier).all()
    by_account: dict[int, list[AccountBlacklist]] = {}
    for e in db.query(AccountBlacklist).order_by(AccountBlacklist.created_at.desc()).all():
        by_account.setdefault(e.account_id, []).append(e)
    return [(a, by_account.get(a.id, [])) for a in accounts]
