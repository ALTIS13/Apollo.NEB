import tempfile
import unittest
from pathlib import Path

import neb_core as core

OWNER = "neb1pqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq3hy0ga"
SUPPORT = "neb1zqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqw44dre"


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
