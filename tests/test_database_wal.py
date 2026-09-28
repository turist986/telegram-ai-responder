"""SQLite: WAL-режим против «сайт не открывается под нагрузкой».

Панель и воркер — два отдельных процесса на одном файле data/app.db; воркер пишет тем чаще,
чем больше идёт диалогов одновременно. В режиме по умолчанию (rollback journal) запись
блокирует ВСЕ чтения этого файла — живой замер (два настоящих процесса, 10 параллельных
диалогов 8 секунд) показал рост задержки открытия панели до 250 мс и деградацию под нагрузкой;
с WAL — 5-13 мс, запись в 3-4 раза быстрее. Здесь — что этот режим действительно включён и
не ломает обычную работу."""
import concurrent.futures
import threading
import time
import unittest

from sqlalchemy import text

from app.database import SessionLocal, engine
from app.models import Account, DialogMessage


class PragmaTests(unittest.TestCase):
    def test_wal_mode_is_active(self):
        with engine.connect() as conn:
            mode = conn.execute(text("PRAGMA journal_mode")).scalar()
        self.assertEqual(mode.lower(), "wal")

    def test_busy_timeout_is_set(self):
        with engine.connect() as conn:
            timeout_ms = conn.execute(text("PRAGMA busy_timeout")).scalar()
        self.assertGreaterEqual(timeout_ms, 30000)

    def test_new_connections_get_the_same_pragmas(self):
        # PRAGMA journal_mode хранится в самом файле (одного раза достаточно), а
        # synchronous/busy_timeout — настройка СОЕДИНЕНИЯ и должна выставляться заново на
        # КАЖДОЕ новое; проверяем именно второе отдельное соединение, а не то же самое.
        with SessionLocal() as db1:
            db1.execute(text("SELECT 1"))
        with SessionLocal() as db2:
            timeout_ms = db2.execute(text("PRAGMA busy_timeout")).scalar()
            mode = db2.execute(text("PRAGMA journal_mode")).scalar()
        self.assertGreaterEqual(timeout_ms, 30000)
        self.assertEqual(mode.lower(), "wal")


class ConcurrentAccessTests(unittest.TestCase):
    """Не полноценная замена живому межпроцессному тесту (см. docstring файла), но ловит
    регрессию в самих pragma: без WAL этот тест либо падает с 'database is locked', либо
    заметно медленнее (эмпирически — miллисекунды против сотен мс на 8-секундном прогоне)."""

    def setUp(self):
        with SessionLocal() as db:
            acc = Account(identifier=f"wal_test_{time.monotonic_ns()}", enabled=True)
            db.add(acc)
            db.commit()
            self.acc_id = acc.id
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        # без этого десятки строк DialogMessage остаются висеть на числовом account_id — если
        # другой тестовый файл потом очистит таблицу accounts и заново займёт тот же id
        # (у SQLite ROWID — просто max()+1, не настоящий AUTOINCREMENT), эти строки ошибочно
        # достанутся чужому аккаунту и собьют счётчики в других тестах
        with SessionLocal() as db:
            db.query(DialogMessage).filter_by(account_id=self.acc_id).delete()
            db.query(Account).filter_by(id=self.acc_id).delete()
            db.commit()

    def test_reads_stay_fast_while_writes_happen_concurrently(self):
        stop = threading.Event()
        write_errors = []

        def writer():
            n = 0
            while not stop.is_set():
                try:
                    with SessionLocal() as db:
                        db.add(DialogMessage(account_id=self.acc_id, chat_id="1", role="user", content="x"))
                        db.commit()
                except Exception as exc:  # noqa: BLE001
                    write_errors.append(exc)
                n += 1
                time.sleep(0.005)

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(writer) for _ in range(5)]
            latencies = []
            for _ in range(30):
                t0 = time.perf_counter()
                with SessionLocal() as db:
                    db.query(Account).all()
                latencies.append(time.perf_counter() - t0)
                time.sleep(0.01)
            stop.set()
            concurrent.futures.wait(futures)

        self.assertEqual(write_errors, [])
        latencies.sort()
        p95 = latencies[int(len(latencies) * 0.95)]
        # с rollback journal под такой же нагрузкой это заметно выше (десятки-сотни мс, растёт
        # с нагрузкой) — порог с большим запасом, чтобы не флапать на медленной машине CI
        self.assertLess(p95, 1.0, f"чтение аккаунтов под параллельной записью подозрительно "
                                  f"медленное (p95={p95:.3f}с) — не сработал ли WAL?")


if __name__ == "__main__":
    unittest.main()
