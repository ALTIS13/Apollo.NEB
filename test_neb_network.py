import io
import json
import threading
import unittest
from unittest.mock import patch

from neb_network import Network


def pool_payload():
    return {
        "pool": {
            "id": "neb",
            "coin": {"type": "NEB", "symbol": "NEB", "algorithm": "neb-pot"},
            "networkStats": {
                "blockHeight": 4379,
                "networkDifficulty": "93710651483947.2969",
                "networkHashrate": 1358555191724.586,
                "connectedPeers": 7,
            },
            "blockReward": 19,
            "blockTime": 68.97817037892183,
            "blockTimeConfigured": 60,
            "lastBlockFound": {"height": 4372, "status": "pending"},
        },
    }


class Response(io.BytesIO):
    def __init__(self, body, status=200, content_type="application/json; charset=utf-8"):
        super().__init__(body)
        self.status = status
        self.content_type = content_type

    def getheader(self, name, default=None):
        if name.lower() == "content-type":
            return self.content_type
        if name.lower() == "location":
            return "https://untrusted.test/stats"
        return default


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.now = 120.0
        for patcher in (
            patch.object(Network, "_cache", type(Network._cache)()),
            patch("neb_network.monotonic", side_effect=lambda: self.now),
            patch("neb_network.time", return_value=1795265445),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        transport = patch("neb_network.HTTPSConnection")
        self.connection_factory = transport.start()
        self.addCleanup(transport.stop)
        self.connection = self.connection_factory.return_value
        self.client = Network()
        self.respond()

    def respond(self, data=None, **options):
        payload = pool_payload() if data is None else data
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.connection.getresponse.return_value = Response(body, **options)

    def test_status_does_not_contact_the_pool(self):
        self.assertEqual(self.client.status(), {"state": "unchecked", "data": None, "retry_after": 0})
        self.connection_factory.assert_not_called()

    def test_refresh_uses_documented_endpoint_and_returns_normalized_values(self):
        self.assertEqual(self.client.refresh(), {
            "state": "fresh",
            "data": {
                "height": 4379, "difficulty": 93710651483947.2969, "hashrate_hs": 1358555191724.586,
                "reward_neb": 19, "target_seconds": 60, "observed_at": 1795265445,
            },
            "retry_after": 60,
        })
        self.connection_factory.assert_called_once_with("neb.suprnova.cc", timeout=4)
        self.connection.request.assert_called_once_with("GET", "/api/pools/neb",
                                                        headers={"Accept": "application/json"})
        self.connection.close.assert_called_once()

    def test_failed_refresh_keeps_last_observation_but_marks_it_stale(self):
        fresh = self.client.refresh()
        self.now += 60
        self.connection.getresponse.side_effect = TimeoutError
        self.assertEqual(self.client.refresh(), {"state": "stale", "data": fresh["data"], "retry_after": 60})
        self.assertEqual(self.client.status()["state"], "stale")

    def test_shared_retry_window_prevents_requests_from_other_instances(self):
        self.client.refresh()
        self.now += 59.5
        self.assertEqual(Network().refresh()["retry_after"], 1)
        self.connection_factory.assert_called_once()
        self.now += 0.5
        self.respond()
        self.assertEqual(Network().refresh()["state"], "fresh")
        self.assertEqual(self.connection_factory.call_count, 2)

    def test_status_becomes_stale_when_observation_expires_without_polling(self):
        self.client.refresh()
        self.now += 300
        status = self.client.status()
        self.assertEqual(status["state"], "stale")
        self.assertEqual(status["data"]["observed_at"], 1795265445)
        self.assertEqual(status["retry_after"], 0)
        self.connection_factory.assert_called_once()

    def test_concurrent_refresh_does_not_start_another_request(self):
        waiting = threading.Event()
        resume = threading.Event()
        response = self.connection.getresponse.return_value

        def wait_for_release():
            waiting.set()
            resume.wait(2)
            return response

        self.connection.getresponse.side_effect = wait_for_release
        results = []
        worker = threading.Thread(target=lambda: results.append(self.client.refresh()), daemon=True)
        worker.start()
        try:
            self.assertTrue(waiting.wait(1))
            self.assertEqual(Network().status()["state"], "loading")
            self.assertEqual(Network().refresh()["state"], "loading")
            self.connection_factory.assert_called_once()
        finally:
            resume.set()
            worker.join(3)
        self.assertEqual(results[0]["state"], "fresh")

    def test_invalid_payload_is_unavailable_without_a_snapshot(self):
        for group, key, value in [
            ("pool", "id", "another"),
            ("coin", "symbol", "BTC"),
            ("networkStats", "blockHeight", True),
            ("networkStats", "blockHeight", -1),
            ("networkStats", "networkDifficulty", "NaN"),
            ("networkStats", "networkDifficulty", 0),
            ("networkStats", "networkHashrate", float("inf")),
            ("pool", "blockReward", -1),
            ("pool", "blockTimeConfigured", 0),
        ]:
            with self.subTest(key=key, value=value):
                payload = pool_payload()
                pool = payload["pool"]
                target = pool if group == "pool" else pool[group]
                target[key] = value
                self.respond(payload)
                self.assertEqual(self.client.refresh(), {"state": "unavailable", "data": None, "retry_after": 60})
                self.now += 60

    def test_zero_reward_is_a_valid_observation(self):
        payload = pool_payload()
        payload["pool"]["blockReward"] = 0
        self.respond(payload)
        self.assertEqual(self.client.refresh()["data"]["reward_neb"], 0)

    def test_redirect_is_rejected_without_another_request(self):
        self.respond(status=302)
        self.assertEqual(self.client.refresh()["state"], "unavailable")
        self.connection_factory.assert_called_once()
        self.connection.request.assert_called_once()

    def test_rejects_json_exceeding_body_limit(self):
        self.respond(json.dumps(pool_payload()).encode() + b" " * 65536)
        self.assertEqual(self.client.refresh()["state"], "unavailable")


if __name__ == "__main__":
    unittest.main()
