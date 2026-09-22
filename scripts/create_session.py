"""Интерактивно создаёт уже авторизованный .session файл Telethon.

ВАЖНО: запускайте этот скрипт ЛОКАЛЬНО (на своём компьютере, не на
продуктовом сервере) для каждого менеджерского аккаунта — потребуется один
раз ввести номер телефона и код из Telegram. Полученный файл
data/sessions/<identifier>.session затем загрузите через веб-панель
(форма «Загрузить .session») или скопируйте прямо в data/sessions/ на сервере.

Сервер не может пройти интерактивный вход, поэтому загружать нужно уже
готовую, авторизованную сессию.

Запуск:
    python scripts/create_session.py <identifier>

<identifier> должен совпадать со значением в столбце "Аккаунт" managers.xlsx.
"""
import sys
from pathlib import Path

from telethon.sync import TelegramClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import settings  # noqa: E402

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Использование: python scripts/create_session.py <identifier>")
        raise SystemExit(1)

    identifier = sys.argv[1]
    out_path = Path("data/sessions") / f"{identifier}.session"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with TelegramClient(str(out_path), settings.telegram_api_id, settings.telegram_api_hash) as client:
        me = client.get_me()
        print(f"Готово: авторизован как {me.first_name} ({me.phone}). Файл: {out_path}")
