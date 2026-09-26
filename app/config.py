from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Корень проекта (папка telegram-ai-responder), не зависит от текущей
# рабочей директории процесса — важно для systemd/uvicorn --app-dir и т.п.
BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    # Telegram API (https://my.telegram.org)
    telegram_api_id: int
    telegram_api_hash: str

    # Ключи ИИ-провайдеров — заполняйте только те, которыми реально
    # пользуетесь. Какой провайдер активен (и какая модель) выбирается в
    # панели «Настройки» → «Нейросеть»; по умолчанию используется DeepSeek.
    # См. app/services/llm_providers.py.
    deepseek_api_key: str | None = None
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    groq_api_key: str | None = None
    mistral_api_key: str | None = None
    openrouter_api_key: str | None = None

    # Пути к данным
    data_dir: Path = BASE_DIR / "data"
    sessions_dir: Path = BASE_DIR / "data" / "sessions"
    knowledge_base_path: Path = BASE_DIR / "data" / "knowledge_base.txt"
    prompt_template_path: Path = BASE_DIR / "data" / "prompt_template.txt"
    managers_excel_path: Path = BASE_DIR / "data" / "managers.xlsx"
    database_url: str = f"sqlite:///{(BASE_DIR / 'data' / 'app.db').as_posix()}"

    # Веб-панель
    admin_username: str = "admin"
    admin_password_hash: str
    secret_key: str

    # Дисклеймер по умолчанию — общий плейсхолдер; задайте своё название компании через .env
    default_manager_name: str = "Служба поддержки"
    default_disclaimer_template: str = "Ответ сгенерирован ИИ-ассистентом менеджера {name}."
    default_fallback_disclaimer: str = "Ответ сгенерирован ИИ-ассистентом."

    # Поведение воркера
    history_limit: int = 12
    typing_delay_min: float = 2.0
    typing_delay_max: float = 6.0
    reconcile_interval_seconds: int = 5
    # Вход большой пачки аккаунтов растягиваем во времени: не более одного
    # подключения за случайную паузу, чтобы не выглядеть как массовый заход с одного IP.
    start_stagger_min_seconds: float = 3.0
    start_stagger_max_seconds: float = 8.0
    # Повторная попытка неудачного запуска: 30 с, 60 с, 120 с ... до 15 минут.
    start_retry_base_seconds: int = 15
    start_retry_max_seconds: int = 900
    # Не более стольких аккаунтов одновременно подключено напрямую (без прокси) с IP сервера.
    max_direct_accounts: int = 10
    # Аккаунт без прокси НЕ запускается вообще (ни одного соединения с IP сервера).
    require_proxy: bool = True
    # Импорт из TData: конвертируется через CreateNewSession (создаёт отдельный сеанс со
    # своим ключом, не делит его с Telegram Desktop — см. session_utils.py) и только
    # через прокси аккаунта, поэтому включён по умолчанию. Выключите, если хотите
    # разрешить добавление аккаунтов только через мастер (вход по номеру).
    allow_tdata_import: bool = True
    # Импорт готовых .session-файлов: такие сессии рождены с чужим api_id/IP — учитывайте
    # это отдельно. Включён по умолчанию (как раньше, до отключения на время аудита).
    allow_legacy_session_import: bool = True
    # Мастер добавления аккаунта: сколько живёт незавершённая попытка и видимость браузера.
    onboarding_ttl_seconds: int = 900
    playwright_headless: bool = True
    # Сколько секунд клиент может быть без связи, прежде чем воркер пересоздаст его.
    reconnect_grace_seconds: int = 180
    # Максимум времени на подключение одного аккаунта, дальше запуск считается неудачным.
    start_timeout_seconds: int = 90
    # Проверка пропущенных непрочитанных: как часто и сколько последних диалогов смотреть.
    catch_up_interval_seconds: int = 300
    # Как часто запрашивать у Telegram пропущенные обновления (дешёвый getDifference):
    # страхует от случаев, когда живые события по аккаунту не доходят вовремя.
    updates_poll_seconds: int = 15
    catch_up_dialogs_limit: int = 50

    model_config = SettingsConfigDict(env_file=str(BASE_DIR / ".env"), env_file_encoding="utf-8")


settings = Settings()
