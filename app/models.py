import datetime as dt

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from .database import Base


class Account(Base):
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True)
    # Идентификатор аккаунта: должен совпадать со столбцом "Аккаунт" в managers.xlsx
    # и с именем загруженного .session файла (без расширения).
    identifier = Column(String(64), unique=True, nullable=False)
    phone = Column(String(32), nullable=True)
    session_path = Column(String(255), nullable=True)
    # Прокси для этого аккаунта, напр. socks5://user:pass@host:port — полезно,
    # когда менеджеры и сервер находятся в разных странах/регионах.
    proxy = Column(String(255), nullable=True)
    manager_name = Column(String(128), nullable=True)
    disclaimer_marker = Column(Text, nullable=True)
    status_note = Column(String(64), nullable=True)  # свободный текст из Excel, информативно
    # True/NULL — пометка «ответ ИИ» в каждом сообщении; False — только в первом ответе диалога.
    disclaimer_every_message = Column(Boolean, default=True, nullable=True)
    # Свой системный промпт аккаунта; если задан — заменяет общий промпт и базу знаний
    # (например, для внутреннего отдела, где бот общается на любые темы).
    system_prompt = Column(Text, nullable=True)
    enabled = Column(Boolean, default=True, nullable=False)  # тумблер в панели
    is_authorized = Column(Boolean, default=False, nullable=False)
    last_error = Column(Text, nullable=True)
    # Собственное приложение аккаунта (my.telegram.org), создаётся мастером добавления.
    # Пусто у «старых» аккаунтов — для них используется общий api_id из .env.
    api_id = Column(Integer, nullable=True)
    api_hash = Column(String(64), nullable=True)
    # Стабильный «профиль устройства»: задаётся один раз при добавлении и больше не меняется,
    # иначе в списке сеансов Telegram аккаунт «меняет устройство» при каждом рестарте.
    device_model = Column(String(64), nullable=True)
    system_version = Column(String(64), nullable=True)
    app_version = Column(String(32), nullable=True)
    lang_code = Column(String(16), nullable=True)
    system_lang_code = Column(String(16), nullable=True)
    # Автостоп при FloodWait/PeerFlood: серия подряд, время последнего срабатывания, пауза до.
    flood_streak = Column(Integer, default=0, nullable=True)
    flood_last_at = Column(DateTime, nullable=True)
    paused_until = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=dt.datetime.utcnow)
    updated_at = Column(DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow)


class DialogMessage(Base):
    __tablename__ = "dialog_messages"

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    chat_id = Column(String(64), nullable=False)
    role = Column(String(16), nullable=False)  # "user" | "assistant"
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=dt.datetime.utcnow)

    account = relationship("Account")


class ChatStatus(Base):
    """Направление, в котором был начат диалог — определяется ОДИН раз первым
    сообщением в чате и больше не меняется:
      inbound          — первым написал клиент (внешний собеседник). Автоответчик работает.
      outbound_manual   — первым написал сам менеджер (сотрудник, вручную, не бот).
                          Чат уходит в исключения (blacklist) — автоответчик его не трогает,
                          чтобы не встревать в диалог, который человек ведёт сам.
    Храним в той же SQLite/SQLAlchemy базе, что и остальные данные проекта — так статус
    переживает перезапуск воркера (см. app/services/chat_status.py)."""

    __tablename__ = "chat_statuses"

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    chat_id = Column(String(64), nullable=False)
    status = Column(String(16), nullable=False)  # "inbound" | "outbound_manual"
    created_at = Column(DateTime, default=dt.datetime.utcnow)

    account = relationship("Account")

    __table_args__ = (
        UniqueConstraint("account_id", "chat_id", name="uq_chat_status_account_chat"),
    )


class GlobalSetting(Base):
    __tablename__ = "global_settings"

    key = Column(String(64), primary_key=True)
    value = Column(Text, nullable=True)


class ApiCredential(Base):
    """Приложение Telegram (api_id/api_hash), доступное для назначения аккаунтам —
    «библиотека» пар, загруженных вручную (см. onboarding.add_credentials) или
    оставшихся про запас. Аккаунт хранит свой api_id/api_hash в своей же строке
    (Account.api_id/api_hash) — эта таблица нужна только чтобы держать пары,
    которые ЕЩЁ не привязаны ни к одному аккаунту, и подписать их меткой."""

    __tablename__ = "api_credentials"

    id = Column(Integer, primary_key=True)
    api_id = Column(Integer, unique=True, nullable=False)
    api_hash = Column(String(64), nullable=False)
    label = Column(String(128), nullable=True)
    created_at = Column(DateTime, default=dt.datetime.utcnow)
