import os
import subprocess
import sys

import psutil

from ..config import BASE_DIR

PID_FILE = BASE_DIR / "data" / "worker.pid"
LOG_FILE = BASE_DIR / "data" / "worker.log"


def _worker_pids() -> list[int]:
    """Все процессы run_worker.py — независимо от того, кто и как их запустил
    (кнопка в панели, start_all.bat, консоль). Ориентироваться только на PID-файл
    нельзя: воркер, запущенный вручную, он бы не увидел и не смог остановить."""
    me = os.getpid()
    pids = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        cmd = proc.info["cmdline"] or []
        if proc.info["pid"] != me and any("run_worker.py" in part for part in cmd):
            pids.append(proc.info["pid"])
    return pids


def is_running() -> bool:
    return bool(_worker_pids())


def start_worker() -> None:
    if is_running():
        return
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    log = LOG_FILE.open("ab")
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen(
        [sys.executable, str(BASE_DIR / "run_worker.py")],
        cwd=str(BASE_DIR),
        stdout=log,
        stderr=log,
        stdin=subprocess.DEVNULL,
        creationflags=flags,
        start_new_session=sys.platform != "win32",
    )
    PID_FILE.write_text(str(proc.pid))


def stop_worker() -> None:
    procs = []
    for pid in _worker_pids():
        try:
            proc = psutil.Process(pid)
            proc.terminate()
            procs.append(proc)
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(procs, timeout=10)
    for proc in alive:
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            pass
    PID_FILE.unlink(missing_ok=True)
    # Убираем только «осиротевшие» блокировки убитых процессов. Удалять все подряд нельзя:
    # блокировка, принадлежащая ЖИВОМУ воркеру, — единственная защита сессии от второго
    # процесса, и её потеря открывает путь к AuthKeyDuplicatedError.
    sessions_dir = BASE_DIR / "data" / "sessions"
    if sessions_dir.exists():
        for lock in sessions_dir.glob("*.lock"):
            try:
                owner = int(lock.read_text().strip())
                if psutil.pid_exists(owner):
                    continue
                lock.unlink()
            except (OSError, ValueError):
                pass
