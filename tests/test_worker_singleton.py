"""Проверка «только один воркер» на НАСТОЯЩИХ процессах.

Регрессия: на Windows venv-python — лаунчер, который запускает настоящий интерпретатор дочерним
процессом с той же командной строкой; воркер принимал собственного родителя за «другой воркер»
и сразу завершался (сайт работал, а ответов в Telegram не было)."""
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parent.parent
MARKER = f"fake_worker_{uuid.uuid4().hex[:8]}.py"      # уникальный: реальные воркеры на машине не мешают

SCRIPT = f'''
import subprocess, sys, time
sys.path.insert(0, {str(ROOT)!r})
from app.services.worker_singleton import another_worker_running
mode = sys.argv[1]
if mode == "check":
    print(another_worker_running(marker={MARKER!r}))
elif mode == "sleep":
    time.sleep(60)
elif mode == "launcher-check":      # как venv-python: настоящая работа — в дочернем процессе с той же командной строкой
    subprocess.run([sys.executable, __file__, "check"])
elif mode == "launcher-sleep":
    subprocess.run([sys.executable, __file__, "sleep"])
'''


def _kill_tree(proc: subprocess.Popen):
    try:
        parent = psutil.Process(proc.pid)
        for child in parent.children(recursive=True):
            child.kill()
        parent.kill()
    except psutil.Error:
        pass


class SingletonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.script = str(Path(cls._tmp.name) / MARKER)
        Path(cls.script).write_text(SCRIPT, encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        self._procs: list[subprocess.Popen] = []
        self.addCleanup(lambda: [_kill_tree(p) for p in self._procs])

    def _run(self, mode: str) -> str:
        out = subprocess.run([sys.executable, self.script, mode], capture_output=True, text=True, timeout=60,
                             env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        return out.stdout.strip()

    def _spawn(self, mode: str):
        p = subprocess.Popen([sys.executable, self.script, mode], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._procs.append(p)
        time.sleep(2.5)         # чтобы процесс точно «старше» проверяющего
        return p

    def test_alone_no_other_worker(self):
        self.assertEqual(self._run("check"), "False")

    def test_own_launcher_parent_is_not_another_worker(self):
        # проверяющий — дочерний процесс «лаунчера» с той же командной строкой: раньше давало True
        self.assertEqual(self._run("launcher-check"), "False")

    def test_real_older_worker_is_detected(self):
        self._spawn("sleep")
        self.assertEqual(self._run("check"), "True")

    def test_older_worker_behind_its_own_launcher_is_still_detected(self):
        self._spawn("launcher-sleep")
        self.assertEqual(self._run("check"), "True")

    def test_old_worker_launcher_alone_does_not_block_new_instance_started_after_it_died(self):
        p = self._spawn("sleep")
        _kill_tree(p)
        time.sleep(0.5)
        self.assertEqual(self._run("check"), "False")


if __name__ == "__main__":
    unittest.main()
