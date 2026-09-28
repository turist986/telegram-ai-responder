"""«Текущие ниши»: сервисная логика выбора актуальной ниши по дате сообщения, роут CRUD и
подстановка в промпт воркера (с приоритетом реального диалога, см. docstring services/niche.py)."""
import datetime as dt
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import unquote_plus

from fastapi.testclient import TestClient

from app.auth import require_login
from app.database import SessionLocal, init_db
from app.main import app
from app.models import Account, AccountNiche, DialogMessage
from app.services.niche import (
    NicheConfigError, accounts_with_niches, add_niche, delete_niche, event_date,
    get_active_niche, list_niches, niche_prompt_block, parse_date,
)
from app.worker import telegram_worker as tw
from tests.test_worker_logic import WorkerBase, _event, _save


class ParseDateTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_date("2026-09-25"), dt.date(2026, 9, 25))

    def test_invalid(self):
        for bad in ("25.09.2026", "not-a-date", "", "2026-13-40"):
            with self.assertRaises(NicheConfigError):
                parse_date(bad)


class ServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        with SessionLocal() as db:
            db.query(AccountNiche).delete()
            db.query(Account).delete()
            db.commit()
            acc = Account(identifier="niche_acc", enabled=True)
            db.add(acc)
            db.commit()
            self.acc_id = acc.id

    def test_add_validates_and_persists(self):
        with SessionLocal() as db:
            n = add_niche(db, self.acc_id, "  Окна  ", "  Пластиковые окна, замер, монтаж  ", "2026-09-25")
            self.assertEqual(n.title, "Окна")               # обрезаны пробелы
            self.assertEqual(n.active_from, dt.date(2026, 9, 25))
            self.assertEqual(len(list_niches(db, self.acc_id)), 1)

    def test_add_rejects_empty_fields(self):
        with SessionLocal() as db:
            with self.assertRaises(NicheConfigError):
                add_niche(db, self.acc_id, "", "текст", "2026-09-25")
            with self.assertRaises(NicheConfigError):
                add_niche(db, self.acc_id, "Название", "  ", "2026-09-25")
            with self.assertRaises(NicheConfigError):
                add_niche(db, self.acc_id, "Название", "текст", "25-09-2026")
            self.assertEqual(list_niches(db, self.acc_id), [])   # ничего не сохранилось

    def test_active_niche_picks_latest_not_later_than_the_date(self):
        with SessionLocal() as db:
            add_niche(db, self.acc_id, "A", "первая ниша", "2026-09-01")
            add_niche(db, self.acc_id, "B", "вторая ниша, сменили рекламу", "2026-09-25")

            # до 1-го числа — ниш ещё не было
            self.assertIsNone(get_active_niche(db, self.acc_id, dt.date(2026, 8, 31)))
            # с 1-го по 24-е — ниша A
            self.assertEqual(get_active_niche(db, self.acc_id, dt.date(2026, 9, 1)).title, "A")
            self.assertEqual(get_active_niche(db, self.acc_id, dt.date(2026, 9, 24)).title, "A")
            # с 25-го — ниша B, даже если сообщение пришло гораздо позже
            self.assertEqual(get_active_niche(db, self.acc_id, dt.date(2026, 9, 25)).title, "B")
            self.assertEqual(get_active_niche(db, self.acc_id, dt.date(2027, 1, 1)).title, "B")

    def test_active_niche_accepts_datetime_too(self):
        with SessionLocal() as db:
            add_niche(db, self.acc_id, "A", "ниша", "2026-09-25")
            found = get_active_niche(db, self.acc_id, dt.datetime(2026, 9, 25, 23, 59))
            self.assertIsNotNone(found)

    def test_delete_removes_it(self):
        with SessionLocal() as db:
            n = add_niche(db, self.acc_id, "A", "ниша", "2026-09-25")
            delete_niche(db, n.id)
            self.assertEqual(list_niches(db, self.acc_id), [])

    def test_delete_nonexistent_id_does_not_raise(self):
        with SessionLocal() as db:
            delete_niche(db, 999999)  # не должно бросать

    def test_add_to_nonexistent_account_is_refused_not_orphaned(self):
        with SessionLocal() as db:
            with self.assertRaises(NicheConfigError) as cm:
                add_niche(db, 999999, "A", "ниша", "2026-09-25")
        self.assertIn("не существует", str(cm.exception))
        with SessionLocal() as db:
            self.assertEqual(db.query(AccountNiche).filter_by(account_id=999999).count(), 0)

    def test_tie_break_same_date_prefers_most_recently_added(self):
        with SessionLocal() as db:
            add_niche(db, self.acc_id, "Старая", "первой добавлена", "2026-09-25")
            add_niche(db, self.acc_id, "Новая", "второй добавлена, та же дата", "2026-09-25")
            found = get_active_niche(db, self.acc_id, dt.date(2026, 9, 25))
        self.assertEqual(found.title, "Новая")

    def test_special_characters_in_title_and_description_round_trip_intact(self):
        tricky = "«кавычки», <script>alert(1)</script>, \"двойные\", 'одинарные', перенос\nстроки"
        with SessionLocal() as db:
            n = add_niche(db, self.acc_id, tricky, tricky, "2026-09-25")
            self.assertEqual(n.title, tricky)
            self.assertEqual(n.description, tricky)

    def test_niche_prompt_block_names_priority_of_real_dialog(self):
        with SessionLocal() as db:
            n = add_niche(db, self.acc_id, "Окна", "Пластиковые окна", "2026-09-25")
            text = niche_prompt_block(n)
        self.assertIn("Окна", text)
        self.assertIn("Пластиковые окна", text)
        self.assertIn("25.09.2026", text)
        self.assertIn("собеседник", text.lower())

    def test_accounts_with_niches_groups_by_account_including_empty(self):
        with SessionLocal() as db:
            db.add(Account(identifier="no_niches", enabled=True))
            db.commit()
            add_niche(db, self.acc_id, "A", "ниша", "2026-09-25")
            rows = accounts_with_niches(db)
        by_ident = {a.identifier: ns for a, ns in rows}
        self.assertEqual(len(by_ident["niche_acc"]), 1)
        self.assertEqual(by_ident["no_niches"], [])

    def test_event_date_uses_message_date_not_now(self):
        ev = SimpleNamespace(date=dt.datetime(2026, 9, 25, 10, 0, tzinfo=dt.timezone.utc))
        self.assertEqual(event_date(ev), dt.datetime(2026, 9, 25, 10, 0))

    def test_event_date_falls_back_to_now_without_a_date(self):
        ev = SimpleNamespace()
        d = event_date(ev)
        self.assertLess(abs((dt.datetime.utcnow() - d).total_seconds()), 5)


class RouterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        app.dependency_overrides[require_login] = lambda: "admin"
        cls.client = TestClient(app, follow_redirects=False)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        app.dependency_overrides.clear()

    def setUp(self):
        with SessionLocal() as db:
            db.query(AccountNiche).delete()
            db.query(Account).delete()
            db.commit()
            acc = Account(identifier="router_niche_acc", enabled=True)
            db.add(acc)
            db.commit()
            self.acc_id = acc.id

    def _msg(self, r):
        return unquote_plus(r.headers["location"].split("msg=", 1)[-1])

    def test_page_renders_and_lists_nav_link(self):
        r = self.client.get("/niches")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Текущие ниши", r.text)
        self.assertIn("router_niche_acc", r.text)
        r2 = self.client.get("/accounts")
        self.assertIn('href="/niches"', r2.text)

    def test_add_then_shown_on_page(self):
        r = self.client.post(f"/niches/{self.acc_id}/add",
                             data={"title": "Окна", "description": "Пластик, монтаж", "active_from": "2026-09-25"})
        self.assertEqual(r.status_code, 303)
        self.assertIn("добавлена", self._msg(r))
        page = self.client.get("/niches").text
        self.assertIn("Окна", page)
        self.assertIn("25.09.2026", page)

    def test_add_bad_date_shows_error_and_saves_nothing(self):
        r = self.client.post(f"/niches/{self.acc_id}/add",
                             data={"title": "Окна", "description": "текст", "active_from": "не дата"})
        self.assertIn("формат даты", self._msg(r))
        with SessionLocal() as db:
            self.assertEqual(list_niches(db, self.acc_id), [])

    def test_delete(self):
        with SessionLocal() as db:
            n = add_niche(db, self.acc_id, "A", "ниша", "2026-09-25")
            niche_id = n.id
        r = self.client.post(f"/niches/{niche_id}/delete")
        self.assertEqual(r.status_code, 303)
        self.assertIn("удалена", self._msg(r))
        with SessionLocal() as db:
            self.assertEqual(list_niches(db, self.acc_id), [])

    def test_add_to_deleted_account_shows_error_via_http(self):
        r = self.client.post("/niches/999999/add",
                             data={"title": "A", "description": "ниша", "active_from": "2026-09-25"})
        self.assertIn("не существует", self._msg(r))

    def test_deleting_account_removes_its_niches_too(self):
        with SessionLocal() as db:
            add_niche(db, self.acc_id, "A", "ниша", "2026-09-25")
        r = self.client.post(f"/accounts/{self.acc_id}/delete")
        self.assertEqual(r.status_code, 303)
        with SessionLocal() as db:
            self.assertEqual(db.query(AccountNiche).filter_by(account_id=self.acc_id).count(), 0)

    def test_special_characters_render_escaped_not_executable(self):
        with SessionLocal() as db:
            add_niche(db, self.acc_id, "<b>жирный</b>", 'кавычки "и" «ёлочки»', "2026-09-25")
        html = self.client.get("/niches").text
        self.assertNotIn("<b>жирный</b>", html)          # не исполняемый HTML
        self.assertIn("&lt;b&gt;жирный&lt;/b&gt;", html)  # а экранированный текст


