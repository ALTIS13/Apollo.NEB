import base64
from contextlib import closing
import http.client
import json
import secrets
import tempfile
import threading
import time
import unittest
from pathlib import Path

import neb_app as app
import neb_core as core
from neb_devfee_owner import OwnerPolicy


class OwnerPolicyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / "devfee-policy.json"
        self.owner = OwnerPolicy(self.path)

    def test_default_pause_persisted_writes_and_fresh_reads(self):
        self.assertEqual(self.owner.status(), {"paused": True})
        self.assertFalse(self.path.exists())
        self.assertEqual(self.owner.set_paused(False), {"paused": False})
        self.assertEqual(OwnerPolicy(self.path).status(), {"paused": False})
        with self.assertRaises(ValueError):
            self.owner.set_paused(1)
        self.assertEqual(self.owner.status(), {"paused": False})
        core.store(self.path, {"paused": True})
        self.assertEqual(self.owner.status(), {"paused": True})

    def test_corruption_never_resumes_or_replaces_the_saved_policy(self):
        for value in (b"null", b'{"paused":1}', b'{"paused":true,"paused":false}',
                      b'{"paused":false,"extra":true}', b"{", b"\xff"):
            with self.subTest(value=value):
                self.path.write_bytes(value)
                with self.assertRaises(OSError):
                    self.owner.status()
                with self.assertRaises(OSError):
                    self.owner.set_paused(False)
                self.assertEqual(self.path.read_bytes(), value)

    def test_only_authenticated_owner_can_change_policy(self):
        password = "synthetic devfee fixture"
        secret = base64.b32encode(secrets.token_bytes(20)).decode("ascii")
        auth = core.Auth(core.new_login(password, secret), self.root / "owner-auth.sqlite")
        public = "https://neb.fixture.invalid"
        with app.Server(("127.0.0.1", 0), None, auth, app.HERE / "web", public, owner=self.owner) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with closing(http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)) as conn:
                    def request(method, path, body=None, *, cookie=None, origin=public):
                        headers = {"Host": "neb.fixture.invalid", "Origin": origin}
                        if body is not None:
                            headers["Content-Type"] = "application/json"
                        if cookie:
                            headers["Cookie"] = cookie
                        conn.request(method, path, None if body is None else json.dumps(body), headers)
                        response = conn.getresponse()
                        return response.status, json.loads(response.read()), response.getheader("Set-Cookie")

                    self.assertEqual(request("GET", "/policy?paused=false")[:2], (200, {"paused": True}))
                    self.assertEqual(request("GET", "/api/auth/info")[:2], (200, {"totp": True, "enrollment": False}))
                    self.assertEqual(request("GET", "/api/devfee")[0], 401)
                    self.assertEqual(request("POST", "/api/devfee", {"paused": False})[0], 401)
                    self.assertEqual(request("POST", "/api/login", {"password": password})[0], 401)
                    code, _, header = request("POST", "/api/login",
                                              {"password": password, "code": core.totp(secret, int(time.time()))})
                    self.assertEqual(code, 200)
                    self.assertTrue(header.startswith("neb_devfee_sid="))
                    for attr in ("Path=/devfee/", "HttpOnly", "SameSite=Strict", "Secure"):
                        self.assertIn(attr, header)
                    cookie = header.split(";", 1)[0]
                    self.assertEqual(request("POST", "/api/devfee", {"paused": False}, cookie=cookie,
                                             origin="https://foreign.fixture.invalid")[0], 403)
                    self.assertEqual(request("GET", "/policy")[:2], (200, {"paused": True}))
                    self.assertEqual(request("POST", "/api/devfee", {"paused": False}, cookie=cookie)[:2],
                                     (200, {"paused": False}))
                    self.assertEqual(request("GET", "/policy")[:2], (200, {"paused": False}))
                    self.assertEqual(request("GET", "/api/status", cookie=cookie)[0], 404)
                    self.assertEqual(request("POST", "/api/logout", {}, cookie=cookie)[0], 200)
                    self.assertEqual(request("GET", "/api/devfee", cookie=cookie)[0], 401)
            finally:
                server.shutdown()
                thread.join(timeout=2)

    def test_first_web_enrollment_is_loopback_only_and_does_not_publish_policy(self):
        password = "synthetic enrollment fixture"
        auth = core.Auth(core.new_login(password), self.root / "owner-auth.sqlite")
        public = "https://neb.fixture.invalid"
        with self.assertRaises(ValueError):
            app.Server(("127.0.0.1", 0), None, auth, app.HERE / "web", public, owner=self.owner)
        with self.assertRaises(ValueError):
            app.Server(("127.0.0.1", 0), None, auth, app.HERE / "web", public, owner=self.owner,
                       owner_enrollment=True)
        with app.Server(("127.0.0.1", 0), None, auth, app.HERE / "web", None, owner=self.owner,
                        owner_enrollment=True) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host = f"127.0.0.1:{server.server_port}"
                with closing(http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)) as conn:
                    def request(method, path, body=None, *, cookie=None):
                        headers = {"Host": host, "Origin": "http://" + host, "Content-Type": "application/json"}
                        if cookie:
                            headers["Cookie"] = cookie
                        conn.request(method, path, None if body is None else json.dumps(body), headers)
                        response = conn.getresponse()
                        return response.status, json.loads(response.read()), response.getheader("Set-Cookie")

                    self.assertEqual(request("GET", "/api/auth/info")[1], {"totp": False, "enrollment": True})
                    self.assertEqual(request("GET", "/policy")[0], 404)
                    code, _, header = request("POST", "/api/login", {"password": password})
                    self.assertEqual(code, 200)
                    cookie = header.split(";", 1)[0]
                    self.assertEqual(request("POST", "/api/devfee", {"paused": False}, cookie=cookie)[0], 404)
                    code, enrollment, _ = request("POST", "/api/auth/totp/begin", {"password": password},
                                                  cookie=cookie)
                    self.assertEqual(code, 200)
                    otp = core.totp(enrollment["secret"], int(time.time()))
                    code, result, _ = request("POST", "/api/auth/totp/confirm", {"code": otp}, cookie=cookie)
                    self.assertEqual(code, 200)
                    self.assertTrue(result["recovery_codes"])
                    self.assertTrue(auth.is_totp_enabled())
                    self.assertEqual(request("GET", "/policy")[0], 404)
            finally:
                server.shutdown()
                thread.join(timeout=2)
