import logging

import httpx

from ..config import settings
from .llm_providers import DEFAULT_PROVIDER, PROVIDERS, resolve_provider

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


def _api_key_for(provider_key: str) -> str | None:
    return getattr(settings, PROVIDERS[provider_key]["key_field"])


async def generate_reply(
    system_prompt: str,
    history: list[dict],
    user_message: str,
    provider: str = DEFAULT_PROVIDER,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> str:
    """Генерирует ответ через выбранный ИИ-провайдер.

    provider/model/base_url/api_key обычно приходят из панели «Настройки»
    (см. services/settings_store.get_llm_settings, где ключ уже разрешён —
    приоритет у сохранённого в панели, иначе .env); пустые model/base_url
    заменяются дефолтами провайдера из llm_providers.PROVIDERS. Если
    api_key не передан явно, используется значение только из .env — как
    резервный путь для прямых вызовов в обход settings_store.
    """
    provider_key = resolve_provider(provider)
    config = PROVIDERS[provider_key]

    api_key = api_key or _api_key_for(provider_key)
    if not api_key:
        raise LLMError(
            f"Не задан API-ключ для провайдера «{config['label']}» — "
            f"вставьте его в панели («Настройки» → «Нейросеть») или задайте "
            f"переменную {config['env_var']} в .env"
        )

    resolved_model = model or config["model"]
    resolved_base_url = (base_url or config["base_url"]).rstrip("/")

    if config["kind"] == "anthropic":
        return await _call_anthropic(resolved_base_url, api_key, resolved_model, system_prompt, history, user_message)
    return await _call_openai_compatible(resolved_base_url, api_key, resolved_model, system_prompt, history, user_message)


async def _call_openai_compatible(
    base_url: str, api_key: str, model: str, system_prompt: str, history: list[dict], user_message: str
) -> str:
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    payload = {"model": model, "messages": messages, "temperature": 0.4, "max_tokens": 700}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    url = f"{base_url}/chat/completions"

    # trust_env=False: не подхватывать системные HTTP_PROXY/ALL_PROXY —
    # запросы к ИИ-провайдеру всегда идут напрямую, независимо от того,
    # что настроено в сети сервера/машины (иначе httpx падает на схемах
    # вроде socks4:// без доп. зависимости, либо просто ломает вызовы,
    # даже если сам ключ и провайдер настроены верно).
    async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"].strip()
            except httpx.HTTPStatusError as exc:
                logger.warning("LLM HTTP %s: %s", exc.response.status_code, exc.response.text[:300])
                last_exc = exc
                if exc.response.status_code in (429, 500, 502, 503) and attempt < 2:
                    continue
                raise LLMError(f"Ошибка API: {exc.response.status_code}") from exc
            except httpx.HTTPError as exc:
                logger.warning("LLM network error (attempt %s): %s", attempt, exc)
                last_exc = exc
                if attempt < 2:
                    continue
                raise LLMError(str(exc)) from exc

    raise LLMError(f"API недоступен после повторных попыток: {last_exc}")


async def _call_anthropic(
    base_url: str, api_key: str, model: str, system_prompt: str, history: list[dict], user_message: str
) -> str:
    messages = list(history)
    messages.append({"role": "user", "content": user_message})

    payload = {"model": model, "system": system_prompt, "messages": messages, "max_tokens": 700}
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    url = f"{base_url}/v1/messages"

    async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                return "".join(block["text"] for block in data["content"] if block["type"] == "text").strip()
            except httpx.HTTPStatusError as exc:
                logger.warning("LLM HTTP %s: %s", exc.response.status_code, exc.response.text[:300])
                last_exc = exc
                if exc.response.status_code in (429, 500, 502, 503) and attempt < 2:
                    continue
                raise LLMError(f"Ошибка API: {exc.response.status_code}") from exc
            except httpx.HTTPError as exc:
                logger.warning("LLM network error (attempt %s): %s", attempt, exc)
                last_exc = exc
                if attempt < 2:
                    continue
                raise LLMError(str(exc)) from exc

    raise LLMError(f"API недоступен после повторных попыток: {last_exc}")
