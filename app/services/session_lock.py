"""Обеспечивает правило «1 аккаунт = 1 воркер»: файл сессии одного аккаунта не может
быть открыт (client.connect()) больше чем в одном месте одновременно. Одновременное
использование одного ключа Telegram аннулирует его безвозвратно (AuthKeyDuplicatedError).

Правило действует на трёх уровнях защиты:
  1. WorkerManager.workers — словарь по account_id внутри ОДНОГО процесса: не даёт
     создать второй AccountWorker для того же аккаунта, пока первый жив (см. telegram_worker.py).
  2. _held_in_process (ниже) — та же гарантия внутри процесса, но на уровне файла
     сессии: ловит ошибку, даже если что-то попытается открыть тот же .session
     в обход WorkerManager (например, диагностический скрипт, запущенный из кода воркера).
  3. SessionLock (ниже) — файл-блокировка на диске: не даёт ДВУМ ПРОЦЕССАМ (например,
     двум запущенным run_worker.py, или воркеру и отдельным диагностическим скриптом)
     одновременно открыть один и тот же файл сессии.
Любой код, который открывает TelegramClient(session_path, ...), ДОЛЖЕН получить
SessionLock для этого пути первым — иначе три уровня защиты не работают."""
import os
from pathlib import Path

import psutil

_held_in_process: set[str] = set()  # абсолютные пути .session, занятые ЭТИМ процессом


class SessionInUseError(RuntimeError):
    pass


def _owner_alive(pid: int, lock_mtime: float) -> bool:
    """Процесс жив и это тот же процесс, что писал блокировку (а не новый с тем же PID)."""
    try:
        proc = psutil.Process(pid)
        return proc.is_running() and proc.create_time() <= lock_mtime + 2
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


class SessionLock:
    """Использовать как контекстный менеджер:
        with SessionLock(session_path):
            client = TelegramClient(session_path, ...)
            await client.connect()
            ...
    При выходе из `with` (в том числе через исключение) блокировка снимается сама.
    """

    def __init__(self, session_path: str):
        self.key = str(Path(session_path).resolve())
        self.path = Path(self.key + ".lock")
        self._acquired = False

    def acquire(self) -> None:
        if self.key in _held_in_process:
            # Тот же процесс уже держит эту сессию (например, забыли release() при
            # прошлой попытке, или два корутины по ошибке взяли один и тот же путь).
            raise SessionInUseError(
                f"Сессия {self.key} уже открыта в этом же процессе — правило "
                f"«1 аккаунт = 1 воркер» нарушено на уровне кода, а не файла"
            )

        me = os.getpid()
        try:
            other = int(self.path.read_text().strip())
            mtime = self.path.stat().st_mtime
        except (OSError, ValueError):
            other, mtime = None, 0.0
        if other and other != me and _owner_alive(other, mtime):
            raise SessionInUseError(
                f"Сессия {self.key} уже используется процессом {other} — второй "
                f"воркер/скрипт на неё не пускаем, иначе Telegram аннулирует ключ"
            )

        self.path.write_text(str(me))
        _held_in_process.add(self.key)
        self._acquired = True

    def release(self) -> None:
        _held_in_process.discard(self.key)
        if not self._acquired:
            return
        self._acquired = False
        try:
            if self.path.read_text().strip() == str(os.getpid()):
                self.path.unlink()
        except OSError:
            pass

    def __enter__(self) -> "SessionLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()
