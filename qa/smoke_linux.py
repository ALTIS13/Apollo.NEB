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
    payouts = directory / "payouts.jsonl"
    fixture.write_text("#!/usr/bin/env python3\nimport json, sys, time\n"
                       f"with open({str(payouts)!r}, 'a', encoding='utf-8') as receipt:\n"
                       "    receipt.write(json.dumps(sys.argv[1:]) + '\\n')\n"
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
    shared = directory / "existing-shared"
    shared.mkdir()
    shared.chmod(0o755)
    refused = subprocess.run(["python3", "-I", str(SOURCE / "neb.py"), "--config-dir", str(config),
                              "--state-dir", str(shared), "configure", "--password-stdin"],
                             input=password + "\n", text=True, capture_output=True, timeout=5)
    assert refused.returncode != 0 and shared.stat().st_mode & 0o777 == 0o755
    assert not (shared / "service.lock").exists() and not config.exists()
    installed = subprocess.run(["/bin/sh", str(SOURCE / "install.sh"), "--prefix", str(prefix),
                    "--config-dir", str(config), "--state-dir", str(state), "--",
                    "--wallet", SYNTHETIC_NEVER_PAID, "--workers", str(workers),
                    "--pools", str(test_pool), "--pool", "offline", "--port", str(port), "--password-stdin"],
                   input=password + "\n", text=True, check=True, timeout=12, capture_output=True)
    launcher = prefix / "bin" / "apollo-neb"
    assert (config / "login.json").stat().st_mode & 0o077 == 0
    args = ["--config-dir", str(config), "--state-dir", str(state)]
    recipient = json.loads((SOURCE / "developer-support.json").read_text(encoding="utf-8"))["wallet"]
    expected = {"enabled": True, "basis_points": 50, "wallet": recipient}
    service = subprocess.Popen([str(launcher), *args, "serve"])
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
            assert all(status["support"][k] == v for k,v in expected.items()), "Fresh install must run with the release DevFee policy."
            assert status["support"]["state"] == "online"
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
    assert installed.stdout.index("0.5%") < installed.stdout.index("Preparing the administrator account")
    assert subprocess.run([str(launcher), *args, "configure", "--disable-support"], text=True,
                          timeout=5, capture_output=True).returncode == 2
    # Reopen as an upgrade with the former settings, then exercise an available
    # balance without waiting 16 hours. Only the test process shortens the window.
    settings = config / "settings.json"
    settings.write_text(json.dumps({**json.loads(settings.read_text()), "support": {"enabled": False}}))
    window = 6
    (state / "support-time.json").write_text(json.dumps({"config": expected, "credit": {"fixture": window}}))
    installed_code = prefix / "lib" / "apollo-neb" / "current"
    expedite = (
        "import sys\nsys.path.insert(0,sys.argv.pop(1))\nimport neb,neb_core\n"
        "original=neb_core.SupportClock\n"
        "window=int(sys.argv.pop(1))\n"
        "neb_core.SupportClock=lambda config,path: original(config,path,window=window)\n"
        "raise SystemExit(neb.main())\n"
    )
    # Clear the previous smoke phase's receipt; its confirmed stop is above.
    payouts.write_text("")
    service = subprocess.Popen(["python3", "-I", "-c", expedite, str(installed_code), str(window), *args, "serve"])
    try:
        assert eventually(lambda: request(port, "GET", "/")[0] == 200)
        code, _, raw_cookie = request(port, "POST", "/api/login", {"password": password})
        assert code == 200
        cookie = raw_cookie.split(";", 1)[0]

        def launches():
            return [json.loads(line) for line in payouts.read_text().splitlines() if line]

        assert get_status()["state"] == "stopped" and not launches()
        assert request(port, "POST", "/api/start", {"id":"fixture"}, cookie)[0] == 200
        eventually(lambda: len(launches()) >= 2)
        support = json.loads(request(port, "GET", "/api/status", cookie=cookie)[1])["support"]
        assert support["active_workers"] == ["fixture"]
        eventually(lambda: len(launches()) >= 3, seconds=window+8)
        assert launches() == [[address,"stratum+tcp://127.0.0.1:1"]
                              for address in (SYNTHETIC_NEVER_PAID, recipient, SYNTHETIC_NEVER_PAID)]
        assert not json.loads(request(port, "GET", "/api/status", cookie=cookie)[1])["support"]["active_workers"]
        assert request(port, "POST", "/api/stop", {"id":"fixture"}, cookie)[0] == 200
    finally:
        service.terminate()
        service.wait(timeout=10)
    print("PASS: rootless install, authenticated offline miner, default DevFee, old settings ignored, both wallet switches.")


if __name__ == "__main__":
    assert os.geteuid() != 0, "Use a regular Linux account."
    with tempfile.TemporaryDirectory(prefix="apollo-neb-install-qa-") as directory:
        test(Path(directory))
