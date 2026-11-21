"""Read the public DevFee pause flag from https://neb.apollot.ru/devfee/policy."""

import json
from contextlib import closing
from http.client import HTTPException, HTTPSConnection
from threading import Lock
from time import monotonic

_INTERVAL = 60
_MAX_AGE = 120
_TIMEOUT = 4
_MAX_BODY = 1024


def _parse(body):
    # Preserve the object pairs so a duplicate key cannot override a pause.
    fields = json.loads(body, object_pairs_hook=tuple)
    if not isinstance(fields, tuple) or len(fields) != 1:
        raise ValueError("Expected a single DevFee pause flag.")
    key, paused = fields[0]
    if key != "paused" or type(paused) is not bool:
        raise ValueError("Expected a boolean DevFee pause flag.")
    return "paused" if paused else "enabled"


def _fetch():
    deadline = monotonic() + _TIMEOUT
    with closing(HTTPSConnection("neb.apollot.ru", timeout=_TIMEOUT)) as conn:
        conn.request("GET", "/devfee/policy", headers={"Cache-Control": "no-cache"})
        with conn.getresponse() as response:
            if response.status != 200:
                raise ValueError("Unexpected DevFee policy response.")
            body = bytearray()
            while monotonic() < deadline:
                chunk = response.read1(_MAX_BODY + 1 - len(body))
                if monotonic() >= deadline:
                    break
                if not chunk:
                    return _parse(body)
                body.extend(chunk)
                if len(body) > _MAX_BODY:
                    raise ValueError("DevFee policy exceeded the body limit.")
    raise TimeoutError("DevFee policy request timed out.")


class DevFeePolicy:
    """Expose a cached state to the supervisor while a separate thread polls."""

    def __init__(self):
        self._lock = Lock()
        self._status = "unavailable"
        self._checked = 0.0

    def state(self):
        with self._lock:
            if monotonic() - self._checked >= _MAX_AGE:
                return "unavailable"
            return self._status

    def loop(self, stopping):
        while not stopping.is_set():
            try:
                status = _fetch()
            except (OSError, HTTPException, ValueError, RecursionError):
                status = "unavailable"
            with self._lock:
                self._status = status
                self._checked = monotonic()
            if stopping.wait(_INTERVAL):
                return
