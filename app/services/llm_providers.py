"""Реестр поддерживаемых ИИ-провайдеров.

Чисто справочные данные, без обращений к settings/БД — используется и
клиентом (llm_client.py), и веб-панелью (routers/settings_router.py) для
построения выпадающего списка. DeepSeek — провайдер по умолчанию.
"""

DEFAULT_PROVIDER = "deepseek"

# kind: "openai" — OpenAI-совместимый эндпоинт /chat/completions
#       "anthropic" — Messages API Anthropic (другой формат запроса/ответа)
PROVIDERS = {
    "deepseek": {
        "label": "DeepSeek (по умолчанию)",
        "kind": "openai",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "key_field": "deepseek_api_key",
        "env_var": "DEEPSEEK_API_KEY",
    },
    "openai": {
        "label": "OpenAI",
        "kind": "openai",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "key_field": "openai_api_key",
        "env_var": "OPENAI_API_KEY",
    },
    "anthropic": {
        "label": "Anthropic Claude",
        "kind": "anthropic",
        "base_url": "https://api.anthropic.com",
        "model": "claude-3-5-haiku-20241022",
        "key_field": "anthropic_api_key",
        "env_var": "ANTHROPIC_API_KEY",
    },
    "groq": {
        "label": "Groq",
        "kind": "openai",
        "base_url": "https://api.groq.com/openai/v1",
        "model": "llama-3.3-70b-versatile",
        "key_field": "groq_api_key",
        "env_var": "GROQ_API_KEY",
    },
    "mistral": {
        "label": "Mistral",
        "kind": "openai",
        "base_url": "https://api.mistral.ai/v1",
        "model": "mistral-large-latest",
        "key_field": "mistral_api_key",
        "env_var": "MISTRAL_API_KEY",
    },
    "openrouter": {
        "label": "OpenRouter (доступ к десяткам моделей разных провайдеров)",
        "kind": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "model": "deepseek/deepseek-chat",
        "key_field": "openrouter_api_key",
        "env_var": "OPENROUTER_API_KEY",
    },
}


def resolve_provider(provider_key: str | None) -> str:
    """Возвращает валидный ключ провайдера, откатываясь на дефолтный."""
    if provider_key in PROVIDERS:
        return provider_key
    return DEFAULT_PROVIDER
