from ..config import settings
from ..models import Account


def build_disclaimer(account: Account) -> str:
    """Возвращает текст дисклеймера, который добавляется в конец каждого ИИ-ответа.

    Приоритет:
      1. Явный текст в столбце "Дисклеймер" managers.xlsx для этого аккаунта.
      2. Шаблон по умолчанию, подставляется имя менеджера (столбец "Имя менеджера").
      3. Полная дефолтная заглушка из .env, если и имя не задано.
    """
    if account.disclaimer_marker:
        return account.disclaimer_marker

    if account.manager_name:
        return settings.default_disclaimer_template.format(name=account.manager_name)

    return settings.default_fallback_disclaimer
