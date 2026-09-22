"""Отдельный процесс воркера — держит Telethon-клиенты менеджеров и отвечает
на входящие сообщения. Запускается независимо от веб-панели (см.
deploy/systemd/ai-responder-worker.service)."""
import asyncio
import logging
import signal

from app.database import init_db
from app.worker.telegram_worker import WorkerManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


async def main():
    init_db()
    manager = WorkerManager()

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _handle_signal():
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            pass  # Windows: нет add_signal_handler, Ctrl+C всё равно поднимет KeyboardInterrupt

    run_task = asyncio.create_task(manager.run())
    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        pass
    await manager.shutdown()
    run_task.cancel()


def _another_worker_is_running() -> bool:
    """Два воркера на одних сессиях ломают ключи авторизации (AuthKeyDuplicatedError),
    поэтому работает только самый старый процесс — остальные сразу завершаются."""
    import os

    import psutil

    me = psutil.Process(os.getpid())
    for proc in psutil.process_iter(["pid", "cmdline", "create_time"]):
        cmd = proc.info["cmdline"] or []
        if proc.info["pid"] == me.pid or not any("run_worker.py" in part for part in cmd):
            continue
        if proc.info["create_time"] < me.create_time():
            return True
    return False


if __name__ == "__main__":
    if _another_worker_is_running():
        logging.getLogger(__name__).error("Воркер уже запущен в другом процессе — этот экземпляр завершается")
        raise SystemExit(0)
    asyncio.run(main())
