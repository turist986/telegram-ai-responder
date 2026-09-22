import json

from sqlalchemy.orm import Session

from ..config import settings as env_settings
from ..models import GlobalSetting
from .llm_providers import DEFAULT_PROVIDER, PROVIDERS, resolve_provider
from .schedule import ScheduleConfigError, parse_hhmm, parse_minutes

_DEFAULTS = {
    "global_enabled": "true",
    "llm_provider": DEFAULT_PROVIDER,
    "prompt_enabled": "true",
    "kb_enabled": "true",
    "work_hours_enabled": "false",
    "break_enabled": "false",
    "keep_active_dialog_enabled": "false",
    "keep_active_dialog_minutes": "30",
}

_DEFAULT_WORK_WINDOWS = [{"start": "09:00", "end": "21:00"}]
_DEFAULT_BREAK_WINDOWS = [{"start": "13:00", "end": "14:00"}]


def get_setting(db: Session, key: str, default: str | None = None) -> str | None:
    row = db.get(GlobalSetting, key)
    if row is not None:
        return row.value
    return _DEFAULTS.get(key, default)


def set_setting(db: Session, key: str, value: str) -> None:
    row = db.get(GlobalSetting, key)
    if row is None:
        row = GlobalSetting(key=key, value=value)
        db.add(row)
    else:
        row.value = value
    db.commit()


def is_global_enabled(db: Session) -> bool:
    return get_setting(db, "global_enabled", "true") == "true"


def is_prompt_enabled(db: Session) -> bool:
    return get_setting(db, "prompt_enabled", "true") == "true"


def is_kb_enabled(db: Session) -> bool:
    return get_setting(db, "kb_enabled", "true") == "true"


def get_llm_settings(db: Session) -> dict:
    """Активный ИИ-провайдер, необязательные переопределения модели/base_url
    и разрешённый ключ (панель приоритетнее .env), выставленные в «Настройках»."""
    provider = resolve_provider(get_setting(db, "llm_provider", DEFAULT_PROVIDER))
    return {
        "provider": provider,
        "model": get_setting(db, "llm_model") or None,
        "base_url": get_setting(db, "llm_base_url") or None,
        "api_key": get_llm_api_key(db, provider),
    }


def set_llm_settings(db: Session, provider: str, model: str = "", base_url: str = "") -> None:
    set_setting(db, "llm_provider", resolve_provider(provider))
    set_setting(db, "llm_model", model.strip())
    set_setting(db, "llm_base_url", base_url.strip())


def _api_key_setting_key(provider_key: str) -> str:
    return f"llm_api_key_{provider_key}"


def is_llm_key_set_in_panel(db: Session, provider_key: str) -> bool:
    return bool(get_setting(db, _api_key_setting_key(provider_key)))


def get_llm_api_key(db: Session, provider_key: str) -> str | None:
    """Ключ для провайдера: приоритет у сохранённого в панели, иначе —
    значение из .env (если задано)."""
    if provider_key not in PROVIDERS:
        return None
    stored = get_setting(db, _api_key_setting_key(provider_key))
    if stored:
        return stored
    return getattr(env_settings, PROVIDERS[provider_key]["key_field"]) or None


def set_llm_api_key(db: Session, provider_key: str, api_key: str) -> None:
    if provider_key not in PROVIDERS:
        raise ValueError(f"Неизвестный провайдер «{provider_key}»")
    set_setting(db, _api_key_setting_key(provider_key), api_key.strip())


def _load_windows(db: Session, key: str, default: list[dict]) -> list[dict]:
    raw = get_setting(db, key)
    if not raw:
        return default
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return default
    if not isinstance(data, list) or not data:
        return default
    return data


def _save_windows(db: Session, key: str, windows: list[tuple[str, str]]) -> None:
    set_setting(db, key, json.dumps([{"start": s.strip(), "end": e.strip()} for s, e in windows]))


class ProtectionConfigError(ValueError):
    pass


