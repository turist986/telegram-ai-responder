import logging
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import settings

logger = logging.getLogger(__name__)

_IS_SQLITE = settings.database_url.startswith("sqlite")

if _IS_SQLITE:
    db_file = settings.database_url.split("///")[-1]
    Path(db_file).parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if _IS_SQLITE else {},
)

if _IS_SQLITE:
    # Веб-панель и воркер — два ОТДЕЛЬНЫХ процесса, оба постоянно ходят в один файл БД; воркер
    # тем чаще пишет, чем больше идёт диалогов одновременно. По умолчанию SQLite (rollback
    # journal) на время любой записи блокирует ВСЕ чтения этого файла — под реальной нагрузкой
    # (проверено: 10 параллельных диалогов) открытие панели занимало до 250 мс и росло дальше
    # вместе с нагрузкой, вплоть до «сайт не открывается». WAL позволяет читать, пока идёт
    # запись (в живом тесте: те же 10 диалогов — 5-13 мс вместо 60-250 мс, запись в 3-4 раза
    # быстрее). busy_timeout — подстраховка на случай двух записей одновременно (сама WAL
    # такого не разруливает, но такое редко и коротко): ждать, а не сразу падать с "database
    # is locked". synchronous=NORMAL — рекомендованная для WAL настройка, безопасна (в WAL,
    # в отличие от rollback journal, она не рискует целостностью БД при сбое, только теряет
    # чуть послед. долю секунды при аварийном отключении питания — от одного процесса на VPS
    # это не то, ради чего стоит держать более медленный FULL).
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


def _sync_schema():
    """Добавляет отсутствующие столбцы в уже существующие таблицы.

    create_all() создаёт только новые таблицы и не трогает существующие —
    без этого любое новое поле в models.py (как proxy у Account) ломает
    все запросы к старой БД с ошибкой "no such column". Полноценных миграций
    (Alembic) в проекте пока нет, поэтому это простой автосинк только для
    ДОБАВЛЕНИЯ столбцов; переименования/удаления полей так не обрабатываются.
    """
    inspector = inspect(engine)
    for table_name, table in Base.metadata.tables.items():
        if not inspector.has_table(table_name):
            continue
        existing_cols = {c["name"] for c in inspector.get_columns(table_name)}
        for column in table.columns:
            if column.name in existing_cols:
                continue
            col_type = column.type.compile(engine.dialect)
            with engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE "{table_name}" ADD COLUMN "{column.name}" {col_type}'))
            logger.info("Schema sync: added %s.%s", table_name, column.name)


def init_db():
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _sync_schema()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
