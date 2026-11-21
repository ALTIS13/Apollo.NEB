import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import neb_app as app
import neb_core as core
from test_neb_core import OWNER, SUPPORT


class ExampleWorker:
    def __init__(self):
        self.calls, self.outbox, self.running = [], [], False

    def snapshot(self, limits):
        return [{"id": "gpu", "label": "Test worker", "adapter": "suprminer-opencl", "device": None,
                 "state": "running" if self.running else "stopped", "edge": None, "hot": None,
                 "hbm": None, "watts": None, "rate_hs": 1500000 if self.running else None}]

    def start(self, id_, profile, *, limits):
        self.calls.append(("start", id_, profile["wallet"]))
        self.running = True

    def stop(self, id_):
        self.calls.append(("stop", id_))
        self.running = False

    def drain_events(self):
        events, self.outbox = self.outbox, []
        return events

    def close(self):
        self.running = False


class ServerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.base.joinpath("state").mkdir()
        self.conf = self.base / "config"
        self.login = core.new_login("fixture password only")
        self.runtime = ExampleWorker()
        core.store(self.conf / "profile.json", {"wallet": OWNER, "worker": "test", "pool": "suprnova-eu"})
        self.service = app.Service(self.conf, self.base / "state", self.runtime, core.DEFAULT_POOLS, None)
        self.service.poll()
        self.auth = core.Auth(self.login, self.base / "state" / "auth.sqlite")

    def test_auth_then_start_only_named_worker_with_operator_wallet(self):
        with app.Server(("127.0.0.1", 0), self.service, self.auth, app.HERE / "web", None) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_port
            origin = f"http://127.0.0.1:{port}"
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            try:
                def req(method, route, body=None, *, ref=origin, cookie=None, host=None):
                    headers = {"Host": host or f"127.0.0.1:{port}"}
                    if ref:
                        headers["Origin"] = ref
                    if cookie:
                        headers["Cookie"] = cookie
                    if body is not None:
                        headers["Content-Type"] = "application/json"
                    conn.request(method, route, None if body is None else json.dumps(body), headers=headers)
                    res = conn.getresponse()
                    data = json.loads(res.read())
                    return res.status, data, res.getheader("Set-Cookie")

                self.assertEqual(req("GET", "/api/status", host="untrusted.example")[0], 403)
                self.assertEqual(req("GET", "/api/status")[0], 401)
                self.assertEqual(req("POST", "/api/start", {"id": "gpu"}, ref="https://untrusted.example")[0], 403)
                self.assertEqual(req("POST", "/api/start", {"id": "gpu"})[0], 401)
                status, _, cookie = req("POST", "/api/login", {"password": "fixture password only"})
                self.assertEqual(status, 200)
                self.assertIn("HttpOnly; SameSite=Strict", cookie)
                cookie = cookie.split(";", 1)[0]
                self.assertEqual(req("POST", "/api/start", {"id":"different"}, cookie=cookie)[0], 409)
                self.assertEqual(req("POST", "/api/start", {"id":"gpu", "wallet": SUPPORT}, cookie=cookie)[0], 200)
                self.assertEqual(self.runtime.calls, [("start", "gpu", OWNER)])
                self.assertEqual(req("POST", "/api/profile", {"wallet": SUPPORT, "worker":"test", "pool":"suprnova-eu"}, cookie=cookie)[0], 409)
                self.assertEqual(req("POST", "/api/logout", {}, cookie=cookie)[0], 200)
                self.assertEqual(req("GET", "/api/status", cookie=cookie)[0], 401)
            finally:
                conn.close()
                server.shutdown()
                thread.join()

    def test_thermal_stop_is_in_history_without_an_active_output_rate(self):
        self.thermal_event = {"type": "thermal", "time": 2000, "worker": "gpu",
                              "detail": "Over limit", "status": "stopped"}
        self.runtime.outbox.append(self.thermal_event)
        self.service.poll(now=2000)
        data = self.service.archive.history("30m", now=2010)
        self.assertEqual(data["incidents"][0]["worker"], "gpu")
        self.assertEqual(data["series"][-1]["rate"], 0.)
        self.assertIsNone(data["series"][-1]["watts"])

    def test_support_switches_only_after_funding_and_returns_to_operator(self):
        self.service.clock = core.SupportClock(core.support_config(
            {"enabled": True, "wallet": SUPPORT, "basis_points": 50}), self.base / "credit.json", window=3)
        self.service.change("start", {"id":"gpu"})
        self.service.clock.credit["gpu"] = 3
        with patch("neb_app.time.monotonic", return_value=10):
            self.service.poll()
        self.assertEqual(self.service.status()["support"]["active_workers"], ["gpu"])
        with patch("neb_app.time.monotonic", return_value=13.1):
            self.service.poll()
        self.assertEqual(self.runtime.calls, [("start","gpu",OWNER), ("stop","gpu"),
                                              ("start","gpu",SUPPORT), ("stop","gpu"), ("start","gpu",OWNER)])
        self.assertFalse(self.service.status()["support"]["active_workers"])
