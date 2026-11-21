from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import secrets
import ssl
import tempfile
import threading
import unittest
from unittest.mock import patch

from neb_fleet import Client, Fleet


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
