from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import secrets
import sqlite3
import ssl
import tempfile
import threading
import unittest
from unittest.mock import patch

from neb_fleet import Client, Fleet, FleetAuthError


LINUX_RELEASE = {"version": "0.9.1-rc.1", "platform": "linux", "arch": "x64", "kernel": "6.6"}
WINDOWS_RELEASE = {"version": "0.9.2", "platform": "windows", "arch": "x64", "kernel": None}
POWER_GPU = {"id": "00000000:01:00.0", "label": "NVIDIA test device", "vendor": "nvidia", "edge": 45,
             "watts": 20, "utilization": 1, "memory_used_mib": 256, "memory_total_mib": 4096,
             "power_limit": {"min": 175, "max": 300, "current": 250}}
UPGRADABLE = {**LINUX_RELEASE, "upgrade": True}
UPGRADE_PICK = {"current": LINUX_RELEASE["version"], "tag": "v0.9.2", "sha256": "a" * 64, "channel": "stable"}
STOPPED = [{"id": "card", "state": "stopped"}]


class FleetTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "fleet.sqlite"
        self.fleet = Fleet(self.path)
        self.at = 1700000100
        self.key = secrets.token_urlsafe(32)

    def node(self, label="Rig", key=None):
        key = key or self.key
        issued = self.fleet.enroll(label, now=self.at)
        self.fleet.pair(issued["id"], issued["code"], key, now=self.at)
        return issued

    def test_pair_claim_is_atomic_idempotent_and_expires(self):
        enrollment = self.fleet.enroll("One", now=self.at)
        ident = enrollment["id"]
        other = secrets.token_urlsafe(32)
        endpoints = (self.fleet, Fleet(self.path))

        def claim(pair):
            instance, key = pair
            try:
                instance.pair(ident, enrollment["code"], key, now=self.at)
                return key
            except ValueError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            winners = [key for key in pool.map(claim, zip(endpoints, (self.key, other))) if key]
        self.assertEqual(len(winners), 1)
        self.assertEqual(self.fleet.pair(ident, enrollment["code"], winners[0], now=self.at), {"ok": True})
        later = self.fleet.enroll("Two", now=self.at)
        with self.assertRaises(ValueError):
            self.fleet.pair(later["id"], later["code"], winners[0], now=self.at)
        with self.assertRaises(ValueError):
            self.fleet.pair(later["id"], later["code"], secrets.token_urlsafe(32), now=later["expires"])
        self.assertNotIn(winners[0].encode(), self.path.read_bytes())
        self.assertNotIn(enrollment["code"].encode(), self.path.read_bytes())

    def test_separate_windows_hardware_keeps_single_gpu_and_expires(self):
        node = self.node()["id"]
        device = {"id": "00000000:01:00.0", "label": "RTX 5070", "vendor": "nvidia", "edge": 0,
                  "watts": 26.4, "utilization": 8, "memory_used_mib": 1988, "memory_total_mib": 12227}
        hardware = {"devices": [device]}
        self.fleet.poll(node, self.key, [], [], now=self.at, release=WINDOWS_RELEASE, hardware=hardware)
        report = self.fleet.list(now=self.at)["nodes"][0]
        self.assertEqual(report["hardware"], hardware)
        self.assertEqual(report["workers"], [])
        with self.assertRaises(ValueError):
            self.fleet.poll(node, self.key, STOPPED, [], now=self.at + 1, release=WINDOWS_RELEASE, hardware=hardware)
        with self.assertRaises(ValueError):
            self.fleet.poll(node, self.key, [], [], now=self.at + 1, release=LINUX_RELEASE, hardware=hardware)
        duplicate = {**device, "id": "0000:01:00.0"}
        with self.assertRaises(ValueError):
            self.fleet.poll(node, self.key, [], [], now=self.at + 1, release=WINDOWS_RELEASE,
                            hardware={"devices": [device, duplicate]})
        self.assertNotIn("hardware", self.fleet.list(now=self.at + 30)["nodes"][0])
        self.fleet.revoke(node, now=self.at + 2)
        self.assertEqual(self.fleet.observations(now=self.at + 2)["nodes"], [])

    def test_power_requires_available_opted_gpu_single_claim_and_matching_readback(self):
        node = self.node()["id"]
        hardware = {"devices": [POWER_GPU]}
        self.fleet.poll(node, self.key, [], [], now=self.at, release=WINDOWS_RELEASE, hardware=hardware)
        with self.assertRaises(ValueError):
            self.fleet.queue_power(node, POWER_GPU["id"], 225, now=self.at)
        self.fleet.poll(node, self.key, [], [], now=self.at, release=WINDOWS_RELEASE,
                       hardware={**hardware, "power_control": True})
        for value in (True, 301, 174, 200.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.fleet.queue_power(node, POWER_GPU["id"], value, now=self.at)
        queued = self.fleet.queue_power(node, POWER_GPU["id"], 225, now=self.at)
        claim = {"id": queued["id"], "phase": "claim"}
        with self.assertRaises(ValueError):
            self.fleet.power_poll(node, self.key, claim, now=self.at)
        job = self.fleet.power_poll(node, self.key, now=self.at)["job"]
        self.assertEqual(job["gpu"], POWER_GPU["id"])
        self.assertEqual(job["watts"], 225)
        self.assertEqual(self.fleet.power_poll(node, self.key, now=self.at + 1), {"job": None})
        with self.assertRaises(FleetAuthError):
            self.fleet.power_poll(node, "x" * 43, claim, now=self.at + 1)
        self.assertEqual(self.fleet.power_poll(node, self.key, claim, now=self.at + 1),
                         {"ok": True, "lease_expires": self.at + 16})
        with self.assertRaises(ValueError):
            self.fleet.power_poll(node, self.key, claim, now=self.at + 2)
        result = {"id": job["id"], "phase": "applied", "observed_watts": 250}
        with self.assertRaises(ValueError):
            self.fleet.power_poll(node, self.key, result, now=self.at + 2)
        result["observed_watts"] = 225
        for at in (self.at + 2, self.at + 3):
            self.assertEqual(self.fleet.power_poll(node, self.key, result, now=at), {"ok": True})
        row = self.fleet.list(now=self.at + 3)["nodes"][0]["power_job"]
        self.assertEqual((row["status"], row["observed_watts"]), ("applied", 225))

    def test_power_rechecks_permission_before_claim_and_reports_uncertain_expiry(self):
        node = self.node()["id"]
        enabled = {"devices": [POWER_GPU], "power_control": True}
        self.fleet.poll(node, self.key, [], [], now=self.at, release=WINDOWS_RELEASE, hardware=enabled)
        queued = self.fleet.queue_power(node, POWER_GPU["id"], 225, now=self.at)
        self.fleet.power_poll(node, self.key, now=self.at)
        claim = {"id": queued["id"], "phase": "claim"}
        self.fleet.poll(node, self.key, [], [], now=self.at + 1, release=WINDOWS_RELEASE, hardware={"devices": [POWER_GPU]})
        with self.assertRaises(ValueError):
            self.fleet.power_poll(node, self.key, claim, now=self.at + 1)
        self.fleet.poll(node, self.key, [], [], now=self.at + 2, release=WINDOWS_RELEASE, hardware=enabled)
        with self.assertRaises(ValueError):
            self.fleet.power_poll(node, self.key, claim, now=self.at + 2)
        self.assertEqual(self.fleet.list(now=self.at + 2)["nodes"][0]["power_job"]["status"], "cancelled")
        replacement = self.fleet.queue_power(node, POWER_GPU["id"], 225, now=self.at + 3)
        self.fleet.power_poll(node, self.key, now=self.at + 4)
        claim["id"] = replacement["id"]
        lease = self.fleet.power_poll(node, self.key, claim, now=self.at + 30)["lease_expires"]
        self.assertEqual(lease, self.at + 45)
        self.fleet.poll(node, self.key, [], [], now=self.at + 34, release=WINDOWS_RELEASE, hardware=enabled)
        with self.assertRaises(ValueError):
            self.fleet.queue_power(node, POWER_GPU["id"], 220, now=self.at + 34)
        self.assertEqual(self.fleet.list(now=lease)["nodes"][0]["power_job"]["status"], "unknown")
        with self.assertRaises(ValueError):
            self.fleet.queue_power(node, POWER_GPU["id"], 220, now=lease)
        self.fleet.poll(node, self.key, [], [], now=lease + 1, release=WINDOWS_RELEASE, hardware=enabled)
        self.assertEqual(self.fleet.queue_power(node, POWER_GPU["id"], 220, now=lease + 1)["status"], "queued")

    def test_status_is_sanitized_and_revoke_closes_existing_identity(self):
        ident = self.node()["id"]
        private = {"id": "card", "state": "running", "edge": 60, "hot": float("inf"),
                   "command": "discard", "message": "discard", "pool": "discard", "env": {"discard": "discard"}}
        self.assertEqual(self.fleet.poll(ident, self.key, [private], [], now=self.at), {"commands": []})
        public = self.fleet.list(now=self.at)["nodes"][0]
        self.assertEqual(public["status"], "online")
        self.assertEqual(public["workers"], [{"id": "card", "state": "running", "edge": 60}])
        self.assertNotIn("discard", json.dumps(public))
        self.fleet.revoke(ident, now=self.at)
        self.assertEqual(self.fleet.list(now=self.at)["nodes"][0]["status"], "revoked")
        with self.assertRaises(ValueError):
            self.fleet.poll(ident, self.key, [private], [], now=self.at + 1)

    def test_command_dispatches_once_refuses_unlisted_workers_and_expires_unknown(self):
        ident = self.node()["id"]
        workers = [{"id": "card", "state": "stopped"}]
        with self.assertRaises(ValueError):
            self.fleet.command(ident, "start", ["card"], now=self.at)
        self.fleet.poll(ident, self.key, workers, [], now=self.at)
        with self.assertRaises(ValueError):
            self.fleet.command(ident, "exec", ["card"], now=self.at)
        with self.assertRaises(ValueError):
            self.fleet.command(ident, "start", ["other"], now=self.at)
        command = self.fleet.command(ident, "start", ["card"], now=self.at)
        self.assertEqual(command["status"], "queued")
        delivery = self.fleet.poll(ident, self.key, workers, [], now=self.at + 1)["commands"][0]
        self.assertEqual(delivery["id"], command["id"])
        reopened = Fleet(self.path)
        self.assertEqual(reopened.poll(ident, self.key, workers, [], now=self.at + 2)["commands"], [])
        expired = reopened.list(now=delivery["expires"])["nodes"][0]["commands"][0]
        self.assertEqual(expired["status"], "unknown")

    def test_only_own_sent_command_can_be_acknowledged_and_next_report_revalidates_workers(self):
        ident = self.node()["id"]
        key2 = secrets.token_urlsafe(32)
        other = self.node("Other", key2)["id"]
        workers = [{"id": "card", "state": "running"}]
        self.fleet.poll(ident, self.key, workers, [], now=self.at)
        queued = self.fleet.command(ident, "stop", ["card"], now=self.at)["id"]
        self.assertEqual(self.fleet.poll(ident, self.key, [], [], now=self.at + 1)["commands"], [])
        self.fleet.poll(ident, self.key, workers, [], now=self.at + 2)
        sent = self.fleet.command(ident, "stop", ["card"], now=self.at + 2)["id"]
        self.fleet.poll(ident, self.key, workers, [], now=self.at + 3)
        ack = [{"id": sent, "status": "acknowledged"}]
        self.fleet.poll(other, key2, workers, ack, now=self.at + 4)
        commands = {c["id"]: c["status"] for n in self.fleet.list(now=self.at + 4)["nodes"] for c in n["commands"]}
        self.assertEqual(commands, {queued: "unknown", sent: "sent"})
        self.fleet.poll(ident, self.key, workers, ack, now=self.at + 4)
        self.fleet.poll(ident, self.key, workers, ack, now=self.at + 5)
        commands = {c["id"]: c["status"] for n in self.fleet.list(now=self.at + 5)["nodes"] for c in n["commands"]}
        self.assertEqual(commands, {queued: "unknown", sent: "acknowledged"})

    def test_concurrent_poll_cannot_deliver_one_command_twice(self):
        ident = self.node()["id"]
        workers = [{"id": "card", "state": "stopped"}]
        self.fleet.poll(ident, self.key, workers, [], now=self.at)
        queued = self.fleet.command(ident, "start", ["card"], now=self.at)["id"]
        other = Fleet(self.path)

        def poll(instance):
            return instance.poll(ident, self.key, workers, [], now=self.at + 1)["commands"]

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual([item["id"] for result in pool.map(poll, (self.fleet, other)) for item in result], [queued])

    def test_restart_requires_current_node_capability_and_running_workers(self):
        ident = self.node()["id"]
        rows = [{"id": "card", "state": "running", "device": "0000:01:00.0", "adapter": "custom"}]
        details = {"system": {"platform": "Linux", "kernel": "6.8.1", "arch": "x86_64", "cpu_count": 8,
                              "memory_bytes": 2**33, "boot_time": self.at - 180},
                   "events": [{"time": self.at - 30, "worker": "card", "type": "start", "status": "running"}]}
        self.fleet.poll(ident, self.key, rows, [], now=self.at)
        with self.assertRaises(ValueError):
            self.fleet.command(ident, "restart", ["card"], now=self.at)
        self.fleet.poll(ident, self.key, rows, [], details=details, now=self.at + 1)
        node = self.fleet.list(now=self.at + 1)["nodes"][0]
        self.assertEqual(node["details"], details)
        self.assertEqual(node["workers"], rows)
        first = self.fleet.command(ident, "restart", ["card"], now=self.at + 1)
        self.assertEqual(self.fleet.poll(ident, self.key, [{**rows[0], "state": "protection"}], [],
                                         details=details, now=self.at + 2)["commands"], [])
        with self.assertRaises(ValueError):
            self.fleet.command(ident, "restart", ["card"], now=self.at + 2)
        self.assertEqual(self.fleet.list(now=self.at + 2)["nodes"][0]["commands"][0]["status"], "unknown")
        self.fleet.poll(ident, self.key, rows, [], details=details, now=self.at + 3)
        second = self.fleet.command(ident, "restart", ["card"], now=self.at + 3)
        delivery = self.fleet.poll(ident, self.key, rows, [], details=details, now=self.at + 4)["commands"]
        self.assertEqual([cmd["id"] for cmd in delivery], [second["id"]])
        self.assertNotEqual(first, second)
        self.fleet.revoke(ident, now=self.at + 5)
        self.assertNotIn("details", self.fleet.list(now=self.at + 5)["nodes"][0])

    def test_details_reject_arbitrary_payload_and_absence_clears_capability(self):
        ident = self.node()["id"]
        rows = [{"id": "card", "state": "running"}]
        details = {"system": {"platform": "Linux", "arch": "x86_64", "kernel": "6.8", "cpu_count": 1,
                              "memory_bytes": None, "boot_time": None}, "events": []}
        with self.assertRaises(ValueError):
            self.fleet.poll(ident, self.key, rows, [], details={**details, "environment": "secret"}, now=self.at)
        self.fleet.poll(ident, self.key, rows, [], details=details, now=self.at)
        self.fleet.command(ident, "restart", ["card"], now=self.at)
        self.assertEqual(self.fleet.poll(ident, self.key, rows, [], now=self.at + 1)["commands"], [])
        self.assertNotIn("details", self.fleet.list(now=self.at + 2)["nodes"][0])

    def test_link_check_does_not_extend_presence_or_take_queued_commands(self):
        ident = self.node()["id"]
        workers = [{"id": "card", "state": "stopped"}]
        self.assertEqual(self.fleet.check(ident, self.key), {"ok": True, "last_seen": None})
        self.fleet.poll(ident, self.key, workers, [], now=self.at)
        queued = self.fleet.command(ident, "start", ["card"], now=self.at)
        self.assertEqual(self.fleet.check(ident, self.key), {"ok": True, "last_seen": self.at})
        row = self.fleet.list(now=self.at + 2)["nodes"][0]
        self.assertEqual(row["last_seen"], self.at)
        self.assertEqual(row["commands"][0]["status"], "queued")
        delivery = self.fleet.poll(ident, self.key, workers, [], now=self.at + 3)["commands"]
        self.assertEqual([command["id"] for command in delivery], [queued["id"]])
        self.fleet.revoke(ident, now=self.at + 4)
        with self.assertRaises(ValueError):
            self.fleet.check(ident, self.key)

    def test_release_replaces_previous_report_and_omission_clears_it(self):
        ident = self.node()["id"]
        self.assertIsNone(self.fleet.release_target(ident, now=self.at))
        workers = [{"id": "card", "state": "stopped"}]
        self.assertEqual(self.fleet.poll(ident, self.key, workers, [], now=self.at, release=LINUX_RELEASE),
                         {"commands": []})
        reloaded = Fleet(self.path)
        self.assertEqual(reloaded.list(now=self.at)["nodes"][0]["release"], LINUX_RELEASE)
        self.assertEqual(reloaded.release_target(ident, now=self.at), LINUX_RELEASE)
        self.assertEqual(set(reloaded.observations(now=self.at)["nodes"][0]),
                         {"id", "status", "last_seen", "workers"})

        newer = {**LINUX_RELEASE, "version": "0.9.2", "kernel": "6.12"}
        older = {**LINUX_RELEASE, "version": "0.9.0", "kernel": None}
        for now, release in ((self.at + 1, newer), (self.at + 2, older)):
            reloaded.poll(ident, self.key, workers, [], now=now, release=release)
            self.assertEqual(reloaded.release_target(ident, now=now), release)
        self.assertEqual(reloaded.poll(ident, self.key, workers, [], now=self.at + 3), {"commands": []})
        self.assertNotIn("release", reloaded.list(now=self.at + 3)["nodes"][0])
        self.assertIsNone(reloaded.release_target(ident, now=self.at + 3))

    def test_package_target_excludes_pending_stale_future_and_revoked_nodes(self):
        ident = self.node()["id"]
        pending = self.fleet.enroll("Not paired", now=self.at)["id"]
        self.assertIsNone(self.fleet.release_target(pending, now=self.at))
        self.assertIsNone(self.fleet.release_target("invalid", now=self.at))
        self.fleet.poll(ident, self.key, [], [], now=self.at, release=WINDOWS_RELEASE)
        self.assertEqual(self.fleet.release_target(ident, now=self.at + 29), WINDOWS_RELEASE)
        for when in (self.at - 1, self.at + 30):
            self.assertIsNone(self.fleet.release_target(ident, now=when))
            self.assertTrue(all("release" not in row for row in self.fleet.list(now=when)["nodes"]))

        self.fleet.revoke(ident, now=self.at + 2)
        self.assertIsNone(self.fleet.release_target(ident, now=self.at + 2))
        self.assertTrue(all("release" not in row for row in self.fleet.list(now=self.at + 2)["nodes"]))

    def test_invalid_release_does_not_update_the_heartbeat_or_previous_target(self):
        ident = self.node()["id"]
        self.fleet.poll(ident, self.key, [], [], now=self.at, release=LINUX_RELEASE)
        for invalid in (
            {}, [], {**LINUX_RELEASE, "asset": "untrusted"},
            {**LINUX_RELEASE, "version": "v0.9.1-rc.1"},
            {**LINUX_RELEASE, "version": "0.09.1"},
            {**LINUX_RELEASE, "version": "0.9.1+build"},
            {**LINUX_RELEASE, "platform": "darwin"},
            {**LINUX_RELEASE, "arch": "arm64"},
            {**LINUX_RELEASE, "kernel": "6.8"},
            {**WINDOWS_RELEASE, "kernel": "6.6"},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.fleet.poll(ident, self.key, [], [], now=self.at + 1, release=invalid)
        self.assertEqual(self.fleet.check(ident, self.key), {"ok": True, "last_seen": self.at})
        self.assertEqual(self.fleet.release_target(ident, now=self.at + 1), LINUX_RELEASE)

    def test_release_migration_keeps_existing_nodes_and_queued_commands(self):
        ident = self.node()["id"]
        workers = [{"id": "card", "state": "stopped"}]
        self.fleet.poll(ident, self.key, workers, [], now=self.at)
        queued = self.fleet.command(ident, "start", ["card"], now=self.at)
        # A registry created by the old code contains no release table.
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DROP TABLE IF EXISTS node_releases")

        migrated = Fleet(self.path)
        self.assertIsNone(migrated.release_target(ident, now=self.at))
        self.assertEqual(migrated.check(ident, self.key), {"ok": True, "last_seen": self.at})
        result = migrated.poll(ident, self.key, workers, [], now=self.at + 1, release=LINUX_RELEASE)
        self.assertEqual([item["id"] for item in result["commands"]], [queued["id"]])
        self.assertEqual(migrated.release_target(ident, now=self.at + 1), LINUX_RELEASE)

    def test_client_adds_release_only_when_provided_and_valid(self):
        ident = self.node()["id"]
        agent = Client("https://fleet.example.invalid", ident, self.key, self.path.with_name("agent.sqlite"))
        workers = [{"id": "card", "state": "stopped"}]
        with patch.object(agent, "_post", return_value={"commands": []}) as post:
            agent.cycle(workers, None, now=self.at)
            route, legacy = post.call_args.args
            self.assertEqual(route, "/api/node/poll")
            self.assertEqual(set(legacy), {"id", "key", "workers", "acks"})
            agent.cycle(workers, None, now=self.at + 1, release=LINUX_RELEASE)
            route, wire = post.call_args.args
            self.assertEqual(route, "/api/node/poll")
            self.assertEqual(wire, {**legacy, "release": LINUX_RELEASE})
            with self.assertRaises(ValueError):
                agent.cycle(workers, None, now=self.at + 2, release={**WINDOWS_RELEASE, "kernel": "6.12"})
            self.assertEqual(post.call_count, 2)

    def upgrade_node(self, *, release=UPGRADABLE):
        ident = self.node()["id"]
        self.fleet.poll(ident, self.key, STOPPED, [], now=self.at, release=release)
        return ident

    def queue_upgrade(self, ident, *, now=None, **changes):
        return self.fleet.queue_upgrade(ident, now=self.at if now is None else now, **{**UPGRADE_PICK, **changes})

    def send_upgrade_receipt(self, ident, job_id, status, now):
        return self.fleet.upgrade_poll(ident, self.key, {"id": job_id, "status": status}, now=now)

    def test_upgrade_capability_is_optional_and_linux_only(self):
        ident = self.node()["id"]
        for invalid in ({**WINDOWS_RELEASE, "upgrade": True}, {**LINUX_RELEASE, "upgrade": 1},
                        {**LINUX_RELEASE, "upgrade": "true"}):
            with self.assertRaises(ValueError):
                self.fleet.poll(ident, self.key, STOPPED, [], now=self.at, release=invalid)
        self.fleet.poll(ident, self.key, STOPPED, [], now=self.at, release=LINUX_RELEASE)
        self.assertEqual(self.fleet.release_target(ident, now=self.at), LINUX_RELEASE)
        self.fleet.poll(ident, self.key, STOPPED, [], now=self.at, release={**WINDOWS_RELEASE, "upgrade": False})
        self.assertEqual(self.fleet.release_target(ident, now=self.at), {**WINDOWS_RELEASE, "upgrade": False})
        with self.assertRaises(ValueError):
            self.queue_upgrade(ident, current=WINDOWS_RELEASE["version"], tag="v0.9.3")

    def test_upgrade_queue_refuses_stale_busy_unpaired_and_invalid_selection(self):
        unpaired = self.fleet.enroll("Waiting", now=self.at)["id"]
        with self.assertRaises(ValueError):
            self.queue_upgrade(unpaired)
        ident = self.upgrade_node(release=LINUX_RELEASE)
        with self.assertRaises(ValueError):
            self.queue_upgrade(ident)
        for state in ("running", "protection"):
            self.fleet.poll(ident, self.key, [{"id": "card", "state": state}], [], now=self.at, release=UPGRADABLE)
            with self.subTest(state=state), self.assertRaises(ValueError):
                self.queue_upgrade(ident)
        self.fleet.poll(ident, self.key, STOPPED, [], now=self.at, release=UPGRADABLE)
        for limits in (
            {"now": self.at + 30}, {"current": "0.9.0"}, {"tag": "v0.9.0"},
            {"tag": "v0.9.1-rc.1", "channel": "testing"}, {"tag": "0.9.2"},
            {"tag": "v0.9.2-rc.1"}, {"sha256": "x" * 64}, {"channel": "dangerous"},
        ):
            with self.subTest(limits=limits), self.assertRaises(ValueError):
                self.queue_upgrade(ident, **limits)
        self.fleet.command(ident, "start", ["card"], now=self.at)
        with self.assertRaises(ValueError):
            self.queue_upgrade(ident)
        self.assertTrue(all("upgrade" not in row for row in self.fleet.list(now=self.at)["nodes"]))

    def test_upgrade_queue_is_atomic_across_instances_and_excludes_start_restart(self):
        ident = self.upgrade_node()
        # Lift a state file with no OTA table, preserving the enrolled node and its snapshot.
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DROP TABLE IF EXISTS node_upgrades")
        self.fleet = Fleet(self.path)

        def enqueue(registry):
            try:
                return registry.queue_upgrade(ident, now=self.at, **UPGRADE_PICK)
            except ValueError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(enqueue, (self.fleet, Fleet(self.path))))
        queued, = (job for job in results if job)
        self.assertEqual(queued, {"id": queued["id"], "tag": UPGRADE_PICK["tag"], "status": "queued", "time": self.at})
        for action in ("start", "restart"):
            with self.assertRaisesRegex(ValueError, "обновлен"):
                self.fleet.command(ident, action, ["card"], now=self.at)
        self.assertEqual(self.fleet.observations(now=self.at)["nodes"][0]["workers"], STOPPED)
        self.assertNotIn("upgrade", self.fleet.observations(now=self.at)["nodes"][0])
        self.assertEqual(self.fleet.list(now=self.at)["nodes"][0]["upgrade"], queued)

    def test_upgrade_offer_and_applying_recheck_incumbent_and_commands(self):
        ident = self.upgrade_node()
        queued = self.queue_upgrade(ident)
        running = [{"id": "card", "state": "running"}]
        self.fleet.poll(ident, self.key, running, [], now=self.at + 1, release=UPGRADABLE)
        self.assertEqual(self.fleet.upgrade_poll(ident, self.key, now=self.at + 1), {"job": None})
        self.fleet.poll(ident, self.key, STOPPED, [], now=self.at + 2, release=UPGRADABLE)
        offered = self.fleet.upgrade_poll(ident, self.key, now=self.at + 2)["job"]
        self.assertEqual(offered["id"], queued["id"])
        self.send_upgrade_receipt(ident, queued["id"], "preparing", self.at + 2)
        other = {**UPGRADABLE, "version": "0.9.0"}
        self.fleet.poll(ident, self.key, STOPPED, [], now=self.at + 3, release=other)
        with self.assertRaises(ValueError):
            self.send_upgrade_receipt(ident, queued["id"], "applying", self.at + 3)
        self.fleet.poll(ident, self.key, STOPPED, [], now=self.at + 4, release=UPGRADABLE)
        stop = self.fleet.command(ident, "stop", ["card"], now=self.at + 4)
        with self.assertRaises(ValueError):
            self.send_upgrade_receipt(ident, queued["id"], "applying", self.at + 4)
        self.assertEqual(self.fleet.poll(ident, self.key, STOPPED, [], now=self.at + 5,
                                        release=UPGRADABLE)["commands"][0]["id"], stop["id"])
        self.fleet.poll(ident, self.key, STOPPED, [{"id": stop["id"], "status": "acknowledged"}],
                        now=self.at + 6, release=UPGRADABLE)
        self.assertEqual(self.send_upgrade_receipt(ident, queued["id"], "applying", self.at + 6), {"job": None})

    def test_upgrade_receipts_are_idempotent_and_installed_requires_an_own_fresh_report(self):
        ident = self.upgrade_node()
        queued = self.queue_upgrade(ident)
        job_id = queued["id"]
        with self.assertRaises(ValueError):
            self.send_upgrade_receipt(ident, job_id, "installed", self.at)
        offered = self.fleet.upgrade_poll(ident, self.key, now=self.at)
        self.assertEqual(offered, {"job": {**UPGRADE_PICK, "id": job_id, "expires": self.at + 900}})
        self.assertEqual(self.fleet.upgrade_poll(ident, self.key, now=self.at + 1), offered)
        self.assertEqual(self.send_upgrade_receipt(ident, job_id, "preparing", self.at + 2), {"job": None})
        self.assertEqual(self.send_upgrade_receipt(ident, job_id, "preparing", self.at + 3), {"job": None})
        self.assertEqual(self.fleet.list(now=self.at + 3)["nodes"][0]["upgrade"]["time"], self.at + 2)
        with self.assertRaises(ValueError):
            self.send_upgrade_receipt(ident, job_id, "rolled_back", self.at + 3)
        self.assertEqual(self.send_upgrade_receipt(ident, job_id, "applying", self.at + 4), {"job": None})
        with self.assertRaises(ValueError):
            self.send_upgrade_receipt(ident, job_id, "preparing", self.at + 5)
        other_key = secrets.token_urlsafe(32)
        other = self.node("Other", key=other_key)["id"]
        upgraded = {**UPGRADABLE, "version": "0.9.2"}
        self.fleet.poll(other, other_key, STOPPED, [], now=self.at + 5, release=upgraded)
        with self.assertRaises(ValueError):
            self.send_upgrade_receipt(ident, job_id, "installed", self.at + 5)
        self.fleet.poll(ident, self.key, STOPPED, [], now=self.at + 5, release=UPGRADABLE)
        with self.assertRaises(ValueError):
            self.send_upgrade_receipt(ident, job_id, "installed", self.at + 5)
        self.fleet.poll(ident, self.key, STOPPED, [], now=self.at + 6, release=upgraded)
        self.assertEqual(self.send_upgrade_receipt(ident, job_id, "installed", self.at + 6), {"job": None})
        self.assertEqual(self.send_upgrade_receipt(ident, job_id, "installed", self.at + 7), {"job": None})
        with self.assertRaises(ValueError):
            self.send_upgrade_receipt(ident, job_id, "failed", self.at + 7)
        row = next(row for row in self.fleet.list(now=self.at + 7)["nodes"] if row["id"] == ident)
        self.assertEqual(row["upgrade"], {**queued, "status": "installed", "time": self.at + 6})

    def test_upgrade_rollback_and_expiry_require_correct_fresh_reporting(self):
        ident = self.upgrade_node()
        first = self.queue_upgrade(ident)
        job = self.fleet.upgrade_poll(ident, self.key, now=self.at)["job"]
        self.send_upgrade_receipt(ident, first["id"], "preparing", self.at)
        self.send_upgrade_receipt(ident, first["id"], "applying", self.at + 1)
        with self.assertRaises(ValueError):
            self.send_upgrade_receipt(ident, first["id"], "rolled_back", self.at + 1)
        self.fleet.poll(ident, self.key, STOPPED, [], now=self.at + 2, release=UPGRADABLE)
        self.assertEqual(self.send_upgrade_receipt(ident, first["id"], "rolled_back", self.at + 2), {"job": None})
        second = self.queue_upgrade(ident, now=self.at + 3)
        offered = self.fleet.upgrade_poll(ident, self.key, now=self.at + 3)["job"]
        self.assertNotEqual(second["id"], job["id"])
        with self.assertRaises(ValueError):
            self.send_upgrade_receipt(ident, first["id"], "rolled_back", self.at + 3)
        self.send_upgrade_receipt(ident, second["id"], "preparing", self.at + 3)
        expires = offered["expires"]
        with self.assertRaises(ValueError):
            self.send_upgrade_receipt(ident, second["id"], "applying", expires)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT status FROM node_upgrades").fetchone(), ("expired",))
        row = self.fleet.list(now=expires)["nodes"][0]
        self.assertEqual(row["upgrade"], {**second, "status": "expired", "time": expires})
        self.assertEqual(self.fleet.upgrade_poll(ident, self.key, now=expires), {"job": None})

    def test_upgrade_retry_of_failed_preparation_keeps_original_time_and_liveness(self):
        ident = self.upgrade_node()
        job_id = self.queue_upgrade(ident)["id"]
        with self.assertRaises(FleetAuthError):
            self.fleet.upgrade_poll(ident, secrets.token_urlsafe(32), now=self.at + 1)
        self.assertEqual(self.fleet.upgrade_poll(ident, self.key, now=self.at + 1)["job"]["id"], job_id)
        with self.assertRaises(ValueError):
            self.send_upgrade_receipt(ident, job_id, "failed", self.at + 2)
        self.send_upgrade_receipt(ident, job_id, "preparing", self.at + 2)
        self.assertEqual(self.send_upgrade_receipt(ident, job_id, "failed", self.at + 3), {"job": None})
        self.assertEqual(self.send_upgrade_receipt(ident, job_id, "failed", self.at + 5), {"job": None})
        self.assertEqual(self.fleet.check(ident, self.key)["last_seen"], self.at)
        job = self.fleet.list(now=self.at + 5)["nodes"][0]["upgrade"]
        self.assertEqual((job["status"], job["time"]), ("failed", self.at + 3))

    def test_interrupted_update_receipt_requires_new_report_of_expected_running_version(self):
        ident = self.upgrade_node()
        queued = self.queue_upgrade(ident)
        self.fleet.upgrade_poll(ident, self.key, now=self.at)
        job_id = queued["id"]
        self.send_upgrade_receipt(ident, job_id, "preparing", self.at)
        self.send_upgrade_receipt(ident, job_id, "applying", self.at + 1)
        with self.assertRaises(ValueError):
            self.send_upgrade_receipt(ident, job_id, "unknown", self.at + 2)
        wrong_release = {**UPGRADABLE, "version": "0.9.0"}
        self.fleet.poll(ident, self.key, STOPPED, [], now=self.at + 2, release=wrong_release)
        with self.assertRaises(ValueError):
            self.send_upgrade_receipt(ident, job_id, "unknown", self.at + 2)
        self.fleet.poll(ident, self.key, STOPPED, [], now=self.at + 3, release=UPGRADABLE)
        self.assertEqual(self.send_upgrade_receipt(ident, job_id, "unknown", self.at + 3), {"job": None})
        self.assertEqual(self.send_upgrade_receipt(ident, job_id, "unknown", self.at + 4), {"job": None})
        row = self.fleet.list(now=self.at + 4)["nodes"][0]
        self.assertEqual(row["upgrade"], {**queued, "status": "unknown", "time": self.at + 3})
        self.assertEqual(self.fleet.command(ident, "start", ["card"], now=self.at + 4)["status"], "queued")

    def test_applying_update_expires_with_unknown_outcome(self):
        ident = self.upgrade_node()
        queued = self.queue_upgrade(ident)
        offer = self.fleet.upgrade_poll(ident, self.key, now=self.at)["job"]
        self.send_upgrade_receipt(ident, queued["id"], "preparing", self.at)
        self.send_upgrade_receipt(ident, queued["id"], "applying", self.at + 1)
        row = self.fleet.list(now=offer["expires"])["nodes"][0]
        self.assertEqual(row["upgrade"], {**queued, "status": "unknown", "time": offer["expires"]})
        self.assertEqual(self.fleet.upgrade_poll(ident, self.key, now=offer["expires"]), {"job": None})

    def test_revoking_a_node_cancels_its_queued_upgrade(self):
        ident = self.upgrade_node()
        queued = self.queue_upgrade(ident)
        self.fleet.revoke(ident, now=self.at + 1)
        row = self.fleet.list(now=self.at + 1)["nodes"][0]
        self.assertEqual(row["upgrade"], {**queued, "status": "cancelled", "time": self.at + 1})
        with self.assertRaises(ValueError):
            self.fleet.upgrade_poll(ident, self.key, now=self.at + 1)

    def test_client_upgrade_exchange_enforces_fixed_request_and_job_shape(self):
        ident = self.node()["id"]
        agent = Client("https://fleet.example.invalid", ident, self.key, None)
        job = {**UPGRADE_PICK, "id": "a" * 32, "expires": self.at + 399}
        with patch.object(agent, "_post", return_value={"job": job}) as post:
            self.assertEqual(agent.upgrade_exchange(), {"job": job})
            post.assert_called_once_with("/api/node/upgrade", {"id": ident, "key": self.key, "receipt": None})
            for invalid in ({"job": {**job, "url": "bad"}}, {"job": {**job, "expires": True}},
                            {"job": {**job, "sha256": "short"}}, {"job": None, "extra": {}}):
                post.return_value = invalid
                with self.assertRaises(ValueError):
                    agent.upgrade_exchange()
            post.return_value = {"job": None}
            receipt = {"id": job["id"], "status": "preparing"}
            self.assertEqual(agent.upgrade_exchange(receipt), {"job": None})
            post.assert_called_with("/api/node/upgrade", {"id": ident, "key": self.key, "receipt": receipt})
            count = post.call_count
            with self.assertRaises(ValueError):
                agent.upgrade_exchange({**receipt, "script": "untrusted"})
            self.assertEqual(post.call_count, count)

    def test_agent_persists_claim_before_effect_and_never_replays_after_interruption(self):
        issued = self.node()["id"]
        path = self.path.parent / "agent.sqlite"
        args = ("https://fleet.example.invalid", issued, self.key, path)
        agent = Client(*args)
        workers = [{"id": "card", "state": "stopped"}]
        wire = {"commands": [{"id": secrets.token_hex(16), "action": "start", "workers": ["card"], "expires": self.at + 10}]}

        def crash(_action, _workers):
            raise KeyboardInterrupt

        with patch.object(agent, "_post", return_value=wire), self.assertRaises(KeyboardInterrupt):
            agent.cycle(workers, crash, now=self.at)

        def should_not_execute(_action, _workers):
            self.fail("Agent executed a command with an unknown prior outcome")

        restarted = Client(*args)
        with patch.object(restarted, "_post", return_value=wire) as post:
            restarted.cycle(workers, should_not_execute, now=self.at + 1)
        self.assertEqual(post.call_args.args[0], "/api/node/poll")
        self.assertEqual(post.call_args.args[1]["acks"], [{"id": wire["commands"][0]["id"], "status": "unknown"}])

    def test_client_rejects_non_tls_except_explicit_literal_loopback(self):
        ident = self.node()["id"]
        path = self.path.parent / "agent.sqlite"
        for origin in ("http://myhost.invalid", "http://localhost:81", "https://name@host.invalid",
                       "https://host.invalid/path", "https://host.invalid?param", "ftp://host.invalid"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                Client(origin, ident, self.key, path, allow_loopback=True)
        with self.assertRaises(ValueError):
            Client("http://127.0.0.1:81", ident, self.key, path)
        Client("http://127.0.0.1:81", ident, self.key, path, allow_loopback=True)

    def test_transport_never_follows_redirect_and_requires_tls_hostname_verification(self):
        issued = self.node()
        path = self.path.parent / "agent.sqlite"
        seen = []

        class Redirect(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                seen.append(self.path)
                self.send_response(307)
                self.send_header("Location", "/elsewhere")
                self.end_headers()

        server = HTTPServer(("127.0.0.1", 0), Redirect)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            local = Client(f"http://127.0.0.1:{server.server_port}", issued["id"], self.key, path, allow_loopback=True)
            with self.assertRaises(ValueError):
                local.join(issued["code"])
            self.assertEqual(seen, ["/api/node/pair"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        tls = Client("https://fleet.example.invalid", issued["id"], self.key, self.path.parent / "tls.sqlite")
        self.assertTrue(tls._context.check_hostname)
        self.assertEqual(tls._context.verify_mode, ssl.CERT_REQUIRED)


if __name__ == "__main__":
    unittest.main()
