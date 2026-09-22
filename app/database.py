import logging
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import settings

logger = logging.getLogger(__name__)

if settings.database_url.startswith("sqlite"):
    db_file = settings.database_url.split("///")[-1]
    Path(db_file).parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
)
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
