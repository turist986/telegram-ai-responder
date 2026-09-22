"""Стабильный «профиль устройства» аккаунта.

Telegram видит device_model / system_version / app_version / lang_code в каждом
initConnection и показывает их в списке активных сеансов. Telethon по умолчанию берёт их
из ОС сервера и версии библиотеки — у всех аккаунтов получается одинаковый «сервер».

Принципы:
  * профиль выбирается ОДИН раз при добавлении аккаунта и хранится в БД — рестарты не
    меняют устройство;
  * профиль внутренне согласован (ОС ↔ модель ↔ версия приложения) и соответствует
    собственному приложению аккаунта (свой api_id): мы не выдаём себя за официальный
    Telegram Desktop, чьи сигнатуры привязаны к его api_id;
  * разные аккаунты получают разные профили.

Список можно заменить своим: data/device_profiles.json — массив объектов с полями
device_model, system_version, app_version."""
import json
import random
from pathlib import Path

from ..config import settings

_DEFAULT_PROFILES = [
    {"device_model": "PC 64bit", "system_version": "Windows 10", "app_version": "1.4.2"},
    {"device_model": "PC 64bit", "system_version": "Windows 11", "app_version": "1.4.2"},
    {"device_model": "PC 64bit", "system_version": "Windows 11", "app_version": "1.5.0"},
    {"device_model": "Desktop", "system_version": "Windows 10", "app_version": "1.3.8"},
    {"device_model": "Desktop", "system_version": "Windows 11", "app_version": "1.5.1"},
    {"device_model": "MacBook Air", "system_version": "macOS 14.5", "app_version": "1.4.2"},
    {"device_model": "MacBook Pro", "system_version": "macOS 14.6", "app_version": "1.5.0"},
    {"device_model": "MacBook Pro", "system_version": "macOS 15.1", "app_version": "1.5.1"},
]

# язык интерфейса по умолчанию: (lang_code, system_lang_code)
DEFAULT_LANG = ("ru", "ru-RU")


def _load_profiles() -> list[dict]:
    path: Path = settings.data_dir / "device_profiles.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        profiles = [p for p in data if all(p.get(k) for k in ("device_model", "system_version", "app_version"))]
        if profiles:
            return profiles
    except (OSError, ValueError, TypeError):
        pass
    return _DEFAULT_PROFILES


def generate_profile(used: set[tuple] | None = None, lang: tuple[str, str] | None = None,
                     rng: random.Random | None = None) -> dict:
    """Профиль для нового аккаунта. used — уже занятые кортежи (model, os, version):
    по возможности выбираем непохожий на существующие."""
    rng = rng or random.Random()
    profiles = _load_profiles()
    used = used or set()
    fresh = [p for p in profiles if (p["device_model"], p["system_version"], p["app_version"]) not in used]
    chosen = rng.choice(fresh or profiles)
    lang_code, system_lang = lang or DEFAULT_LANG
    return {**{k: chosen[k] for k in ("device_model", "system_version", "app_version")},
            "lang_code": lang_code, "system_lang_code": system_lang}


def client_kwargs(account) -> dict:
    """Параметры TelegramClient из полей аккаунта. Если профиль не задан (старый аккаунт) —
    пустой словарь: поведение Telethon не меняем, чтобы не «переобувать» живую сессию."""
    if not account.device_model:
        return {}
    kw = {
        "device_model": account.device_model,
        "system_version": account.system_version,
        "app_version": account.app_version,
    }
    if account.lang_code:
        kw["lang_code"] = account.lang_code
        kw["system_lang_code"] = account.system_lang_code or account.lang_code
    return kw
