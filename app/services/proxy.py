import socket
import time
from urllib.parse import quote, unquote, urlparse

try:
    import socks
except ImportError:
    socks = None

_SCHEME_TO_PYSOCKS_TYPE = {
    "socks5": "SOCKS5",
    "socks4": "SOCKS4",
    "http": "HTTP",
}

TELEGRAM_TEST_ENDPOINTS = [("149.154.167.51", 443), ("149.154.167.91", 443), ("91.108.56.130", 443)]


class ProxyConfigError(ValueError):
    pass


def build_proxy_url(scheme: str, host: str, port: str | int, user: str = "", password: str = "") -> str:
    """Собирает строку прокси из отдельных полей; логин и пароль кодируются,
    поэтому спецсимволы (@ : / # и т.п.) в пароле безопасны."""
    scheme = (scheme or "socks5").strip().lower().rstrip(":/")
    host = host.strip()
    port = str(port).strip()
    if not host or not port.isdigit():
        raise ProxyConfigError("Укажите хост и числовой порт прокси")
    auth = ""
    if user:
        auth = quote(user, safe="")
        if password:
            auth += ":" + quote(password, safe="")
        auth += "@"
    return f"{scheme}://{auth}{host}:{port}"


def normalize_proxy(raw: str | None, default_scheme: str = "socks5") -> str | None:
    """Приводит любой распространённый формат к виду scheme://user:pass@host:port.

    Понимает: scheme://user:pass@host:port, user:pass@host:port, host:port,
    host:port:user:pass и user:pass:host:port (с необязательной схемой)."""
    if raw is None or not raw.strip():
        return None
    text = raw.strip()

    scheme = default_scheme
    if "://" in text:
        scheme, text = text.split("://", 1)
        scheme = scheme.lower()

    if "@" in text:
        creds, hostport = text.rsplit("@", 1)
        if ":" in creds:
            user, password = creds.split(":", 1)
        else:
            user, password = creds, ""
        host, _, port = hostport.rpartition(":")
        # логин/пароль в готовой ссылке уже могут быть закодированы — не кодируем дважды
        return build_proxy_url(scheme, host, port, unquote(user), unquote(password))

    parts = text.split(":")
    if len(parts) == 2:
        return build_proxy_url(scheme, parts[0], parts[1])
    if len(parts) == 4:
        if parts[1].isdigit():
            host, port, user, password = parts
        elif parts[3].isdigit():
            user, password, host, port = parts
        else:
            raise ProxyConfigError("Не удалось разобрать прокси: не найден порт")
        return build_proxy_url(scheme, host, port, user, password)
    raise ProxyConfigError(
        "Формат прокси не распознан. Примеры: socks5://логин:пароль@host:1080, "
        "host:1080:логин:пароль, host:1080"
    )


def proxy_identity(proxy_str: str | None) -> tuple:
    """Ключ «это один и тот же прокси»: хост, порт, логин и пароль (схема не важна).

    У мобильных и резидентных пулов хост (а часто и порт) один на всех, а разные
    прокси различаются логином/паролем (сессия/страна/IP привязаны к логину) — поэтому
    сравнивать только host:port нельзя, аккаунты с разными логинами были бы ложно
    признаны «одним прокси»."""
    try:
        text = normalize_proxy(proxy_str) or ""
    except ProxyConfigError:
        text = proxy_str or ""
    parsed = urlparse(text)
    return (
        (parsed.hostname or "").lower(),
        parsed.port,
        unquote(parsed.username) if parsed.username else "",
        unquote(parsed.password) if parsed.password else "",
    )


def mask_proxy(proxy_str: str | None) -> str:
    """scheme://host:port (+ пометка, что есть логин) — без пароля, для показа в панели."""
    if not proxy_str:
        return ""
    parsed = urlparse(proxy_str)
    shown = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
    return shown + (f"  [логин: {unquote(parsed.username)}]" if parsed.username else "")


def split_proxy(proxy_str: str | None) -> dict:
    parsed = urlparse(proxy_str or "")
    return {
        "scheme": parsed.scheme or "socks5",
        "host": parsed.hostname or "",
        "port": parsed.port or "",
        "user": unquote(parsed.username) if parsed.username else "",
        "has_password": bool(parsed.password),
    }


