"""«Текущие ниши»: у аккаунта может быть несколько ниш подряд, у каждой — своя дата начала
(«с какой даты этот контекст актуален»). Для входящего сообщения берётся та ниша, чья
active_from самая поздняя из тех, что не позже даты САМОГО СООБЩЕНИЯ, а не текущего момента —
иначе запоздало разобранные (после простоя воркера) старые сообщения получили бы ЧУЖОЙ,
более новый контекст ниши."""
import datetime as dt

from sqlalchemy.orm import Session

from ..models import Account, AccountNiche


class NicheConfigError(ValueError):
    pass


def parse_date(value: str) -> dt.date:
    """ГГГГ-ММ-ДД (формат HTML <input type=date>)."""
    try:
        return dt.date.fromisoformat(value.strip())
    except (ValueError, AttributeError):
        raise NicheConfigError(f"Неверный формат даты «{value}» — используйте ГГГГ-ММ-ДД")


def list_niches(db: Session, account_id: int) -> list[AccountNiche]:
    return (
        db.query(AccountNiche)
        .filter_by(account_id=account_id)
        .order_by(AccountNiche.active_from.desc(), AccountNiche.id.desc())
        .all()
    )


def add_niche(db: Session, account_id: int, title: str, description: str, active_from: str) -> AccountNiche:
    title = title.strip()
    description = description.strip()
    if not title:
        raise NicheConfigError("Укажите короткое название ниши")
    if not description:
        raise NicheConfigError("Укажите, о чём должен быть в курсе бот по этой нише")
    date = parse_date(active_from)
    niche = AccountNiche(account_id=account_id, title=title, description=description, active_from=date)
    db.add(niche)
    db.commit()
    return niche


def delete_niche(db: Session, niche_id: int) -> None:
    db.query(AccountNiche).filter_by(id=niche_id).delete()
    db.commit()


def event_date(event) -> dt.datetime:
    """Дата ВХОДЯЩЕГО сообщения (не «сейчас») — см. docstring модуля. Без даты у события
    (не должно случаться для настоящих сообщений Telegram) — берём текущий момент."""
    date = getattr(event, "date", None)
    if date is None:
        return dt.datetime.utcnow()
    if date.tzinfo is not None:
        date = date.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return date


def get_active_niche(db: Session, account_id: int, at: dt.date | dt.datetime) -> AccountNiche | None:
    """Ниша, действовавшая НА ДАТУ at (обычно — дата входящего сообщения, см. event_date).
    None, если ниш нет вовсе или все они начинаются позже этой даты."""
    at_date = at.date() if isinstance(at, dt.datetime) else at
    return (
        db.query(AccountNiche)
        .filter(AccountNiche.account_id == account_id, AccountNiche.active_from <= at_date)
        .order_by(AccountNiche.active_from.desc(), AccountNiche.id.desc())
        .first()
    )


def niche_prompt_block(niche: AccountNiche) -> str:
    """Текст для добавления в системный промпт — с явным приоритетом реального диалога."""
    return (
        f"### Ниша текущих заявок (с {niche.active_from:%d.%m.%Y}): {niche.title}\n"
        f"{niche.description}\n"
        f"Это только общий контекст о том, откуда обычно приходят заявки на этот аккаунт с "
        f"указанной даты — не факт про ЭТОГО КОНКРЕТНОГО собеседника. Если то, что пишет сам "
        f"собеседник (в этом сообщении или в истории переписки выше), говорит о другой теме — "
        f"доверяй собеседнику и истории диалога, а не этому описанию ниши."
    )


def accounts_with_niches(db: Session) -> list[tuple[Account, list[AccountNiche]]]:
    """Все аккаунты вместе со своими нишами (пусто у кого не заведено) — для страницы «Ниши»."""
    accounts = db.query(Account).order_by(Account.identifier).all()
    niches_by_account: dict[int, list[AccountNiche]] = {}
    for n in db.query(AccountNiche).order_by(AccountNiche.active_from.desc(), AccountNiche.id.desc()).all():
        niches_by_account.setdefault(n.account_id, []).append(n)
    return [(a, niches_by_account.get(a.id, [])) for a in accounts]
