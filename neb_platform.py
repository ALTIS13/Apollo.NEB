"""Opt-in publisher for one Portable installation's aggregate status."""
import base64
from contextlib import closing
from http.client import HTTPException, HTTPSConnection
import json
import logging
import os
import re
import ssl
import stat
import sys
import time


HOST = "api.apollot.ru"
ORIGIN = f"https://{HOST}"
PATH = "/v1/internal/neb/farm-samples"
INTERVAL = 30
RETRY_DELAY = 2
SOCKET_TIMEOUT = 3
MAX_SAFE_INTEGER = 2**53 - 1
MAX_SAMPLE_TIME = 253402300799
TOKEN_FORMAT = re.compile(rb"[A-Za-z0-9_-]{43}\n?")


def read_regular(path, size, *, private=False):
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("Expected regular file.")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags), "rb") as file:
        details = os.fstat(file.fileno())
        if not stat.S_ISREG(details.st_mode) or not os.path.samestat(before, details):
            raise ValueError("Expected regular file.")
        if private and (stat.S_IMODE(details.st_mode) != 0o600
                        or details.st_uid != os.geteuid() or details.st_nlink != 1):
            raise ValueError("Expected a private file owned by the service user.")
        return file.read(size)


def unique_fields(pairs):
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError("Duplicate publisher setting.")
    return fields


def configured_token(config):
    try:
        contents = read_regular(config / "platform-export.json", 513)
    except FileNotFoundError:
        return None
    if len(contents) > 512:
        raise ValueError("Invalid Platform publisher settings.")
    data = json.loads(contents, object_pairs_hook=unique_fields)
    if (not isinstance(data, dict) or type(data.get("enabled")) is not bool
            or data.keys() - {"enabled", "origin"}):
        raise ValueError("Invalid Platform publisher settings.")
    if not data["enabled"]:
        return None
    if data.get("origin") != ORIGIN:
        raise ValueError("Invalid Platform publisher origin.")
    return config / "platform-export.token"


def load_token(path):
    contents = read_regular(path, 45, private=True)
    if not TOKEN_FORMAT.fullmatch(contents):
        raise ValueError("Invalid Platform token.")
    encoded = contents.removesuffix(b"\n")
    raw = base64.urlsafe_b64decode(encoded + b"=")
    if len(raw) != 32 or base64.urlsafe_b64encode(raw).rstrip(b"=") != encoded:
        raise ValueError("Invalid Platform token.")
    return encoded.decode("ascii")


def count(number, limit=MAX_SAFE_INTEGER):
    if type(number) is not int or not 0 <= number <= limit:
        raise ValueError("Invalid telemetry count.")
    return number


def measurement(number, lower=0, upper=sys.float_info.max):
    if number is not None and (type(number) not in (int, float) or not lower <= number <= upper):
        raise ValueError("Invalid telemetry measurement.")
    return number


def project(status):
    now = int(time.time())
    stamp = count(status["time"], min(MAX_SAMPLE_TIME, now + 5))
    if not stamp or stamp < now - 120:
        raise ValueError("No current local sample.")
    workers = {key: count(status["workers"][key]) for key in ("total", "running", "attention")}
    if workers["running"] + workers["attention"] > workers["total"]:
        raise ValueError("Inconsistent worker counts.")
    shares = {key: status["shares"][key] for key in ("accepted", "rejected")}
    return {
        "time": stamp,
        "workers": workers,
        "hashrate_hs": measurement(status["hashrate_hs"]),
        "watts": measurement(status["watts"]),
        "temperatures": {key: measurement(status["temperatures"][key], -100, 300)
                         for key in ("edge", "hot", "hbm")},
        "shares": {key: None if number is None else count(number) for key, number in shares.items()},
    }


def post(context, token, body):
    try:
        with closing(HTTPSConnection(HOST, context=context, timeout=SOCKET_TIMEOUT)) as connection:
            connection.request("POST", PATH, body=body, headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Connection": "close",
            })
            with connection.getresponse() as response:
                status = response.status
    except ssl.SSLError:
        return "tls-rejected"
    except (OSError, HTTPException):
        return "unavailable"
    # http.client treats redirect responses exactly like any other non-204.
    return {204: "sent", 503: "unavailable"}.get(status, "rejected")


def publish_once(source, token_path, stopping):
    if stopping.is_set():
        return "stopped"
    try:
        sample = project(source())
    except (KeyError, TypeError, ValueError):
        return "sample-unavailable"
    try:
        token = load_token(token_path)
    except (OSError, ValueError):
        return "token-unavailable"
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_default_certs()
    except (OSError, ValueError):
        return "tls-rejected"
    if stopping.is_set():
        return "stopped"
    body = json.dumps({"schema_version": 1, "sample": sample}, allow_nan=False, separators=(",", ":")).encode("ascii")
    result = post(context, token, body)
    if result == "unavailable":
        return "stopped" if stopping.wait(RETRY_DELAY) else post(context, token, body)
    return result


def run(source, token_path, stopping):
    previous = None
    receipts_left = 2
    while not stopping.is_set():
        start = time.monotonic()
        result = publish_once(source, token_path, stopping)
        if result == "stopped":
            break
        if result == "sent" and receipts_left:
            print("Apollo Platform publisher received HTTP 204", flush=True)
            receipts_left -= 1
        elif result not in ("sent", previous):
            logging.warning("Apollo Platform publisher: %s", result)
        previous = result
        stopping.wait(max(0, INTERVAL - (time.monotonic() - start)))
