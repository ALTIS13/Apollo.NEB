"""Run the extracted Windows controller against a disposable fleet fixture."""

import argparse
from contextlib import closing
import http.client
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from neb_core import totp
from neb_fleet import Client


def invoke(launcher, *args):
    if launcher.suffix.lower() == ".cmd":
        return ["cmd.exe", "/d", "/c", "call", str(launcher), *args]
    return [sys.executable, "-X", "utf8", "-I", str(launcher), *args]


def request(port, method, route, payload=None, cookie=None):
    host = f"127.0.0.1:{port}"
    headers = {"Origin": "http://" + host}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if cookie:
        headers["Cookie"] = cookie
    with closing(http.client.HTTPConnection(host, timeout=3)) as connection:
        connection.request(method, route, json.dumps(payload) if payload is not None else None, headers)
        with connection.getresponse() as response:
            return response.status, json.loads(response.read()), response.getheader("Set-Cookie")


def eventually(ready, process):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline and process.poll() is None:
        try:
            if ready():
                return
        except OSError:
            pass
        time.sleep(0.2)
    raise AssertionError("Windows controller did not reach its loopback API.")


def exercise(launcher, directory):
    cfg, state = directory / "config", directory / "state"
    args = ("--config-dir", str(cfg), "--state-dir", str(state))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    password = secrets.token_urlsafe(24)
    setup = subprocess.run(invoke(launcher, *args, "configure", "--port", str(port), "--totp", "--password-stdin"),
                           input=password + "\n", capture_output=True, text=True, encoding="utf-8",
                           timeout=20)
    if setup.returncode:
        raise AssertionError(f"Windows controller configure failed ({setup.returncode}). {setup.stderr.strip()}")
    key = json.loads((cfg / "login.json").read_text(encoding="utf-8"))["totp"]
    # A manually planted Linux worker file cannot enable local execution.
    (cfg / "workers.json").write_text('[{"id":"qa-worker","binary":"/qa/unavailable"}]', encoding="utf-8")
    with open(directory / "controller.log", "wb") as log:
        service = subprocess.Popen(invoke(launcher, *args, "serve"), stdout=log, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        try:
            eventually(lambda: request(port, "GET", "/api/status")[0] == 401, service)
            assert request(port, "POST", "/api/login", {"password": password})[0] == 401
            code, _, cookie_header = request(port, "POST", "/api/login",
                                             {"password": password, "code": totp(key, int(time.time()))})
            assert code == 200 and "HttpOnly; SameSite=Strict" in cookie_header
            cookie = cookie_header.split(";", 1)[0]
            status = request(port, "GET", "/api/status", cookie=cookie)[1]
            assert status["capabilities"] == {"local_workers": False, "wallet_tools": False}
            assert status["workers"] == []
            assert request(port, "GET", "/api/setup", cookie=cookie)[0] == 404
            assert request(port, "POST", "/api/start", {"id": "qa-worker"}, cookie)[0] == 409
            assert request(port, "POST", "/api/fleet/enroll", {"label": "QA node"})[0] == 401
            code, enroll, _ = request(port, "POST", "/api/fleet/enroll", {"label": "QA node"}, cookie)
            assert code == 200
            follower = Client(f"http://127.0.0.1:{port}", enroll["id"], secrets.token_urlsafe(32),
                              directory / "fixture-client.sqlite", allow_loopback=True)
            assert follower.join(enroll["code"]) == {"ok": True}
            worker = {"id": "fixture", "state": "stopped"}
            received = []

            def execute(action, workers):
                received.append((action, workers))
                worker["state"] = "running" if action == "start" else "stopped"

            follower.cycle([worker], execute)
            for action in ("start", "stop"):
                code, queued, _ = request(port, "POST", "/api/fleet/command",
                                          {"id": enroll["id"], "action": action, "ids": ["fixture"]}, cookie)
                assert code == 200 and queued["status"] == "queued"
                follower.cycle([worker], execute)
                follower.cycle([worker], execute)
                _, data, _ = request(port, "GET", "/api/fleet", cookie=cookie)
                node, = data["nodes"]
                assert node["status"] == "online" and node["workers"] == [worker]
                assert next(command for command in node["commands"] if command["id"] == queued["id"]
                            )["status"] == "acknowledged"
            assert received == [("start", ["fixture"]), ("stop", ["fixture"])]
            assert not request(port, "GET", "/api/status", cookie=cookie)[1]["workers"]
        finally:
            if service.poll() is None:
                service.send_signal(signal.CTRL_BREAK_EVENT)
            try:
                service.wait(timeout=8)
            except subprocess.TimeoutExpired:
                service.kill()
                service.wait(timeout=8)
    print("PASS: native Windows controller, password/TOTP, fleet enrollment, fixture start/stop and no local worker.")


if __name__ == "__main__":
    if os.name != "nt":
        raise SystemExit("Run on native Windows.")
    parser = argparse.ArgumentParser()
    parser.add_argument("launcher", type=Path)
    parser.add_argument("--work-dir", required=True, type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="Apollo NEB controller QA ", dir=args.work_dir) as temporary:
        exercise(args.launcher.resolve(strict=True), Path(temporary))
