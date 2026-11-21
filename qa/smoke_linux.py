"""Exercise a clean rootless installation and an offline test worker."""
import http.client
import json
import os
import secrets
import socket
import subprocess
import tempfile
import time
from contextlib import closing
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
SYNTHETIC_NEVER_PAID = "neb1pqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq3hy0ga"


def request(port, method, path, payload=None, cookie=None):
    address = f"127.0.0.1:{port}"
    headers = {"Host": address, "Origin": "http://" + address}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = cookie
    with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=3)) as client:
        client.request(method, path, json.dumps(payload) if payload is not None else None, headers=headers)
        response = client.getresponse()
        return response.status, response.read(), response.getheader("Set-Cookie")


def eventually(get, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            result = get()
            if result:
                return result
        except (OSError, http.client.HTTPException):
            pass
        time.sleep(0.2)
    raise RuntimeError("Timed out waiting for the isolated dashboard.")


def test(directory):
    prefix, config, state = (directory / k for k in ("prefix", "cfg", "state"))
    fixture = directory / "fixture.py"
    fixture.write_text("#!/usr/bin/env python3\nimport json, time\n"
                       "while True:\n"
                       "    print(json.dumps({'rate_hs': 1250000, 'accepted': 7, 'rejected': 0}), flush=True)\n"
                       "    time.sleep(0.2)\n", encoding="utf-8")
    fixture.chmod(0o700)
    workers = directory / "test-workers.json"
    workers.write_text(json.dumps([{"id":"fixture","adapter":"custom","binary":str(fixture),
                                   "log_format":"json-lines","args":["{wallet}", "{pool}"]}]), encoding="utf-8")
    test_pool = directory / "test-pools.json"
    test_pool.write_text(json.dumps([{"id":"offline","label":"Offline fixture",
                                     "url":"stratum+tcp://127.0.0.1:1"}]), encoding="utf-8")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    password = secrets.token_urlsafe(24)
    subprocess.run(["/bin/sh", str(SOURCE / "install.sh"), "--prefix", str(prefix),
                    "--config-dir", str(config), "--state-dir", str(state), "--",
                    "--wallet", SYNTHETIC_NEVER_PAID, "--workers", str(workers),
                    "--pools", str(test_pool), "--pool", "offline", "--port", str(port), "--password-stdin"],
                   input=password + "\n", text=True, check=True, timeout=12)
    launcher = prefix / "bin" / "apollo-neb"
    assert (config / "login.json").stat().st_mode & 0o077 == 0
    service = subprocess.Popen([str(launcher), "--config-dir", str(config), "--state-dir", str(state), "serve"])
    try:
        assert eventually(lambda: request(port, "GET", "/")[0] == 200)
        assert request(port, "GET", "/api/status")[0] == 401
        code, _, raw_cookie = request(port, "POST", "/api/login", {"password": password})
        assert code == 200 and "HttpOnly; SameSite=Strict" in raw_cookie
        cookie = raw_cookie.split(";", 1)[0]

        def get_status():
            code, data, _ = request(port, "GET", "/api/status", cookie=cookie)
            assert code == 200
            status = json.loads(data)
            assert status["profile"]["wallet"] == SYNTHETIC_NEVER_PAID
            assert status["support"]["enabled"] is False
            return status["workers"][0]

        assert get_status()["state"] == "stopped"
        assert request(port, "POST", "/api/start", {"id":"outside"}, cookie)[0] == 409
        assert request(port, "POST", "/api/start", {"id":"fixture"}, cookie)[0] == 200
        assert eventually(lambda: get_status()["rate_hs"] == 1250000)
        assert request(port, "POST", "/api/stop", {"id":"fixture"}, cookie)[0] == 200
        assert get_status()["state"] == "stopped"
        assert get_status()["rate_hs"] is None
        code, data, _ = request(port, "GET", "/api/events", cookie=cookie)
        assert code == 200 and {"start", "stop"} <= {item["type"] for item in json.loads(data)["events"]}
        assert request(port, "POST", "/api/logout", {}, cookie)[0] == 200
        assert request(port, "GET", "/api/status", cookie=cookie)[0] == 401
    finally:
        service.terminate()
        service.wait(timeout=10)
    print("PASS: fresh Linux install, auth, isolated offline worker, telemetry, start/stop and logout.")


if __name__ == "__main__":
    assert os.geteuid() != 0, "Use a regular Linux account."
    with tempfile.TemporaryDirectory(prefix="apollo-neb-install-qa-") as directory:
        test(Path(directory))
