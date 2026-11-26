import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import neb_app as app
import neb_core as core
from neb_fleet import Fleet
from neb_release import VERSION
from test_neb_core import OWNER, SUPPORT


class ExamplePolicy:
    current = "enabled"

    def state(self):
        return self.current


class ExampleWorker:
    def __init__(self):
        self.calls, self.outbox, self.running = [], [], False

    def snapshot(self, limits):
        return [{"id": "gpu", "label": "Test worker", "adapter": "suprminer-opencl", "device": None,
                 "state": "running" if self.running else "stopped", "edge": None, "hot": None,
                 "hbm": None, "watts": None, "rate_hs": 1500000 if self.running else None}]

    def start(self, id_, profile, *, limits, max_seconds=None):
        self.calls.append(("start", id_, profile["wallet"]))
        if max_seconds is not None:
            self.support_window = max_seconds
        self.running = True

    def stop(self, id_):
        self.calls.append(("stop", id_))
        self.running = False

    def drain_events(self):
        events, self.outbox = self.outbox, []
        return events

    def journal(self, id_):
        if id_ != "gpu":
            raise ValueError("Unknown worker.")
        return [{"time": 2000, "type": "telemetry", "rate_hs": 1500000}]

    def close(self):
        self.running = False

    def is_idle(self):
        return not self.running


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
        self.policy = ExamplePolicy()
        self.service = app.Service(self.conf, self.base / "state", self.runtime, core.DEFAULT_POOLS, None,
                                   devfee=self.policy)
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
                code, observation, _ = req("GET", "/api/public/status")
                self.assertEqual(code, 200)
                self.assertEqual(set(observation), {"time", "workers", "hashrate_hs", "watts",
                                                    "temperatures", "shares", "devfee"})
                self.assertEqual(observation["workers"], {"total": 1, "running": 0, "attention": 0})
                self.assertEqual(observation["hashrate_hs"], 0)
                self.assertIsNone(observation["watts"])
                self.assertNotIn(OWNER, json.dumps(observation))
                self.assertNotIn("suprnova", json.dumps(observation))
                self.assertEqual(req("GET", "/api/logs?worker=gpu")[0], 401)
                self.assertEqual(req("GET", "/api/network")[0], 401)
                self.assertEqual(req("POST", "/api/network/refresh", {})[0], 401)
                self.assertEqual(req("POST", "/api/start", {"id": "gpu"}, ref="https://untrusted.example")[0], 403)
                self.assertEqual(req("POST", "/api/start", {"id": "gpu"})[0], 401)
                self.assertEqual(req("GET", "/api/runtime")[:2], (200, {"version": VERSION, "idle": True}))
                code, overview, _ = req("GET", "/api/public/overview")
                self.assertEqual(code, 200)
                self.assertEqual(overview["nodes"], {"online": 1, "total": 1})
                self.assertEqual(overview["workers"], observation["workers"])
                self.assertNotIn(OWNER, json.dumps(overview))
                status, _, cookie = req("POST", "/api/login", {"password": "fixture password only"})
                self.assertEqual(status, 200)
                self.assertIn("HttpOnly; SameSite=Strict", cookie)
                cookie = cookie.split(";", 1)[0]
                self.assertEqual(req("GET", "/api/devfee", cookie=cookie)[0], 404)
                self.assertEqual(req("POST", "/api/devfee", {"paused": True}, cookie=cookie)[0], 404)
                self.assertEqual(req("GET", "/policy", cookie=cookie)[0], 404)
                code, operator, _ = req("GET", "/api/status", cookie=cookie)
                self.assertEqual(code, 200)
                self.assertEqual(operator["profile"], {"worker": "test", "pool": "suprnova-eu", "wallet_configured": True})
                self.assertEqual(operator["overview"], overview)
                self.assertNotIn(OWNER, json.dumps(operator))
                self.assertEqual(req("GET", "/api/wallets", cookie=cookie)[0], 404)
                self.assertEqual(req("POST", "/api/profile", {"worker": "renamed", "pool": "suprnova-eu"}, cookie=cookie)[0], 200)
                self.assertEqual(req("GET", "/api/network", cookie=cookie)[1]["state"], "unchecked")
                self.assertEqual(req("GET", "/api/logs?worker=other", cookie=cookie)[0], 400)
                code, logs, _ = req("GET", "/api/logs?worker=gpu", cookie=cookie)
                self.assertEqual(code, 200)
                self.assertEqual(logs["entries"][0]["rate_hs"], 1500000)
                self.assertEqual(req("POST", "/api/start", {"id":"different"}, cookie=cookie)[0], 409)
                self.assertEqual(req("POST", "/api/start", {"id":"gpu", "wallet": SUPPORT}, cookie=cookie)[0], 200)
                self.assertEqual(self.runtime.calls, [("start", "gpu", OWNER)])
                self.assertEqual(req("GET", "/api/runtime")[1]["idle"], False)
                self.assertEqual(req("GET", "/api/public/status")[1]["hashrate_hs"], 1500000)
                self.assertEqual(req("POST", "/api/profile", {"wallet": SUPPORT, "worker":"test", "pool":"suprnova-eu"}, cookie=cookie)[0], 409)
                self.assertEqual(req("POST", "/api/logout", {}, cookie=cookie)[0], 200)
                self.assertEqual(req("GET", "/api/status", cookie=cookie)[0], 401)
            finally:
                conn.close()
                server.shutdown()
                thread.join()

    def test_guest_link_and_dashboard_serve_one_document(self):
        with app.Server(("127.0.0.1", 0), self.service, self.auth, app.HERE / "web", None) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                documents = []
                for route in ("/", "/view"):
                    conn.request("GET", route)
                    response = conn.getresponse()
                    self.assertEqual(response.status, 200)
                    documents.append(response.read())
                self.assertTrue(documents[0] == documents[1], "The shared dashboard should serve both entry points.")
            finally:
                conn.close()
                server.shutdown()
                thread.join()

    def test_node_update_reserves_idle_runtime_until_release(self):
        with self.service.lock:
            self.assertTrue(self.service.reserve_upgrade())
            self.assertTrue(self.service.runtime_status()["idle"])
            with self.assertRaisesRegex(ValueError, "обновлен"):
                self.service.change("start", {"id": "gpu"})
        self.assertEqual(self.runtime.calls, [])
        self.service.finish_upgrade()
        self.service.change("start", {"id": "gpu"})
        self.assertFalse(self.service.reserve_upgrade())

    def test_controller_disables_local_mutations_even_with_a_worker_row(self):
        service = app.Service(self.conf, self.base / "state", self.runtime, core.DEFAULT_POOLS, None,
                              devfee=self.policy, controller_only=True)
        service.poll()
        self.assertEqual(service.status()["capabilities"], {"local_workers": False, "wallet_tools": False})
        for action in ("start", "stop", "profile", "limits"):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, "Ноды"):
                service.change(action, {"id": "gpu"})
        with self.assertRaisesRegex(ValueError, "Ноды"):
            service.group("start", ["gpu"])
        self.assertEqual(self.runtime.calls, [])

    def test_thermal_stop_is_in_history_without_an_active_output_rate(self):
        now = self.service.last + 20
        self.thermal_event = {"type": "thermal", "time": now, "worker": "gpu",
                              "detail": "Over limit", "status": "stopped"}
        self.runtime.outbox.append(self.thermal_event)
        self.service.poll(now=now)
        data = self.service.archive.history("31d", now=now + 10)
        self.assertEqual(data["incidents"][0]["worker"], "gpu")
        self.assertEqual(data["thresholds"][0]["edge_limit"], core.LIMITS["edge_limit"])
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
        self.assertEqual(self.runtime.support_window, 3)
        with patch("neb_app.time.monotonic", return_value=13.1):
            self.service.poll()
        self.assertEqual(self.runtime.calls, [("start","gpu",OWNER), ("stop","gpu"),
                                              ("start","gpu",SUPPORT), ("stop","gpu"), ("start","gpu",OWNER)])
        self.assertFalse(self.service.status()["support"]["active_workers"])

    def test_owner_pause_returns_active_worker_and_skips_credit_until_resumed(self):
        self.service.clock = core.SupportClock(core.support_config(
            {"enabled": True, "wallet": SUPPORT, "basis_points": 110}), self.base / "credit.json", window=30)
        self.assertEqual(self.service.status()["support"]["state"], "idle")
        self.service.change("start", {"id": "gpu"})
        self.assertEqual(self.service.status()["support"]["state"], "counting")
        self.service.clock.credit["gpu"] = 30
        with patch("neb_app.time.monotonic", return_value=10):
            self.service.poll()
        self.assertEqual(self.service.status()["support"]["state"], "active")
        self.policy.current = "paused"
        with patch("neb_app.time.monotonic", return_value=12):
            self.service.poll()
        self.assertEqual(self.runtime.calls[-2:], [("stop", "gpu"), ("start", "gpu", OWNER)])
        self.assertEqual(self.service.status()["support"]["state"], "paused")
        self.assertEqual(self.service.clock.credit["gpu"], 0)
        with patch("neb_app.time.monotonic", return_value=14):
            self.service.poll()
        self.assertEqual(self.service.clock.credit["gpu"], 0)
        self.policy.current = "unavailable"
        self.assertEqual(self.service.status()["support"]["state"], "unavailable")

    def test_policy_unavailable_blocks_new_window_even_with_funded_credit(self):
        self.service.clock = core.SupportClock(core.support_config(
            {"enabled": True, "wallet": SUPPORT, "basis_points": 110}), self.base / "credit.json", window=30)
        self.service.clock.credit["gpu"] = 30
        self.policy.current = "unavailable"
        self.service.change("start", {"id": "gpu"})
        self.service.poll()
        self.assertEqual(self.runtime.calls, [("start", "gpu", OWNER)])
        self.assertEqual(self.service.status()["support"]["state"], "unavailable")

    def test_network_forecast_uses_fresh_fleet_rates_and_network_difficulty(self):
        now = self.service.last
        registry = self.service.fleet = Fleet(self.base / "fleet.sqlite")
        for label, key, stamp, rate in (("Running", "a" * 43, now, 2500000),
                                         ("Stale", "b" * 43, now - 60, 10000000)):
            invite = registry.enroll(label, now=stamp)
            registry.pair(invite["id"], invite["code"], key, now=stamp)
            registry.poll(invite["id"], key, [{"id": "gpu", "state": "running", "rate_hs": rate}], [], now=stamp)
        network = {"state": "fresh", "data": {"difficulty": 86400, "reward_neb": 19}}
        with patch.object(self.service.network, "status", return_value=network):
            self.assertEqual(self.service.network_status()["estimate_neb_day"], 2500000 * 19)
            self.runtime.running = True
            self.service.poll()
            self.assertEqual(self.service.network_status()["farm_rate_hs"], 4000000)
            self.assertEqual(self.service.network_status()["estimate_neb_day"], 4000000 * 19)
            network["state"] = "stale"
            self.assertIsNone(self.service.network_status()["estimate_neb_day"])

    def test_atlas_is_only_read_through_operator_status_and_history(self):
        stamp = self.service.last
        snapshot = {"status": "online", "time": stamp, "rate_hs": 6000000, "watts": 200,
                    "temperatures": {"edge": 65, "hot": None, "hbm": None}, "shares": {"accepted": 2, "rejected": 0},
                    "gpu_counts": {"active": 1, "total": 4}, "gpus": []}
        archive = {"range": "30d", "from": stamp - 3600, "to": stamp, "step": 60,
                   "series": [{"t": stamp - 120, "rate": 6}], "incidents": []}
        self.service.atlas = SimpleNamespace(snapshot=lambda: snapshot, history=lambda selected: archive if selected == "30d" else None)
        with app.Server(("127.0.0.1", 0), self.service, self.auth, app.HERE / "web", None) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                def get(path, token=None):
                    connection.request("GET", path, headers={"Cookie": f"neb_sid={token}"} if token else {})
                    response = connection.getresponse()
                    return response.status, json.loads(response.read())

                history = "/api/history?range=30d&source=atlas"
                self.assertEqual(get(history)[0], 401)
                self.assertEqual(get("/api/public/overview")[1]["hashrate_hs"], 0)
                token = self.auth.enter("fixture password only", "")
                status = get("/api/status", token)[1]
                self.assertEqual(status["overview"]["hashrate_hs"], 6000000)
                self.assertEqual(status["atlas"]["gpu_counts"], {"active": 1, "total": 4})
                self.assertEqual(get(history, token), (200, {**archive, "source": "atlas"}))
            finally:
                connection.close()
                server.shutdown()
                thread.join(timeout=3)

    def test_power_routes_require_https_operator_and_node_key(self):
        from test_neb_fleet import POWER_GPU, WINDOWS_RELEASE

        registry = self.service.fleet = Fleet(self.base / "fleet.sqlite")
        invite = registry.enroll("NVIDIA fixture")
        node = invite["id"]
        key = "e" * 43
        registry.pair(node, invite["code"], key)
        registry.poll(node, key, [], [], release=WINDOWS_RELEASE,
                      hardware={"devices": [POWER_GPU], "power_control": True})
        origin = "https://power.fixture.invalid"
        with app.Server(("127.0.0.1", 0), self.service, self.auth, app.HERE / "web", origin) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                def post(path, body, *, host="power.fixture.invalid", ref=origin, cookie=None):
                    headers = {"Host": host, "Content-Type": "application/json"}
                    if ref is not None:
                        headers["Origin"] = ref
                    if cookie:
                        headers["Cookie"] = cookie
                    connection.request("POST", path, json.dumps(body), headers)
                    response = connection.getresponse()
                    return response.status, json.loads(response.read())

                queue, pull = "/api/fleet/power", "/api/node/power"
                setting = {"node": node, "gpu": POWER_GPU["id"], "watts": 225}
                token = "neb_sid=" + self.auth.enter("fixture password only", "")
                self.assertEqual(post(queue, setting)[0], 401)
                self.assertEqual(post(queue, setting, cookie=token, ref="https://another.invalid")[0], 403)
                loopback = f"127.0.0.1:{server.server_port}"
                self.assertEqual(post(queue, setting, host=loopback, ref="http://" + loopback, cookie=token)[0], 403)
                self.assertEqual(post(queue, setting, cookie=token)[0], 202)
                body = {"id": node, "key": key, "receipt": None}
                self.assertEqual(post(pull, body, cookie=token, ref=None)[0], 403)
                self.assertEqual(post(pull, {**body, "key": "x" * 43}, ref=None)[0], 401)
                status, result = post(pull, body, ref=None)
                self.assertEqual(status, 200)
                self.assertEqual(result["job"]["watts"], 225)
                self.assertEqual(post(pull, body, ref=None), (200, {"job": None}))
            finally:
                connection.close()
                server.shutdown()
                thread.join(timeout=3)

    def test_expired_support_returns_to_operator_before_credit_checkpoint(self):
        self.service.clock = core.SupportClock(core.support_config(
            {"enabled": True, "wallet": SUPPORT, "basis_points": 50}), self.base / "credit.json", window=3)
        self.service.change("start", {"id":"gpu"})
        self.service.clock.credit["gpu"] = 3
        with patch("neb_app.time.monotonic", return_value=10):
            self.service.poll()
        original = self.runtime.snapshot
        self.runtime.snapshot = lambda limits: [
            {**original(limits)[0], "id": "other", "state": "running"},
            {**original(limits)[0], "state": "stopped", "exit_reason": "time-limit"}]
        with patch("neb_app.time.monotonic", return_value=100), patch.object(
                self.service.clock, "flush", side_effect=OSError("Disk full")), self.assertRaises(OSError):
            self.service.poll()
        self.assertEqual(self.runtime.calls[-2:], [("stop", "gpu"), ("start", "gpu", OWNER)])
        self.assertEqual(self.service.clock.active(), [])
