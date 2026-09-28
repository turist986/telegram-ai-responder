import datetime as dt
import logging

logger = logging.getLogger(__name__)


class ScheduleConfigError(ValueError):
    pass


def schedule_now(tz_name: str | None = None) -> tuple[dt.datetime, str]:
    """Текущее время для проверки расписания и подпись часового пояса.

    Без tz_name — время сервера. Это ловушка: VPS почти всегда в чужом поясе (у Windows-VPS
    по умолчанию часто Pacific/UTC), а рабочие окна менеджеры задают в СВОЁМ времени — окно
    09:00–21:00 у московского менеджера при сервере в UTC-7 «закрывалось» бы посреди дня, а
    ответов не было. Поэтому пояс задаётся SCHEDULE_TIMEZONE в .env (например Europe/Moscow).
    Неизвестный пояс не должен ломать ответы — предупреждаем и берём время сервера."""
    if tz_name:
        try:
            from zoneinfo import ZoneInfo

            return dt.datetime.now(ZoneInfo(tz_name)).replace(tzinfo=None), tz_name
        except Exception as exc:  # noqa: BLE001 — нет tzdata / опечатка в имени пояса
            logger.warning("SCHEDULE_TIMEZONE=%r не распознан (%s) — расписание идёт по времени сервера. "
                           "На Windows нужен пакет tzdata: pip install tzdata", tz_name, exc)
    return dt.datetime.now(), "время сервера"


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
