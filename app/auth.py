import bcrypt
from itsdangerous import BadSignature, URLSafeTimedSerializer
from fastapi import Request

from .config import settings

serializer = URLSafeTimedSerializer(settings.secret_key, salt="dashboard-auth")

COOKIE_NAME = "session"
COOKIE_MAX_AGE = 60 * 60 * 12  # 12 часов


class NotAuthenticated(Exception):
    pass


def verify_password(plain: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), settings.admin_password_hash.encode("utf-8"))


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def create_session_cookie(username: str) -> str:
    return serializer.dumps({"user": username})


def read_session_cookie(token: str) -> str | None:
    try:
        data = serializer.loads(token, max_age=COOKIE_MAX_AGE)
        return data.get("user")
    except BadSignature:
        return None


def require_login(request: Request) -> str:
    token = request.cookies.get(COOKIE_NAME)
    user = read_session_cookie(token) if token else None
    if not user:
        raise NotAuthenticated()
    return user
