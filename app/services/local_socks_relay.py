"""Локальный SOCKS5-ретранслятор без авторизации, наружу ходит через заданный прокси.

Зачем: Chromium (а значит и Playwright) не умеет SOCKS5 с логином/паролем, а большинство
платных прокси именно такие. Ретранслятор слушает только 127.0.0.1 на случайном порту,
принимает соединения без авторизации и пробрасывает их через upstream (кортеж PySocks,
как из proxy.parse_proxy). Имена хостов передаются прокси как есть (удалённый DNS) —
локальный DNS-запрос не происходит, IP пользователя/сервера не светится.
Если upstream=None — соединяется напрямую (нужно для тестов)."""
import socket
import struct
import threading

import socks


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    return buf


def _pipe(a: socket.socket, b: socket.socket) -> None:
    try:
        while True:
            data = a.recv(65536)
            if not data:
                break
            b.sendall(data)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _open_upstream(upstream, host: str, port: int, timeout: float) -> socket.socket:
    if upstream is None:
        return socket.create_connection((host, port), timeout=timeout)
    s = socks.socksocket()
    s.set_proxy(*upstream)
    s.settimeout(timeout)
    s.connect((host, port))
    return s


def _handle(client: socket.socket, upstream, timeout: float) -> None:
    remote = None
    try:
        client.settimeout(timeout)
        ver, nmethods = _recv_exact(client, 2)
        _recv_exact(client, nmethods)
        if ver != 5:
            return
        client.sendall(b"\x05\x00")  # без авторизации
        ver, cmd, _, atyp = _recv_exact(client, 4)
        if cmd != 1:  # только CONNECT
            client.sendall(b"\x05\x07\x00\x01" + b"\x00" * 6)
            return
        if atyp == 1:
            host = socket.inet_ntoa(_recv_exact(client, 4))
        elif atyp == 3:
            host = _recv_exact(client, _recv_exact(client, 1)[0]).decode()
        elif atyp == 4:
            host = socket.inet_ntop(socket.AF_INET6, _recv_exact(client, 16))
        else:
            client.sendall(b"\x05\x08\x00\x01" + b"\x00" * 6)
            return
        (port,) = struct.unpack("!H", _recv_exact(client, 2))
        try:
            remote = _open_upstream(upstream, host, port, timeout)
        except (OSError, socks.ProxyError):
            client.sendall(b"\x05\x05\x00\x01" + b"\x00" * 6)
            return
        client.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)
        client.settimeout(None)
        remote.settimeout(None)
        t = threading.Thread(target=_pipe, args=(remote, client), daemon=True)
        t.start()
        _pipe(client, remote)
        t.join(timeout=5)
    except (OSError, ConnectionError):
        pass
    finally:
        for s in (client, remote):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass


class LocalSocksRelay:
    def __init__(self, upstream, timeout: float = 30.0):
        self.upstream = upstream
        self.timeout = timeout
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(64)
        self.port = self._srv.getsockname()[1]
        self._stopped = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stopped:
            try:
                client, _ = self._srv.accept()
            except OSError:
                return
            threading.Thread(target=_handle, args=(client, self.upstream, self.timeout), daemon=True).start()

    @property
    def url(self) -> str:
        return f"socks5://127.0.0.1:{self.port}"

    def close(self) -> None:
        self._stopped = True
        try:
            self._srv.close()
        except OSError:
            pass
