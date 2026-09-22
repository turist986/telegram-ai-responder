import socket
import threading
import unittest

import socks

from app.database import SessionLocal, init_db
from app.models import GlobalSetting
from app.services.api_app_creator import parse_credentials
from app.services.local_socks_relay import LocalSocksRelay
from app.services.settings_store import (
    CHECKING_PRESETS,
    ProtectionConfigError,
    get_protection,
    set_protection,
)


class ProtectionSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def test_defaults(self):
        with SessionLocal() as db:
            db.query(GlobalSetting).filter(GlobalSetting.key.like("prot_%")).delete(synchronize_session=False)
            db.commit()
            cfg = get_protection(db)
        self.assertEqual(cfg["reply_mode"], "sticky")
        self.assertTrue(cfg["flood_stop_enabled"])
        self.assertTrue(cfg["reply_age_enabled"])
        self.assertEqual(cfg["reply_age_minutes"], 30)

    def test_roundtrip_and_age_disable(self):
        with SessionLocal() as db:
            set_protection(db, {"checking_preset": "custom", "poll_interval_seconds": "120",
                                "poll_jitter_pct": "55", "flood_multiplier": "3,5",
                                "reply_mode": "fixed", "flood_stop_enabled": "on"})
            cfg = get_protection(db)
        self.assertEqual(cfg["poll_interval_seconds"], 120)
        self.assertEqual(cfg["poll_jitter_pct"], 55)
        self.assertEqual(cfg["flood_multiplier"], 3.5)
        self.assertEqual(cfg["reply_mode"], "fixed")
        self.assertFalse(cfg["reply_age_enabled"])  # флажок не передан => порог выключен

    def test_preset_overrides_numbers(self):
        with SessionLocal() as db:
            set_protection(db, {"checking_preset": "realistic", "poll_interval_seconds": "999",
                                "reply_age_enabled": "on"})
            cfg = get_protection(db)
        p, j, c = CHECKING_PRESETS["realistic"]
        self.assertEqual((cfg["poll_interval_seconds"], cfg["poll_jitter_pct"], cfg["catchup_interval_seconds"]), (p, j, c))
        self.assertTrue(cfg["reply_age_enabled"])

    def test_validation_rejects_and_writes_nothing(self):
        with SessionLocal() as db:
            set_protection(db, {"checking_preset": "custom", "poll_interval_seconds": "77", "reply_age_enabled": "on"})
            for bad in ({"poll_interval_seconds": "5"}, {"poll_interval_seconds": "abc"},
                        {"fast_delay_min": "50", "fast_delay_max": "10"}, {"reply_mode": "weird"},
                        {"flood_multiplier": "0.5"}):
                with self.assertRaises(ProtectionConfigError):
                    set_protection(db, {"checking_preset": "custom", **bad})
            self.assertEqual(get_protection(db)["poll_interval_seconds"], 77)


class CredentialParseTests(unittest.TestCase):
    def test_text_and_html(self):
        text = "App configuration\nApp api_id:\n1234567\nApp api_hash:\n0123456789abcdef0123456789abcdef\n"
        self.assertEqual(parse_credentials(text), (1234567, "0123456789abcdef0123456789abcdef"))
        html = ('<label>App api_id:</label><div><span class="x"> 7654321 </span></div>'
                '<label>App api_hash:</label><div><span>FEDCBA9876543210FEDCBA9876543210</span></div>')
        self.assertEqual(parse_credentials("", html), (7654321, "fedcba9876543210fedcba9876543210"))

    def test_absent(self):
        self.assertIsNone(parse_credentials("Create new application", "<form></form>"))


def _echo_server():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)

    def run():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            data = c.recv(1024)
            c.sendall(b"echo:" + data)
            c.close()

    threading.Thread(target=run, daemon=True).start()
    return srv


class RelayTests(unittest.TestCase):
    def test_chained_relays_and_domain_names(self):
        srv = _echo_server()
        port = srv.getsockname()[1]
        exit_relay = LocalSocksRelay(None)  # «прокси-провайдер»: выходит напрямую
        front = LocalSocksRelay((socks.SOCKS5, "127.0.0.1", exit_relay.port, True, None, None))  # то, что видит Chromium
        try:
            for host in ("127.0.0.1", "localhost"):  # localhost — доменное имя (ATYP 3), резолвит «прокси»
                s = socks.socksocket()
                s.set_proxy(socks.SOCKS5, "127.0.0.1", front.port, rdns=True)
                s.settimeout(5)
                s.connect((host, port))
                s.sendall(b"ping")
                self.assertEqual(s.recv(100), b"echo:ping")
                s.close()
        finally:
            front.close()
            exit_relay.close()
            srv.close()

    def test_unreachable_target_reports_error(self):
        relay = LocalSocksRelay(None, timeout=2)
        try:
            s = socks.socksocket()
            s.set_proxy(socks.SOCKS5, "127.0.0.1", relay.port)
            s.settimeout(5)
            with self.assertRaises(socks.ProxyError):
                s.connect(("127.0.0.1", 1))  # порт закрыт
        finally:
            relay.close()


if __name__ == "__main__":
    unittest.main()