def parse_proxy(proxy_str: str | None):
    """Разбирает строку прокси в кортеж, который принимает Telethon/PySocks.
    Возвращает None, если прокси не задан — тогда используется прямое подключение."""
    if not proxy_str or not proxy_str.strip():
        return None

    if socks is None:
        raise ProxyConfigError("Для работы с прокси установите пакет PySocks (pip install PySocks)")

    normalized = normalize_proxy(proxy_str)
    parsed = urlparse(normalized)
    scheme = parsed.scheme.lower()
    if scheme not in _SCHEME_TO_PYSOCKS_TYPE:
        raise ProxyConfigError(
            f"Неизвестная схема прокси '{parsed.scheme}'. Используйте socks5://, socks4:// или http://"
        )
    if not parsed.hostname or not parsed.port:
        raise ProxyConfigError("В прокси должны быть указаны хост и порт, например socks5://host:1080")

    proxy_type = getattr(socks, _SCHEME_TO_PYSOCKS_TYPE[scheme])
    username = unquote(parsed.username) if parsed.username else None
    password = unquote(parsed.password) if parsed.password else None
    return (proxy_type, parsed.hostname, parsed.port, True, username, password)


# Публичные адреса дата-центров Telegram. Часть прокси (IPv6-only) не соединяет с
# IPv4-адресами, но нормально работает с IPv6 — тогда клиент надо переключить на IPv6.
DC_IPV4 = {1: "149.154.175.53", 2: "149.154.167.51", 3: "149.154.175.100", 4: "149.154.167.91", 5: "91.108.56.130"}
DC_IPV6 = {
    1: "2001:b28:f23d:f001::a",
    2: "2001:67c:4e8:f002::a",
    3: "2001:b28:f23d:f003::a",
    4: "2001:67c:4e8:f004::a",
    5: "2001:b28:f23f:f005::a",
}


def _tcp_via_proxy(proxy_tuple, host: str, port: int, timeout: float) -> bool:
    s = socks.socksocket()
    s.set_proxy(*proxy_tuple)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except (socks.ProxyError, socket.timeout, OSError):
        return False
    finally:
        s.close()


_family_cache: dict[tuple, bool] = {}  # (host, port) прокси -> последнее рабочее семейство (True = IPv6)


def choose_ip_family(proxy_tuple, dc_id: int, timeout: float = 10.0) -> tuple[bool, str | None]:
    """(use_ipv6, адрес_дц). Пробует сначала семейство, которое прошло в прошлый раз
    (у нестабильных прокси это экономит десятки секунд), потом второе. Прокси иногда
    отвечает отказом на случайной попытке, поэтому попыток несколько.
    Если не работает ничего — (False, None)."""
    dc_id = dc_id if dc_id in DC_IPV4 else 2
    key = (proxy_tuple[1], proxy_tuple[2])
    first_v6 = _family_cache.get(key, False)
    plan = [first_v6] * 3 + [not first_v6] * 2
    for use_v6 in plan:
        addr = DC_IPV6[dc_id] if use_v6 else DC_IPV4[dc_id]
        if _tcp_via_proxy(proxy_tuple, addr, 443, timeout):
            _family_cache[key] = use_v6
            return use_v6, addr
    return False, None


def test_proxy(proxy_str: str, timeout: float = 15.0) -> tuple[bool, str]:
    """Проверяет, что через прокси можно достучаться до серверов Telegram.
    Сессии не используются — только TCP-соединение (безопасно для аккаунтов)."""
    try:
        p = parse_proxy(proxy_str)
    except ProxyConfigError as exc:
        return False, str(exc)
    if p is None:
        return False, "Прокси не задан"

    started = time.time()
    use_v6, addr = choose_ip_family(p, 2, timeout=timeout)
    if addr is None:
        return False, "Прокси не пропускает Telegram ни по IPv4, ни по IPv6"
    ms = int((time.time() - started) * 1000)
    family = "IPv6 (прокси выходит в интернет по IPv6, воркер переключится сам)" if use_v6 else "IPv4"
    return True, f"Прокси работает: соединение с Telegram по {family}, {ms} мс"
