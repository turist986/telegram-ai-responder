"""Ошибки нейросети должны быть понятны: кто ответил и что делать (а не голое «Ошибка API: 401»)."""
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.services import llm_client
from app.services.llm_client import LLMError, generate_reply, http_error_text
from app.services.llm_providers import DEFAULT_PROVIDER, PROVIDERS


def _response(status: int) -> httpx.Response:
    return httpx.Response(status, text="{}", request=httpx.Request("POST", "http://llm.test/x"))


class HttpErrorTextTests(unittest.TestCase):
    def test_401_explains_key_problem_and_names_provider(self):
        text = http_error_text("DeepSeek", 401)
        self.assertIn("401", text)
        self.assertIn("DeepSeek", text)
        self.assertIn("ключ", text)
        self.assertIn("Нейросеть", text)

    def test_known_and_unknown_codes(self):
        self.assertIn("баланс", http_error_text("X", 402))
        self.assertIn("лимит", http_error_text("X", 429))
        self.assertIn("модел", http_error_text("X", 404))
        self.assertIn("500", http_error_text("X", 500))
        self.assertIn("ИИ-провайдера", http_error_text("", 401))


class GenerateReplyErrorTests(unittest.IsolatedAsyncioTestCase):
    async def _fail(self, provider: str, status: int) -> str:
        with patch.object(httpx.AsyncClient, "post", AsyncMock(return_value=_response(status))):
            with self.assertRaises(LLMError) as cm:
                await generate_reply("sys", [], "hi", provider=provider, api_key="bad-key")
        return str(cm.exception)

    async def test_openai_compatible_401(self):
        msg = await self._fail(DEFAULT_PROVIDER, 401)
        self.assertIn("401", msg)
        self.assertIn(PROVIDERS[DEFAULT_PROVIDER]["label"], msg)
        self.assertNotEqual(msg, "Ошибка API: 401")

    async def test_anthropic_401(self):
        msg = await self._fail("anthropic", 401)
        self.assertIn("401", msg)
        self.assertIn(PROVIDERS["anthropic"]["label"], msg)

    async def test_missing_key_message_unchanged(self):
        post = AsyncMock(side_effect=AssertionError("сетевой запрос без ключа"))
        with patch.object(llm_client, "_api_key_for", lambda provider: None), \
                patch.object(httpx.AsyncClient, "post", post):
            with self.assertRaises(LLMError) as cm:
                await generate_reply("sys", [], "hi", provider=DEFAULT_PROVIDER, api_key="")
        self.assertIn("Не задан API-ключ", str(cm.exception))
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
