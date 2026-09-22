"""Автостоп при FloodWait / PeerFlood: расчёт паузы с ростом при повторах.

Правило: пауза никогда не короче требования Telegram, а при повторных срабатываниях
подряд растёт геометрически (множитель из настроек) до потолка. Серия сбрасывается,
если аккаунт «молчал» без флудов дольше заданного времени."""
import datetime as dt
import random


def next_streak(prev_streak: int, last_at: dt.datetime | None, now: dt.datetime, reset_hours: float) -> int:
    if not prev_streak or last_at is None or now - last_at > dt.timedelta(hours=reset_hours):
        return 1
    return prev_streak + 1


def compute_pause(streak: int, wait_seconds: int, cfg: dict, peer_flood: bool = False,
                  rng: random.Random | None = None) -> float:
    """Секунды паузы для streak-го подряд срабатывания (streak >= 1)."""
    rng = rng or random
    wait = max(0, int(wait_seconds))
    if not cfg["flood_stop_enabled"]:
        return wait + 1.0
    if peer_flood:
        base = max(cfg["peer_flood_pause_minutes"] * 60.0, cfg["flood_extra_pause_seconds"])
    else:
        base = wait + cfg["flood_extra_pause_seconds"]
    pause = base * cfg["flood_multiplier"] ** (max(1, streak) - 1)
    # потолок из настроек не может опустить паузу ниже того, что потребовал сам Telegram,
    # а для PeerFlood — ниже явно заданной паузы PeerFlood
    floor = max(wait, base if peer_flood else 0)
    pause = min(pause, max(cfg["flood_max_pause_seconds"], floor))
    return pause * rng.uniform(1.0, 1.15)
