"""«Только один воркер»: два воркера на одних сессиях ломают ключи авторизации
(AuthKeyDuplicatedError), поэтому работает самый старый процесс, остальные сразу завершаются.

Что НЕ считается «другим воркером» (раньше из-за этого настоящий воркер на Windows сразу
завершался и не работал вовсе):
  * собственные родительские процессы: `python.exe` из venv на Windows — лаунчер, который
    запускает настоящий интерпретатор дочерним процессом с ТОЙ ЖЕ командной строкой, а сам
    воркер стартует внутри окна `cmd /k ...` (Планировщик — внутри `cmd /c ...bat`);
  * лаунчер чужого экземпляра (у него есть дочерний процесс с той же командной строкой) —
    настоящим воркером считается именно дочерний, иначе два одновременно стартующих
    экземпляра «видели» бы лаунчеры друг друга и оба завершились бы;
  * любые процессы, кроме python (окна cmd и т.п.)."""
import os

import psutil

MARKER = "run_worker.py"


def _has_marker(cmdline, marker: str = MARKER) -> bool:
    return any(marker in part for part in (cmdline or []))


def another_worker_running(pid: int | None = None, marker: str = MARKER) -> bool:
    me = psutil.Process(pid or os.getpid())
    ancestors = {p.pid for p in me.parents()}
    my_start = me.create_time()
    for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        info = proc.info
        if info["pid"] == me.pid or info["pid"] in ancestors:
            continue
        if "python" not in (info["name"] or "").lower() or not _has_marker(info["cmdline"], marker):
            continue
        try:
            if any(_has_marker(child.cmdline(), marker) for child in proc.children()):
                continue                      # лаунчер: настоящий воркер — его дочерний процесс
        except psutil.Error:
            pass
        if info["create_time"] < my_start:
            return True
    return False
