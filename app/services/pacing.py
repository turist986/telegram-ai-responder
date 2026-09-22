"""Темп ответов: «сессия» активности аккаунта.

Режим sticky имитирует человека, который взял телефон: первые ответы в сессии идут
быстро, дальше задержки плавно растут. Характер сессии (множитель скорости) выбирается
один раз при её старте и держится до конца — «липкая» сессия, а не независимый шум на
каждом сообщении. Сессия заканчивается после паузы без активности.

Модуль без обращений к Telegram/БД — только арифметика, поэтому легко тестируется."""
import math
import random
import time


class SessionPacer:
    def __init__(self, rng: random.Random | None = None):
        self._rng = rng or random.Random()
        self._last_activity: float | None = None
        self._count = 0
        self._speed = 1.0

    @property
    def replies_in_session(self) -> int:
        return self._count

    def _session_expired(self, cfg: dict, now: float) -> bool:
        return self._last_activity is None or now - self._last_activity > cfg["session_idle_minutes"] * 60

    def next_delay(self, cfg: dict, now: float | None = None) -> float:
        """Сколько секунд «думать» перед ответом (без учёта времени набора)."""
        now = time.monotonic() if now is None else now
        rng = self._rng
        if cfg["reply_mode"] != "sticky":
            self._last_activity = now
            return rng.uniform(cfg["fixed_delay_min"], cfg["fixed_delay_max"])

        if self._session_expired(cfg, now):
            self._count = 0
            # lognormal: чаще около 1.0, но бывают заметно быстрые и заметно медленные сессии
            self._speed = min(2.5, max(0.5, math.exp(rng.gauss(0.0, cfg["spread"]))))

        idx = self._count
        fast = cfg["fast_replies"]
        if idx < fast:
            lo, hi = cfg["fast_delay_min"], cfg["fast_delay_max"]
        else:
            t = min(1.0, (idx - fast + 1) / max(1, cfg["ramp_replies"]))
            lo = cfg["fast_delay_min"] + (cfg["slow_delay_min"] - cfg["fast_delay_min"]) * t
            hi = cfg["fast_delay_max"] + (cfg["slow_delay_max"] - cfg["fast_delay_max"]) * t
        delay = rng.uniform(lo, hi) * self._speed
        self._count += 1
        self._last_activity = now
        return min(delay, cfg["max_delay_seconds"])

    def touch(self, now: float | None = None) -> None:
        """Отметить активность (после отправки ответа) — продлевает сессию."""
        self._last_activity = time.monotonic() if now is None else now


def typing_seconds(text_len: int, cps: float, rng: random.Random | None = None) -> float:
    """Сколько показывать «печатает…»: пропорционально длине ответа, в разумных пределах."""
    rng = rng or random
    base = text_len / max(cps, 0.1)
    return min(25.0, max(1.5, base * rng.uniform(0.8, 1.2)))


def jittered(base: float, jitter_pct: float, rng: random.Random | None = None) -> float:
    """base ± jitter_pct процентов (для интервалов проверки)."""
    rng = rng or random
    spread = base * jitter_pct / 100.0
    return max(1.0, rng.uniform(base - spread, base + spread))
