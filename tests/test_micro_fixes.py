"""Мелкие баги, найденные при общей проверке: история для нейросети, повторы и пустые ответы
провайдера, порт прокси, вход с битым хешем, лимиты диалога, числа из Excel, дата ниши."""
import datetime as dt
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from app.config import settings
from app.database import SessionLocal
from app.models import DialogMessage
from app.services import llm_client
from app.services.dialogs import DialogOverrideError, set_override
from app.services.niche import local_date
from app.services.proxy import ProxyConfigError, normalize_proxy, parse_proxy
from app.worker import telegram_worker as tw

from tests.test_worker_logic import WorkerBase, _event, _save


class _Ctx:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *a):
        return False


def _ready_worker(case: WorkerBase):
    acc, w = case.make_worker()
    w.client = MagicMock()
    w.client.action = MagicMock(return_value=_Ctx())
    w.client.get_messages = AsyncMock(return_value=[])          # «кто начал чат» — история пуста
    w.client.send_read_acknowledge = AsyncMock()
    w._pacer.next_delay = MagicMock(return_value=0.0)
    return acc, w


class HistoryTests(WorkerBase):
    async def test_current_message_is_not_sent_twice_and_media_gets_placeholder(self):
        _save()
        acc, w = _ready_worker(self)
        gen = AsyncMock(return_value="ответ")
        with patch.object(tw, "generate_reply", gen), patch.object(tw, "typing_seconds", return_value=0.0):
            await w._on_message(_event(msg_id=1, text="первое"))
            await w._on_message(_event(msg_id=2, text=""))            # стикер/фото без подписи
        _, history, user_message = gen.await_args_list[1].args[:3]
        self.assertEqual([h["content"] for h in history], ["первое", "ответ"])   # без дубля и без пустых
        self.assertIn("вложение без текста", user_message)
        first_history = gen.await_args_list[0].args[1]
        self.assertEqual(first_history, [])                         # самое первое сообщение — только user_message


def _resp(status: int, payload=None, text=""):
    req = httpx.Request("POST", "https://x/y")
    if payload is not None:
        return httpx.Response(status, json=payload, request=req)
    return httpx.Response(status, text=text, request=req)


class _FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        self.calls.append(json)
        return self.responses.pop(0)


class LLMClientTests(unittest.IsolatedAsyncioTestCase):
    def _patch(self, responses):
        fake = _FakeClient(responses)
        p1 = patch.object(llm_client.httpx, "AsyncClient", lambda *a, **k: fake)
        p2 = patch.object(llm_client, "RETRY_BASE_SECONDS", 0.0)
        p1.start(), p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)
        return fake

    async def test_retries_on_overload_then_succeeds(self):
        fake = self._patch([_resp(529, text="overloaded"), _resp(200, {"choices": [{"message": {"content": " ок "}}]})])
        out = await llm_client._call_openai_compatible("https://x", "k", "m", "sys", [], "hi", "P")
        self.assertEqual(out, "ок")
        self.assertEqual(len(fake.calls), 2)

    async def test_empty_or_malformed_reply_is_llm_error(self):
        self._patch([_resp(200, {"choices": [{"message": {"content": None}}]})])
        with self.assertRaises(llm_client.LLMError):
            await llm_client._call_openai_compatible("https://x", "k", "m", "sys", [], "hi", "P")
        self._patch([_resp(200, {"unexpected": True})])
        with self.assertRaises(llm_client.LLMError):
            await llm_client._call_openai_compatible("https://x", "k", "m", "sys", [], "hi", "P")

    async def test_anthropic_history_starts_with_user(self):
        fake = self._patch([_resp(200, {"content": [{"type": "text", "text": "ок"}]})])
        history = [{"role": "assistant", "content": "a"}, {"role": "user", "content": "u"}]
        await llm_client._call_anthropic("https://x", "k", "m", "sys", history, "hi", "A")
        self.assertEqual([m["role"] for m in fake.calls[0]["messages"]], ["user", "user"])


class SmallValidationTests(unittest.TestCase):
    def test_proxy_port_out_of_range_is_a_clear_error(self):
        with self.assertRaises(ProxyConfigError):
            normalize_proxy("1.2.3.4:99999:u:p")
        with self.assertRaises(ProxyConfigError):
            parse_proxy("socks5://u:p@h:70000")

    def test_login_with_broken_hash_is_wrong_password_not_crash(self):
        from app import auth

        with patch.object(settings, "admin_password_hash", "not-a-bcrypt-hash"):
            self.assertFalse(auth.verify_password("x"))

    def test_dialog_override_has_upper_bound(self):
        with SessionLocal() as db:
            with self.assertRaises(DialogOverrideError):
                set_override(db, 1, "1", "", str(10 ** 12))

    def test_niche_date_follows_schedule_timezone(self):
        late_utc = dt.datetime(2026, 9, 24, 22, 30)                       # 01:30 25-го по Москве
        self.assertEqual(local_date(late_utc, "Europe/Moscow"), dt.date(2026, 9, 25))
        self.assertEqual(local_date(late_utc, None), dt.date(2026, 9, 24))
        self.assertEqual(local_date(late_utc, "Nowhere/Zone"), dt.date(2026, 9, 24))

    def test_excel_float_identifier(self):
        import tempfile
        from pathlib import Path

        from openpyxl import Workbook

        from app.services.excel_loader import _read_rows

        wb = Workbook()
        wb.active.append(["Аккаунт", "Имя менеджера"])
        wb.active.append([79161234567.0, "Анна"])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.xlsx"
            wb.save(path)
            rows, _ = _read_rows(path)
        self.assertEqual(rows[0]["identifier"], "79161234567")


if __name__ == "__main__":
    unittest.main()