class WorkerIntegrationTests(WorkerBase):
    def setUp(self):
        _save()
        with SessionLocal() as db:
            db.query(AccountNiche).delete()
            db.commit()

    def _ready(self):
        acc, w = self.make_worker()
        w.client = MagicMock()

        class _Ctx:
            async def __aenter__(self): return None
            async def __aexit__(self, *a): return False
        w.client.action = MagicMock(return_value=_Ctx())
        w._pacer.next_delay = MagicMock(return_value=0.0)
        # _process() пишет DialogMessage на числовой account_id — если не убрать, эти строки
        # висят и после теста могут ошибочно достаться чужому аккаунту с тем же id, который
        # SQLite выдаст заново после того, как другой тестовый файл очистит таблицу accounts
        # (см. такой же фикс в test_database_wal.py/test_worker_silence.py)
        self.addCleanup(lambda: self._wipe(acc.id))
        return acc, w

    @staticmethod
    def _wipe(account_id):
        with SessionLocal() as db:
            db.query(DialogMessage).filter_by(account_id=account_id).delete()
            db.commit()

    async def test_niche_text_is_appended_to_the_prompt_after_its_start_date(self):
        acc, w = self._ready()
        with SessionLocal() as db:
            add_niche(db, acc.id, "Окна", "Пластиковые окна, монтаж", "2026-09-01")
        captured = {}

        async def fake_generate(system_prompt, history, user_text, **kw):
            captured["system_prompt"] = system_prompt
            return "ответ"

        ev = _event()
        ev.date = dt.datetime(2026, 9, 15, 12, 0, tzinfo=dt.timezone.utc)
        with patch.object(tw, "generate_reply", fake_generate), patch.object(tw, "typing_seconds", return_value=0.0):
            self.assertTrue(await w._process(ev))
        self.assertIn("Окна", captured["system_prompt"])
        self.assertIn("Пластиковые окна, монтаж", captured["system_prompt"])

    async def test_no_niche_block_when_message_predates_all_niches(self):
        acc, w = self._ready()
        with SessionLocal() as db:
            add_niche(db, acc.id, "Окна", "Пластиковые окна", "2026-09-25")
        captured = {}

        async def fake_generate(system_prompt, history, user_text, **kw):
            captured["system_prompt"] = system_prompt
            return "ответ"

        ev = _event()
        ev.date = dt.datetime(2026, 9, 10, 12, 0, tzinfo=dt.timezone.utc)  # раньше 25-го
        with patch.object(tw, "generate_reply", fake_generate), patch.object(tw, "typing_seconds", return_value=0.0):
            self.assertTrue(await w._process(ev))
        self.assertNotIn("Окна", captured["system_prompt"])

    async def test_uses_the_niche_active_on_the_message_date_not_the_latest_one(self):
        # запоздало разобранное старое сообщение (после простоя воркера) должно получить
        # контекст ТОЙ ниши, что действовала на момент его прихода, а не текущей
        acc, w = self._ready()
        with SessionLocal() as db:
            add_niche(db, acc.id, "Окна", "старая ниша: окна", "2026-09-01")
            add_niche(db, acc.id, "Двери", "новая ниша: двери", "2026-09-25")
        captured = {}

        async def fake_generate(system_prompt, history, user_text, **kw):
            captured["system_prompt"] = system_prompt
            return "ответ"

        ev = _event()
        ev.date = dt.datetime(2026, 9, 10, 12, 0, tzinfo=dt.timezone.utc)  # между 1-м и 25-м -> ниша "Окна"
        with patch.object(tw, "generate_reply", fake_generate), patch.object(tw, "typing_seconds", return_value=0.0):
            await w._process(ev)
        self.assertIn("старая ниша: окна", captured["system_prompt"])
        self.assertNotIn("новая ниша: двери", captured["system_prompt"])

    async def test_niche_added_even_with_custom_account_system_prompt(self):
        acc, w = self._ready()
        with SessionLocal() as db:
            db.get(Account, acc.id).system_prompt = "Ты особенный ассистент этого аккаунта."
            add_niche(db, acc.id, "Окна", "Пластиковые окна", "2026-09-01")
            db.commit()
        captured = {}

        async def fake_generate(system_prompt, history, user_text, **kw):
            captured["system_prompt"] = system_prompt
            return "ответ"

        ev = _event()
        ev.date = dt.datetime(2026, 9, 15, tzinfo=dt.timezone.utc)
        with patch.object(tw, "generate_reply", fake_generate), patch.object(tw, "typing_seconds", return_value=0.0):
            await w._process(ev)
        self.assertIn("особенный ассистент", captured["system_prompt"])
        self.assertIn("Окна", captured["system_prompt"])


if __name__ == "__main__":
    unittest.main()
