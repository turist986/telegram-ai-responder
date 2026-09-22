"""Тесты изолированы от боевых данных: БД, сессии и data-каталог — во временной папке.
Переменные окружения имеют приоритет над .env (pydantic-settings), поэтому реальный
app.db и файлы сессий тесты не видят. К Telegram тесты не подключаются."""
import os
import sys
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="ai_responder_tests_"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_tmp / 'test.db').as_posix()}"
os.environ["DATA_DIR"] = str(_tmp / "data")
os.environ["SESSIONS_DIR"] = str(_tmp / "sessions")
os.environ.setdefault("TELEGRAM_API_ID", "12345")
os.environ.setdefault("TELEGRAM_API_HASH", "0" * 32)
os.environ.setdefault("ADMIN_PASSWORD_HASH", "x")
os.environ.setdefault("SECRET_KEY", "test-secret")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
TMP = _tmp