# key -> (тип, значение по умолчанию, минимум, максимум, подпись для сообщений об ошибке)
_PROTECTION_NUMERIC = {
    "poll_interval_seconds": (int, 60, 15, 3600, "Период проверки обновлений"),
    "poll_jitter_pct": (int, 40, 0, 90, "Разброс периода проверки, %"),
    "catchup_interval_seconds": (int, 300, 60, 7200, "Период проверки непрочитанных"),
    "fixed_delay_min": (float, 4.0, 0, 600, "Фикс. задержка: от"),
    "fixed_delay_max": (float, 12.0, 0, 600, "Фикс. задержка: до"),
    "session_idle_minutes": (int, 20, 1, 720, "Конец сессии после простоя, мин"),
    "fast_replies": (int, 3, 0, 50, "Число быстрых ответов в начале сессии"),
    "fast_delay_min": (float, 3.0, 0, 600, "Быстрые ответы: от"),
    "fast_delay_max": (float, 9.0, 0, 600, "Быстрые ответы: до"),
    "slow_delay_min": (float, 25.0, 0, 1800, "Медленные ответы: от"),
    "slow_delay_max": (float, 120.0, 0, 1800, "Медленные ответы: до"),
    "ramp_replies": (int, 4, 1, 50, "За сколько ответов темп замедляется"),
    "spread": (float, 0.35, 0.0, 1.5, "Разброс характера сессии"),
    "max_delay_seconds": (float, 300.0, 5, 1800, "Потолок задержки, с"),
    "typing_cps": (float, 6.0, 1.0, 30.0, "Скорость «печати», симв./с"),
    "flood_extra_pause_seconds": (int, 30, 0, 86400, "Запас к паузе FloodWait, с"),
    "flood_multiplier": (float, 2.0, 1.0, 10.0, "Множитель роста паузы"),
    "flood_max_pause_seconds": (int, 21600, 60, 604800, "Потолок паузы, с"),
    "flood_reset_hours": (int, 24, 1, 720, "Сброс серии флудов через, ч"),
    "peer_flood_pause_minutes": (int, 360, 1, 10080, "Пауза при PeerFlood, мин"),
    "reply_age_minutes": (int, 30, 1, 10080, "Возрастной порог ответа, мин"),
    # Сколько аккаунтов могут делить одно приложение (api_id/api_hash). Официальные клиенты
    # Telegram сами используют один api_id на всех пользователей — это не признак связи
    # аккаунтов; экономит только проходы через my.telegram.org при добавлении. Прокси,
    # сессия и профиль устройства при этом всё равно остаются уникальными на аккаунт.
    "api_pool_max_accounts": (int, 5, 1, 10, "Макс. аккаунтов на один api_id"),
}
_PROTECTION_BOOLS = {"flood_stop_enabled": True, "reply_age_enabled": True}
_PROTECTION_CHOICES = {
    "reply_mode": ("sticky", ("sticky", "fixed")),
    "checking_preset": ("standard", ("standard", "realistic", "quiet", "custom")),
}
# Пресеты периода проверки: (период, разброс %, период проверки непрочитанных)
CHECKING_PRESETS = {
    "standard": (60, 30, 300),
    "realistic": (90, 60, 420),
    "quiet": (180, 50, 600),
}
_PROTECTION_PREFIX = "prot_"


def get_protection(db: Session) -> dict:
    """Все настройки темпа/проверок/автостопа/возраста одним запросом, с типами и умолчаниями.
    Битое значение в БД заменяется умолчанием — воркер не должен падать из-за настроек."""
    stored = {r.key: r.value for r in db.query(GlobalSetting).filter(GlobalSetting.key.like(_PROTECTION_PREFIX + "%"))}
    out: dict = {}
    for key, (typ, default, lo, hi, _label) in _PROTECTION_NUMERIC.items():
        try:
            value = typ(stored[_PROTECTION_PREFIX + key])
            out[key] = min(max(value, lo), hi)
        except (KeyError, TypeError, ValueError):
            out[key] = default
    for key, default in _PROTECTION_BOOLS.items():
        raw = stored.get(_PROTECTION_PREFIX + key)
        out[key] = default if raw is None else raw == "true"
    for key, (default, allowed) in _PROTECTION_CHOICES.items():
        raw = stored.get(_PROTECTION_PREFIX + key)
        out[key] = raw if raw in allowed else default
    return out


