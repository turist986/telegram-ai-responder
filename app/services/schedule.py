import datetime as dt


class ScheduleConfigError(ValueError):
    pass


def parse_hhmm(value: str) -> dt.time:
    """Разбирает строку вида "09:00" во время. Бросает ScheduleConfigError
    при некорректном формате (в т.ч. часы/минуты вне допустимого диапазона)."""
    try:
        hh, mm = value.strip().split(":")
        return dt.time(int(hh), int(mm))
    except (ValueError, AttributeError):
        raise ScheduleConfigError(
            f"Неверный формат времени «{value}» — используйте ЧЧ:ММ, например 09:00"
        )


def parse_minutes(value: str) -> int:
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        raise ScheduleConfigError("Таймаут бездействия должен быть целым числом минут")
    if minutes < 1:
        raise ScheduleConfigError("Таймаут бездействия должен быть не меньше 1 минуты")
    return minutes


def _in_window(now: dt.time, start: dt.time, end: dt.time) -> bool:
    if start <= end:
        return start <= now <= end
    # Окно через полночь, например 22:00–06:00
    return now >= start or now <= end


def _in_any_window(now: dt.time, windows: list[tuple[str, str]]) -> bool:
    return any(_in_window(now, parse_hhmm(start), parse_hhmm(end)) for start, end in windows)


def is_within_work_hours(
    now: dt.datetime,
    work_windows: list[tuple[str, str]],
    break_enabled: bool,
    break_windows: list[tuple[str, str]],
) -> bool:
    """True, если текущее время попадает хотя бы в одно рабочее окно и не
    попадает ни в одно окно перерыва. Пустой список рабочих окон трактуется
    как «без ограничения» (безопасный дефолт — не блокировать ответы из-за
    забытой/пустой настройки)."""
    t = now.time()
    if work_windows and not _in_any_window(t, work_windows):
        return False
    if break_enabled and break_windows and _in_any_window(t, break_windows):
        return False
    return True
