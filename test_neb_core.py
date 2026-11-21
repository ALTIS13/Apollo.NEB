from concurrent.futures import ThreadPoolExecutor
import sqlite3
import tempfile
import unittest
from pathlib import Path

import neb_core as core

OWNER = "neb1pqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq3hy0ga"
SUPPORT = "neb1zqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqw44dre"
FIXTURE_PASSWORD = "test fixture password"
RFC_TEST_KEY = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


class CoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)

    def test_payout_addresses_are_validated_without_private_keys(self):
        self.assertEqual(core.wallet(OWNER), OWNER)
        for bad in (OWNER[:-1] + "q", OWNER.replace("neb1", "test1"), "secret", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                core.wallet(bad)

    def test_pool_endpoint_contains_no_credentials_path_or_shell_fragment(self):
        self.assertEqual(core.pool_url("stratum+ssl://pool.example:7806"), "stratum+ssl://pool.example:7806")
        for unsafe in ("stratum+ssl://alice@pool.example:7806", "stratum+tcp://pool.example:1/a",
                       "stratum+ssl://pool.example:7806?x=1", "file:///tmp/miner", "stratum+tcp://a b:1"):
            with self.assertRaises(ValueError):
                core.pool_url(unsafe)

    def test_config_never_enables_unapproved_or_unbounded_support(self):
        self.assertFalse(core.support_config(None)["enabled"])
        for invalid in ({"wallet": SUPPORT, "basis_points": 50},
                        {"enabled": True, "wallet": "", "basis_points": 50},
                        {"enabled": True, "wallet": SUPPORT, "basis_points": 101}):
            with self.assertRaises(ValueError):
                core.support_config(invalid)

    def test_support_time_is_budgeted_from_normal_runtime_and_charged_before_switch(self):
        cfg = core.support_config({"enabled": True, "wallet": SUPPORT, "basis_points": 100})
        path = self.base / "credit.json"
        time = core.SupportClock(cfg, path, window=3)
        self.assertIsNone(time.observe("gpu", True, 1))
        for second in range(2, 298):
            self.assertIsNone(time.observe("gpu", True, second))
        self.assertEqual(time.observe("gpu", True, 298), "support")
        time.fund("gpu", 298)
        self.assertIsNone(time.observe("gpu", True, 300))
        self.assertEqual(time.observe("gpu", True, 301), "operator")
        time.finish("gpu")
        again = core.SupportClock(cfg, path, window=3)
        self.assertIsNone(again.observe("gpu", True, 302))
        self.assertIsNone(again.observe("gpu", True, 303))

    def test_service_suspend_does_not_fund_support(self):
        cfg = core.support_config({"enabled": True, "wallet": SUPPORT, "basis_points": 100})
        time = core.SupportClock(cfg, self.base / "state.json", window=1)
        time.observe("cpu", True, 10)
        self.assertIsNone(time.observe("cpu", True, 1000))
        self.assertFalse(time.active())

    def test_auth_password_totp_and_server_side_revoke(self):
        key = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
        auth = core.Auth(core.new_login("test fixture password", key), self.base / "auth.sqlite")
        at = 1234567890
        code = core.totp(key, at)
        with self.assertRaises(ValueError):
            auth.enter("test fixture password", "invalid", now=at)
        token = auth.enter("test fixture password", code, now=at)
        self.assertTrue(auth.valid(token, now=at + 1))
        with self.assertRaises(ValueError):
            auth.enter("test fixture password", code, now=at + 1)
        another = core.Auth(auth.config, self.base / "auth.sqlite")
        self.assertTrue(another.valid(token, now=at + 1))
        another.revoke(token)
        self.assertFalse(auth.valid(token, now=at + 1))

    def test_totp_matches_rfc6238_sha1_vector_at_59_seconds(self):
        self.assertEqual(core.totp(RFC_TEST_KEY, 59), "287082")

    def test_failed_attempts_are_shared_and_survive_new_auth_instance(self):
        path, at = self.base / "auth.sqlite", 1700000100
        config = core.new_login(FIXTURE_PASSWORD)
        for _ in range(5):
            with self.assertRaises(ValueError):
                core.Auth(config, path).enter("not the fixture password", None, now=at)
        another = core.Auth(config, path)
        with self.assertRaises(ValueError):
            another.enter(FIXTURE_PASSWORD, None, now=at + 1)
        issued = another.enter(FIXTURE_PASSWORD, None, now=at + 61)
        self.assertTrue(another.valid(issued, now=at + 61))

    def test_enrollment_is_session_bound_and_rotates_all_sessions_atomically(self):
        path, at = self.base / "auth.sqlite", 1700000100
        config = core.new_login(FIXTURE_PASSWORD)
        auth = core.Auth(config, path)
        first = auth.enter(FIXTURE_PASSWORD, None, now=at)
        other = auth.enter(FIXTURE_PASSWORD, None, now=at)
        self.assertFalse(auth.is_totp_enabled())
        pending = auth.begin_enrollment(first, FIXTURE_PASSWORD, now=at)
        self.assertEqual(set(pending), {"secret", "uri", "expires_in"})
        self.assertTrue(pending["uri"].startswith("otpauth://totp/"))
        self.assertIn("secret=" + pending["secret"], pending["uri"])
        code = core.totp(pending["secret"], at)
        with self.assertRaises(ValueError):
            auth.confirm_enrollment(other, code, now=at)
        self.assertFalse(auth.is_totp_enabled())
        fresh, recovery = auth.confirm_enrollment(first, code, now=at)
        another = core.Auth(config, path)
        self.assertTrue(another.is_totp_enabled())
        self.assertTrue(another.valid(fresh, now=at))
        self.assertFalse(another.valid(first, now=at))
        self.assertFalse(another.valid(other, now=at))
        with self.assertRaises(ValueError):
            another.enter(FIXTURE_PASSWORD, code, now=at + 1)
        self.assertEqual(len(recovery), 8)
        self.assertEqual(len(set(recovery)), 8)
        self.assertNotIn(recovery[0].encode("ascii"), path.read_bytes())

    def test_enrollment_expiry_and_attempt_limit_invalidate_pending_secret(self):
        auth = core.Auth(core.new_login(FIXTURE_PASSWORD), self.base / "auth.sqlite")
        at = 1700000100
        first = auth.enter(FIXTURE_PASSWORD, None, now=at)
        pending = auth.begin_enrollment(first, FIXTURE_PASSWORD, now=at)
        later = at + pending["expires_in"]
        with self.assertRaises(ValueError):
            auth.confirm_enrollment(first, core.totp(pending["secret"], later), now=later)
        pending = auth.begin_enrollment(first, FIXTURE_PASSWORD, now=later)
        for step in range(5):
            with self.assertRaises(ValueError):
                auth.confirm_enrollment(first, "bad-code", now=later + step)
        with self.assertRaises(ValueError):
            auth.confirm_enrollment(first, core.totp(pending["secret"], later + 61), now=later + 61)
        self.assertFalse(auth.is_totp_enabled())

    def test_recovery_code_is_only_spent_once_under_concurrent_login(self):
        path, at = self.base / "auth.sqlite", 1700000100
        cfg = core.new_login(FIXTURE_PASSWORD)
        auth = core.Auth(cfg, path)
        token = auth.enter(FIXTURE_PASSWORD, None, now=at)
        pending = auth.begin_enrollment(token, FIXTURE_PASSWORD, now=at)
        _, recovery = auth.confirm_enrollment(token, core.totp(pending["secret"], at), now=at)

        def consume(instance):
            try:
                return instance.enter(FIXTURE_PASSWORD, recovery[0], now=at + 1)
            except ValueError:
                return None

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(consume, (auth, core.Auth(cfg, path))))
        self.assertEqual(sum(token is not None for token in results), 1)
        for token in filter(None, results):
            self.assertTrue(auth.valid(token, now=at + 1))

    def test_legacy_migration_keeps_tick_and_disabled_factor_does_not_return(self):
        path, at = self.base / "auth.sqlite", 1700000100
        cfg = core.new_login(FIXTURE_PASSWORD, RFC_TEST_KEY)
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE auth (id INTEGER PRIMARY KEY CHECK (id=1), last_tick INTEGER NOT NULL)")
            db.execute("INSERT INTO auth VALUES (1, ?)", (at // 30,))
        db.close()
        auth = core.Auth(cfg, path)
        self.assertTrue(auth.is_totp_enabled())
        with self.assertRaises(ValueError):
            auth.enter(FIXTURE_PASSWORD, core.totp(RFC_TEST_KEY, at), now=at)
        token = auth.enter(FIXTURE_PASSWORD, core.totp(RFC_TEST_KEY, at + 30), now=at + 30)
        auth.reset_after_password_change()
        self.assertFalse(auth.valid(token, now=at + 30))
        self.assertTrue(auth.is_totp_enabled())
        with self.assertRaises(ValueError):
            auth.reset_after_password_change(RFC_TEST_KEY)
        with self.assertRaises(ValueError):
            auth.enter(FIXTURE_PASSWORD, core.totp(RFC_TEST_KEY, at + 30), now=at + 30)
        token = auth.enter(FIXTURE_PASSWORD, core.totp(RFC_TEST_KEY, at + 60), now=at + 60)
        fresh = auth.disable_enrollment(token, FIXTURE_PASSWORD, core.totp(RFC_TEST_KEY, at + 90), now=at + 90)
        another = core.Auth(cfg, path)
        self.assertFalse(another.is_totp_enabled())
        self.assertFalse(another.valid(token, now=at + 90))
        self.assertTrue(another.valid(fresh, now=at + 90))
