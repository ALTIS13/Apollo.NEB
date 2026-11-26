import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, call, patch

from neb import node_loop
from neb_core import read, store
import neb_node_update as update


NOW = 1_800_000_000
JOB = {"id": "2" * 32, "current": "0.9.1-rc.1", "tag": "v0.9.1", "sha256": "a" * 64,
       "channel": "stable", "expires": NOW + 70}


class UpdateAgentTests(unittest.TestCase):
    def setUp(self):
        sandbox = tempfile.TemporaryDirectory()
        self.addCleanup(sandbox.cleanup)
        self.root = Path(sandbox.name)
        self.journal = self.root / "ota.json"
        self.service = Mock()
        self.service.reserve_upgrade.return_value = True
        self.client = Mock()

    def test_offer_is_reserved_and_acknowledged_before_helper_and_not_launched_again(self):
        self.client.upgrade_exchange.side_effect = [{"job": JOB}, {"job": None}]
        agent = update.Updates(self.journal, self.root / "neb.py", self.service, self.client)
        def start(argv, **_):
            self.assertEqual(read(self.journal)["status"], "scheduled")
            self.assertEqual(self.client.upgrade_exchange.call_args_list,
                             [call(), call({"id": JOB["id"], "status": "preparing"})])
            self.assertIn("--user", argv)
            self.assertEqual(argv[-2:], ["node-update-helper", JOB["id"]])
        with patch.object(update.time, "time", return_value=NOW), \
             patch.object(update.subprocess, "run", side_effect=start) as launch:
            agent.cycle()
            agent.cycle()
        launch.assert_called_once()
        self.service.reserve_upgrade.assert_called_once()

    def test_terminal_receipt_is_retried_and_only_cleared_after_acceptance(self):
        store(self.journal, {"job": JOB, "status": "installed", "changed": NOW})
        self.client.upgrade_exchange.side_effect = [OSError("offline"), {"job": None}]
        agent = update.Updates(self.journal, self.root / "neb.py", self.service, self.client)
        with patch.object(update.time, "time", return_value=NOW):
            with self.assertRaises(OSError):
                agent.cycle()
            self.assertEqual(read(self.journal)["status"], "installed")
            agent.cycle()
        self.assertFalse(self.journal.exists())
        self.service.finish_upgrade.assert_called()

    def test_expired_terminal_result_clears_without_blocking_the_next_offer(self):
        store(self.journal, {"job": JOB, "status": "rolled_back", "changed": NOW})
        self.client.upgrade_exchange.return_value = {"job": None}
        agent = update.Updates(self.journal, self.root / "neb.py", self.service, self.client)
        with patch.object(update.time, "time", return_value=JOB["expires"]):
            agent.cycle()
            self.assertFalse(self.journal.exists())
            self.client.upgrade_exchange.assert_not_called()
            agent.cycle()
        self.client.upgrade_exchange.assert_called_once_with()

    def test_timeout_does_not_launch_a_second_helper(self):
        self.client.upgrade_exchange.side_effect = [{"job": JOB}, {"job": None}]
        agent = update.Updates(self.journal, self.root / "neb.py", self.service, self.client)
        with patch.object(update.time, "time", return_value=NOW), \
             patch.object(update.subprocess, "run", side_effect=subprocess.TimeoutExpired("systemd-run", 8)) as launch:
            agent.cycle()
            agent.cycle()
        launch.assert_called_once()
        self.assertEqual(read(self.journal)["status"], "scheduled")

    def test_interrupted_update_unlocks_verified_runtime_even_offline(self):
        entry = self.root / ".local/lib/apollo-neb/current/neb.py"
        entry.parent.mkdir(parents=True)
        entry.touch()
        store(self.journal, {"job": JOB, "status": "applying", "changed": NOW - 20})
        self.service.runtime_status.return_value = {"version": JOB["current"], "idle": True}
        self.service.status.return_value = {"workers": []}
        self.service.stopping.is_set.side_effect = [False, True]
        self.client.cycle.side_effect = OSError("disconnected")
        agent = update.Updates(self.journal, entry, self.service, self.client)
        probe = subprocess.CompletedProcess([], 1, stdout="LoadState=not-found\nActiveState=inactive\n")
        with patch.object(Path, "home", return_value=self.root), \
             patch.object(update.time, "time", return_value=NOW), \
             patch.object(update.subprocess, "run", return_value=probe) as systemctl, \
             self.assertLogs(level="WARNING"):
            node_loop(self.service, self.client, updates=agent)
        self.assertEqual(read(self.journal)["status"], "unknown")
        self.service.finish_upgrade.assert_called_once()
        self.client.upgrade_exchange.assert_not_called()
        self.client.cycle.assert_called_once()
        systemctl.assert_called_once()
        self.assertEqual(systemctl.call_args.args[0][:4], ["systemctl", "--user", "show", update._unit(JOB)])

        with patch.object(update.time, "time", return_value=NOW):
            agent.cycle()
        self.client.upgrade_exchange.assert_called_once_with({"id": JOB["id"], "status": "unknown"})
        self.assertFalse(self.journal.exists())

    def test_live_helper_and_unexpected_running_release_keep_reservation(self):
        entry = self.root / ".local/lib/apollo-neb/current/neb.py"
        entry.parent.mkdir(parents=True)
        entry.touch()
        store(self.journal, {"job": JOB, "status": "unconfirmed", "changed": NOW - 20})
        agent = update.Updates(self.journal, entry, self.service, self.client)
        for active, version in (("active", JOB["current"]), ("failed", "0.9.0")):
            with self.subTest(active=active), patch.object(Path, "home", return_value=self.root), \
                 patch.object(update.time, "time", return_value=NOW), \
                 patch.object(update.subprocess, "run", return_value=subprocess.CompletedProcess(
                     [], 0, stdout=f"LoadState=loaded\nActiveState={active}\n")):
                self.service.runtime_status.return_value = {"version": version, "idle": True}
                agent.cycle()
                self.assertEqual(read(self.journal)["status"], "unconfirmed")
        self.service.finish_upgrade.assert_not_called()
        self.client.upgrade_exchange.assert_not_called()

    def test_helper_requires_acknowledgment_before_applying(self):
        store(self.journal, {"job": JOB, "status": "scheduled", "changed": NOW})
        self.client.upgrade_exchange.side_effect = ValueError("revoked")
        effects = []

        def apply(job, *, progress, **_):
            self.assertEqual(job, JOB)
            progress("applying")
            effects.append("stop")
            return "installed"

        with self.assertRaises(ValueError):
            update._apply_saved(self.journal, JOB["id"], config=self.root, state=self.root, prefix=self.root,
                                port=18763, client=self.client, apply=apply)
        self.assertEqual(effects, [])
        self.assertEqual(read(self.journal)["status"], "failed")
