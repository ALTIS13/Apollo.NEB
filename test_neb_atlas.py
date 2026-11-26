from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import ssl
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


AT = 1_760_000_000
GPU = "0000:08:00.0"
GPU_SECOND = "09:00.0"
PRIVATE = "private-fixture-do-not-export"


class FakeAtlas(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.server.requests.append((self.path, dict(self.headers)))
        status, media, value, extras = self.server.routes.get(self.path, (404, "application/json", {}, {}))
        if callable(value):
            return value(self)
        data = value if isinstance(value, bytes) else json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", media)
        for name, content in extras.items():
            self.send_header(name, content)
        if "Content-Length" not in extras:
            self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def packet(**changes):
    return {
        "time": AT,
        "local": {"rate": 0, "accepted": 0, "rejected": None, "address": PRIVATE},
        "gpus": [
            {"id": GPU, "present": True, "state": "mining", "label": PRIVATE, "rate": 0,
             "watts": 100, "edge": 64, "hot": 82, "hbm": None, "accepted": 0},
            {"id": GPU_SECOND, "present": True, "state": "retrying", "watts": None, "edge": 61},
            {"id": "0000:0a:00.0", "present": False, "state": "mining", "watts": 800, "hbm": 90},
        ],
        "farm": {"active_count": 5, "total_count": 6, "private": PRIVATE},
        "events": [{"message": PRIVATE}],
        "profile": {"address": PRIVATE},
        **changes,
    }


class AtlasTests(unittest.TestCase):
    def setUp(self):
        import neb_atlas
        self.atlas = neb_atlas
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAtlas)
        self.server.requests = []
        self.server.routes = {}
        self.addCleanup(self.server.server_close)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 1)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_port
        self.source = self.atlas.AtlasSource(self.port)

    def respond(self, data, *, path="/api/status", status=200, media="application/json", headers=None):
        self.server.routes[path] = (status, media, data, headers or {})

    def test_opt_in_requires_one_integer_port_and_a_regular_file(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp)
            self.assertIsNone(self.atlas.AtlasSource.load(config))
            file = config / "atlas-source.json"
            file.write_text(json.dumps({"port": self.port}), encoding="utf-8")
            self.assertIsInstance(self.atlas.AtlasSource.load(config), self.atlas.AtlasSource)
            for raw in (
                b'{"port":true}', b'{"port":"18000"}', b'{"port":1023}', b'{"port":65536}',
                b'{"port":18000,"port":18001}', b'{"port":18000,"host":"127.0.0.1"}',
                b'{"port":18000,"credential":"fixture"}', b'[]', b"x" * 4096,
            ):
                file.write_bytes(raw)
                with self.assertRaises(ValueError):
                    self.atlas.AtlasSource.load(config)
            file.unlink()
            file.mkdir()
            with self.assertRaises(ValueError):
                self.atlas.AtlasSource.load(config)
        self.assertEqual(self.server.requests, [])

    def test_tls_source_uses_fixed_private_address_and_explicit_service_credentials(self):
        options = {"address": "10.203.42.1", "port": 18762, "server_name": "atlas.lab.test"}
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp)
            credentials = config / "credentials"
            credentials.mkdir()
            for filename in ("atlas-read-ca.pem", "atlas-read-client.pem", "atlas-read-client.key"):
                f = credentials / filename
                f.write_text("fixture", encoding="ascii")
                f.chmod(0o600)
            path = config / "atlas-source.json"
            path.write_text(json.dumps({"tls": options}), encoding="ascii")
            with patch.object(sys, "platform", "linux"), \
                 patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": str(credentials)}), \
                 patch.object(ssl, "SSLContext") as context:
                self.assertIsNotNone(self.atlas.AtlasSource.load(config))
                context.assert_called_once_with(ssl.PROTOCOL_TLS_CLIENT)
                context.return_value.load_verify_locations.assert_called_once_with(
                    cafile=os.fspath(credentials / "atlas-read-ca.pem"))
                context.return_value.load_cert_chain.assert_called_once_with(
                    certfile=os.fspath(credentials / "atlas-read-client.pem"),
                    keyfile=os.fspath(credentials / "atlas-read-client.key"), password="")
                (credentials / "atlas-read-client.key").unlink()
                with self.assertRaises(ValueError):
                    self.atlas.AtlasSource.load(config)

    def test_tls_source_can_load_certificate_from_protected_config(self):
        options = {"address": "10.203.42.1", "port": 18762, "server_name": "atlas.lab.test"}
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp).resolve()
            (config / "atlas-source.json").write_text(json.dumps({"tls": options}), encoding="utf-8")
            credentials = config / "atlas-read-credentials"
            credentials.mkdir(mode=0o700)
            for name in ("atlas-read-ca.pem", "atlas-read-client.pem", "atlas-read-client.key"):
                file = credentials / name
                file.touch(mode=0o600)
            with patch.object(sys, "platform", "linux"), patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": ""}), \
                    patch.object(self.atlas.os, "geteuid", return_value=credentials.stat().st_uid, create=True), \
                    patch.object(self.atlas.ssl, "SSLContext") as context:
                self.assertIsNotNone(self.atlas.AtlasSource.load(config))
                context.return_value.load_verify_locations.assert_called_once_with(
                    cafile=str(credentials / "atlas-read-ca.pem"))

    def test_tls_options_reject_ambiguous_or_public_targets(self):
        valid = {"address": "10.203.42.1", "port": 18762, "server_name": "atlas.lab.test"}
        invalid = (
            {"address": "8.8.8.8"}, {"address": "169.254.169.254"}, {"address": "atlas.example.test"},
            {"address": "010.203.42.1"}, {"port": True}, {"port": 80}, {"port": 65536},
            {"server_name": "atlas.lab.test/extra"}, {"server_name": "atlas.lab.test:443"},
            {"server_name": "-atlas.lab.test"}, {"server_name": "atlas..test"}, {"ca": "/tmp/other"},
        )
        with tempfile.TemporaryDirectory() as temp, patch.object(sys, "platform", "linux"):
            config = Path(temp)
            path = config / "atlas-source.json"
            for filename in ("atlas-read-ca.pem", "atlas-read-client.pem", "atlas-read-client.key"):
                f = config / filename
                f.write_text("fixture", encoding="ascii")
                f.chmod(0o600)
            for change in invalid:
                with self.subTest(change=change), \
                     patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": temp}), \
                     patch.object(ssl, "SSLContext") as context:
                    path.write_text(json.dumps({"tls": {**valid, **change}}), encoding="ascii")
                    with self.assertRaises(ValueError):
                        self.atlas.AtlasSource.load(config)
                    context.assert_not_called()

    def test_live_projection_preserves_zero_and_does_not_export_raw_strings(self):
        self.respond(packet())
        live = self.source.snapshot(now=AT)
        self.assertEqual(live, {
            "status": "online", "time": AT, "rate_hs": 0, "watts": None,
            "temperatures": {"edge": 64, "hot": 82, "hbm": None},
            "shares": {"accepted": 0, "rejected": None},
            "gpu_counts": {"active": 1, "total": 2},
            "gpus": [
                {"id": GPU, "label": "GPU 0", "state": "mining", "rate_hs": 0, "watts": 100,
                 "edge": 64, "hot": 82, "hbm": None, "accepted": 0, "rejected": None},
                {"id": GPU_SECOND, "label": "GPU 1", "state": "retrying", "rate_hs": None, "watts": None,
                 "edge": 61, "hot": None, "hbm": None, "accepted": None, "rejected": None},
            ],
        })
        self.assertNotIn(PRIVATE, json.dumps(live))

    def test_snapshot_cached_data_must_still_be_fresh_and_isolated(self):
        self.respond(packet(gpus=[]))
        early = self.source.snapshot(now=AT - 1)
        self.assertEqual(early["status"], "offline")
        self.assertIsNone(early["gpu_counts"]["total"])
        fresh = self.source.snapshot(now=AT + 30)
        self.assertEqual(fresh["status"], "online")
        self.assertEqual(fresh["gpu_counts"], {"active": 0, "total": 0})
        fresh["shares"]["accepted"] = 999
        self.assertEqual(self.source.snapshot(now=AT + 30)["shares"]["accepted"], 0)
        stale = self.source.snapshot(now=AT + 31)
        self.assertEqual((stale["status"], stale["gpus"], stale["time"]), ("offline", [], None))
        self.assertEqual(len(self.server.requests), 1)

    def test_duplicate_gpu_address_forms_cannot_double_count_one_card(self):
        data = packet()
        data["gpus"][1].update(id="08:00.0", watts=120)
        self.respond(data)
        live = self.source.snapshot(now=AT)
        self.assertEqual([gpu["id"] for gpu in live["gpus"]], [GPU])
        self.assertIsNone(live["watts"])
        self.assertEqual(live["gpu_counts"], {"active": None, "total": None})

    def test_transport_is_fixed_loopback_and_sends_no_credentials(self):
        self.respond(packet())
        with patch("socket.getaddrinfo", side_effect=AssertionError("DNS attempted")), patch.dict(os.environ, {
            "HTTP_PROXY": "http://127.0.0.1:9", "ALL_PROXY": "http://127.0.0.1:9", "NO_PROXY": "",
        }):
            self.assertEqual(self.source.snapshot(now=AT)["status"], "online")
        path, headers = self.server.requests[0]
        self.assertEqual(path, "/api/status")
        self.assertEqual(headers.get("Host"), f"127.0.0.1:{self.port}")
        self.assertFalse({"cookie", "authorization", "proxy-authorization", "origin"} & {k.lower() for k in headers})

    def test_auth_redirect_and_malformed_responses_are_offline(self):
        for status, media, value, headers in (
            (401, "application/json", {"detail": PRIVATE}, {}),
            (403, "application/json", {"detail": PRIVATE}, {}),
            (302, "application/json", {}, {"Location": f"http://127.0.0.1:{self.port}/redirect-target"}),
            (200, "text/html", b"<html>login</html>", {}),
            (200, "application/json", b'{"time":1,"time":2}', {}),
            (200, "application/json", {}, {"Content-Length": "524289"}),
            (200, "application/json", b'{}', {"Content-Length": "99"}),
        ):
            with self.subTest(status=status, media=media, headers=headers):
                self.respond(value, status=status, media=media, headers=headers)
                offline = self.atlas.AtlasSource(self.port).snapshot(now=AT)
                self.assertEqual(offline["status"], "offline")
                self.assertIsNone(offline["rate_hs"])
                self.assertNotIn(PRIVATE, json.dumps(offline))
        self.assertNotIn("/redirect-target", [path for path, _ in self.server.requests])

    def test_history_preserves_missing_buckets_and_only_explicit_thermal_causes(self):
        rows = [
            {"t": AT - 180, "rate": 2.5, "samples": 2, "watts": 230, "hot": 81},
            {"t": AT - 60, "rate": 0, "watts": None},
        ]
        self.respond({
            "range": "6h", "from": AT - 21600, "to": AT, "step": 60, "count": 3,
            "series": rows, "limits": {"edge_limit": 80},
            "incidents": [
                {"id": 42, "time": AT - 60, "gpu": GPU, "status": "unconfirmed",
                 "reasons": ["edge-hot", "foreign-process"], "detail": PRIVATE},
                {"id": 43, "time": AT - 61, "gpu": GPU, "status": "stopped", "reasons": ["ownership-unknown"]},
                {"id": 44, "time": AT - 62, "gpu": GPU, "status": "stopped", "reasons": ["temperature-missing"]},
            ],
        }, path="/api/history?range=6h")
        history = self.source.history("6h")
        self.assertEqual(set(history), {"range", "from", "to", "step", "count", "series", "incidents"})
        self.assertEqual((history["count"], history["step"]), (3, 60))
        self.assertEqual([(p["t"], p["rate"], p["samples"]) for p in history["series"]],
                         [(AT - 180, 2.5, 2), (AT - 60, 0, None)])
        self.assertIsNone(history["series"][1]["watts"])
        incident, = history["incidents"]
        self.assertEqual((incident["type"], incident["time"], incident["worker"], incident["status"]),
                         ("thermal", AT - 60, GPU, "unconfirmed"))
        self.assertNotIn(PRIVATE, json.dumps(history))
        self.assertEqual(len(self.server.requests), 1)
        history["series"].clear()
        self.assertEqual(len(self.source.history("6h")["series"]), 2)
        self.assertEqual(len(self.server.requests), 1)

    def test_empty_history_differs_from_denied_and_unknown_range(self):
        self.respond({}, path="/api/history?range=30m", status=401)
        self.assertIsNone(self.source.history("30m"))
        self.respond({"range": "7d", "from": AT - 604800, "to": AT, "step": 1800,
                      "series": [], "count": 0}, path="/api/history?range=7d")
        empty = self.source.history("7d")
        self.assertEqual((empty["count"], empty["series"]), (0, []))
        for value in ("31d", "../../", "30m&another=1", []):
            with self.assertRaises(ValueError):
                self.source.history(value)
        self.assertEqual([path for path, _ in self.server.requests],
                         ["/api/history?range=30m", "/api/history?range=7d"])

    def test_slow_drip_cannot_extend_read_deadline(self):
        def drip(handler):
            try:
                for byte in b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{}":
                    handler.wfile.write(bytes([byte]))
                    handler.wfile.flush()
                    time.sleep(0.035)
            except OSError:
                pass

        self.respond(drip)
        with patch.object(self.atlas, "REQUEST_TIMEOUT", .25):
            started = time.monotonic()
            self.assertEqual(self.source.snapshot(now=AT)["status"], "offline")
            self.assertLess(time.monotonic() - started, 1)

    def test_valid_chunked_json_is_projected(self):
        def chunks(handler):
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json; charset=utf-8")
            handler.send_header("Transfer-Encoding", "chunked")
            handler.end_headers()
            body = json.dumps(packet()).encode("utf-8")
            for part in (body[:20], body[20:]):
                handler.wfile.write(f"{len(part):x}\r\n".encode("ascii") + part + b"\r\n")
            handler.wfile.write(b"0\r\n\r\n")

        self.respond(chunks)
        self.assertEqual(self.source.snapshot(now=AT)["gpu_counts"], {"active": 1, "total": 2})


    def test_config_can_select_only_the_fixed_adjacent_feed(self):
        with tempfile.TemporaryDirectory() as root, patch.object(sys, "platform", "linux"):
            config = Path(root)
            file = config / "atlas-source.json"
            file.write_text('{"adjacent":true}', encoding="utf-8")
            self.assertIsNotNone(self.atlas.AtlasSource.load(config))
            for value in ('{"adjacent":false}', '{"adjacent":1}', '{"adjacent":true,"port":18761}',
                          '{"socket":"/run/some-other.sock"}'):
                file.write_text(value, encoding="utf-8")
                with self.subTest(value=value), self.assertRaises(ValueError):
                    self.atlas.AtlasSource.load(config)
        self.assertEqual(self.server.requests, [])


if __name__ == "__main__":
    unittest.main()