def set_protection(db: Session, form: dict) -> None:
    """Валидирует и сохраняет настройки. form — сырые строки из формы (флажки: есть/нет).
    Бросает ProtectionConfigError с понятным текстом; при ошибке ничего не записывается."""
    values: dict[str, str] = {}
    for key, (default, allowed) in _PROTECTION_CHOICES.items():
        raw = str(form.get(key, default))
        if raw not in allowed:
            raise ProtectionConfigError(f"Недопустимое значение поля «{key}»")
        values[key] = raw
    for key in _PROTECTION_BOOLS:
        values[key] = "true" if form.get(key) else "false"

    numeric: dict[str, float] = {}
    for key, (typ, default, lo, hi, label) in _PROTECTION_NUMERIC.items():
        raw = form.get(key)
        if raw is None or str(raw).strip() == "":
            numeric[key] = default
            continue
        try:
            value = typ(str(raw).strip().replace(",", "."))
        except ValueError:
            raise ProtectionConfigError(f"«{label}»: нужно число")
        if not lo <= value <= hi:
            raise ProtectionConfigError(f"«{label}»: допустимо от {lo} до {hi}")
        numeric[key] = value

    for a, b, label in (
        ("fixed_delay_min", "fixed_delay_max", "Фиксированная задержка"),
        ("fast_delay_min", "fast_delay_max", "Быстрые ответы"),
        ("slow_delay_min", "slow_delay_max", "Медленные ответы"),
    ):
        if numeric[a] > numeric[b]:
            raise ProtectionConfigError(f"{label}: «от» не может быть больше «до»")

    preset = values["checking_preset"]
    if preset != "custom":
        numeric["poll_interval_seconds"], numeric["poll_jitter_pct"], numeric["catchup_interval_seconds"] = (
            CHECKING_PRESETS[preset]
        )

    for key, value in {**values, **{k: str(v) for k, v in numeric.items()}}.items():
        row = db.get(GlobalSetting, _PROTECTION_PREFIX + key)
        if row is None:
            db.add(GlobalSetting(key=_PROTECTION_PREFIX + key, value=value))
        else:
            row.value = value
    db.commit()


def get_schedule_settings(db: Session) -> dict:
    """Расписание работы автоответчика (рабочие часы, перерыв, продление
    активного диалога) — см. app/services/schedule.py за логикой применения.
    work_windows/break_windows — списки {"start": "ЧЧ:ММ", "end": "ЧЧ:ММ"},
    можно задать несколько периодов (например, сплит-график, два перерыва)."""
    try:
        keep_minutes = int(get_setting(db, "keep_active_dialog_minutes", "30"))
    except (TypeError, ValueError):
        keep_minutes = 30
    return {
        "work_hours_enabled": get_setting(db, "work_hours_enabled") == "true",
        "work_windows": _load_windows(db, "work_windows", _DEFAULT_WORK_WINDOWS),
        "break_enabled": get_setting(db, "break_enabled") == "true",
        "break_windows": _load_windows(db, "break_windows", _DEFAULT_BREAK_WINDOWS),
        "keep_active_dialog_enabled": get_setting(db, "keep_active_dialog_enabled") == "true",
        "keep_active_dialog_minutes": keep_minutes,
    }


def set_schedule_settings(
    db: Session,
    work_hours_enabled: bool,
    work_windows: list[tuple[str, str]],
    break_enabled: bool,
    break_windows: list[tuple[str, str]],
    keep_active_dialog_enabled: bool,
    keep_active_dialog_minutes: str,
) -> None:
    """Валидирует и сохраняет расписание. Бросает ScheduleConfigError при
    некорректном формате времени/минут — вызывающий код должен её ловить.
    work_windows/break_windows — списки пар (начало, конец), можно несколько."""
    if work_hours_enabled and not work_windows:
        raise ScheduleConfigError("Добавьте хотя бы один период работы")
    if break_enabled and not break_windows:
        raise ScheduleConfigError("Добавьте хотя бы один период перерыва")

    # parse_hhmm бросает ScheduleConfigError при некорректном значении —
    # валидируем всё сразу, до записи в БД, чтобы не сохранить частично
    # неверные данные.
    for start, end in work_windows:
        parse_hhmm(start)
        parse_hhmm(end)
    for start, end in break_windows:
        parse_hhmm(start)
        parse_hhmm(end)
    minutes = parse_minutes(keep_active_dialog_minutes)

    set_setting(db, "work_hours_enabled", "true" if work_hours_enabled else "false")
    _save_windows(db, "work_windows", work_windows)
    set_setting(db, "break_enabled", "true" if break_enabled else "false")
    _save_windows(db, "break_windows", break_windows)
    set_setting(db, "keep_active_dialog_enabled", "true" if keep_active_dialog_enabled else "false")
    set_setting(db, "keep_active_dialog_minutes", str(minutes))
