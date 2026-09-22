import datetime as dt
import random
import statistics
import unittest

from app.services.flood_guard import compute_pause, next_streak
from app.services.pacing import SessionPacer, jittered, typing_seconds

STICKY = {
    "reply_mode": "sticky", "fixed_delay_min": 4, "fixed_delay_max": 12, "session_idle_minutes": 20,
    "fast_replies": 3, "fast_delay_min": 3, "fast_delay_max": 9, "slow_delay_min": 25,
    "slow_delay_max": 120, "ramp_replies": 4, "spread": 0.0, "max_delay_seconds": 300,
}
FLOOD = {
    "flood_stop_enabled": True, "flood_extra_pause_seconds": 30, "flood_multiplier": 2.0,
    "flood_max_pause_seconds": 3600, "flood_reset_hours": 24, "peer_flood_pause_minutes": 360,
}


class PacingTests(unittest.TestCase):
    def test_fast_then_slow_within_session(self):
        p = SessionPacer(random.Random(1))
        delays = [p.next_delay(STICKY, now=i * 30.0) for i in range(10)]
        self.assertTrue(all(3 <= d <= 9 for d in delays[:3]), delays)       # быстрые
        self.assertGreater(statistics.mean(delays[5:]), statistics.mean(delays[:3]) * 3)  # затем медленные
        self.assertTrue(all(d <= 120 for d in delays))

    def test_idle_starts_new_fast_session(self):
        p = SessionPacer(random.Random(2))
        for i in range(8):
            p.next_delay(STICKY, now=i * 30.0)
        d = p.next_delay(STICKY, now=8 * 30.0 + 21 * 60)  # простой > 20 мин
        self.assertLessEqual(d, 9)
        self.assertEqual(p.replies_in_session, 1)

    def test_sticky_speed_is_per_session(self):
        cfg = {**STICKY, "spread": 0.8}
        speeds = set()
        for seed in range(6):
            p = SessionPacer(random.Random(seed))
            a = p.next_delay({**cfg, "fast_delay_min": 10, "fast_delay_max": 10}, now=0.0)
            b = p.next_delay({**cfg, "fast_delay_min": 10, "fast_delay_max": 10}, now=1.0)
            self.assertAlmostEqual(a, b)  # внутри сессии множитель один и тот же
            speeds.add(round(a, 3))
        self.assertGreater(len(speeds), 3)  # между сессиями — разный

    def test_fixed_mode_and_ceiling(self):
        p = SessionPacer(random.Random(3))
        d = p.next_delay({**STICKY, "reply_mode": "fixed"}, now=0.0)
        self.assertTrue(4 <= d <= 12)
        cap = {**STICKY, "fast_delay_min": 500, "fast_delay_max": 600, "max_delay_seconds": 60}
        self.assertLessEqual(SessionPacer(random.Random(4)).next_delay(cap, now=0.0), 60)

    def test_typing_and_jitter_bounds(self):
        self.assertGreaterEqual(typing_seconds(1, 6), 1.5)
        self.assertLessEqual(typing_seconds(10_000, 6), 25)
        for _ in range(200):
            v = jittered(60, 40)
            self.assertTrue(36 <= v <= 84)


class FloodGuardTests(unittest.TestCase):
    def test_growth_and_never_below_telegram_wait(self):
        rng = random.Random(0)
        p1 = compute_pause(1, 100, FLOOD, rng=rng)
        p2 = compute_pause(2, 100, FLOOD, rng=rng)
        p3 = compute_pause(3, 100, FLOOD, rng=rng)
        self.assertTrue(130 <= p1 <= 130 * 1.15 + 1)
        self.assertTrue(p1 < p2 < p3)
        self.assertGreaterEqual(p3, 130 * 4)

    def test_cap_but_not_below_wait(self):
        rng = random.Random(0)
        self.assertLessEqual(compute_pause(20, 100, FLOOD, rng=rng), 3600 * 1.15)
        big = compute_pause(1, 90_000, FLOOD, rng=rng)  # Telegram потребовал больше потолка — слушаемся Telegram
        self.assertGreaterEqual(big, 90_000)

    def test_peer_flood_and_disabled(self):
        # PeerFlood: 360 мин из настроек, даже если общий потолок паузы меньше (1 ч)
        self.assertGreaterEqual(compute_pause(1, 0, FLOOD, peer_flood=True, rng=random.Random(0)), 360 * 60)
        off = {**FLOOD, "flood_stop_enabled": False}
        self.assertEqual(compute_pause(5, 40, off), 41)

    def test_streak_reset(self):
        now = dt.datetime(2026, 1, 2, 12, 0)
        self.assertEqual(next_streak(0, None, now, 24), 1)
        self.assertEqual(next_streak(2, now - dt.timedelta(hours=1), now, 24), 3)
        self.assertEqual(next_streak(5, now - dt.timedelta(hours=25), now, 24), 1)


if __name__ == "__main__":
    unittest.main()
