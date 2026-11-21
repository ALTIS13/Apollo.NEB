"""Public NEB Mainnet statistics from https://neb.suprnova.cc/API.html."""

import json
from contextlib import closing
from dataclasses import dataclass
from http.client import HTTPException, HTTPSConnection
from math import ceil, isfinite
from threading import Lock
from time import monotonic, time

_RETRY_SECONDS = 60
_FRESH_SECONDS = 300
_TIMEOUT = 4
_MAX_BODY = 65536


def _number(value, *, allow_zero=False, allow_string=False):
    if type(value) not in (int, float) and not (allow_string and isinstance(value, str)):
        raise ValueError("Expected a number in NEB network statistics.")
    number = float(value)
    if not isfinite(number) or number < 0 or (number == 0 and not allow_zero):
        raise ValueError("Invalid number in NEB network statistics.")
    return number


def _parse(body):
    try:
        pool = json.loads(body)["pool"]
        if pool["id"] != "neb" or pool["coin"]["symbol"] != "NEB" or pool["coin"]["type"] != "NEB":
            raise ValueError("Unexpected coin or pool in NEB network statistics.")
        network = pool["networkStats"]
        height = network["blockHeight"]
        if type(height) is not int or height < 0:
            raise ValueError("Invalid NEB block height.")
        return {
            "height": height,
            "difficulty": _number(network["networkDifficulty"], allow_string=True),
            "hashrate_hs": _number(network["networkHashrate"]),
            "reward_neb": _number(pool["blockReward"], allow_zero=True),
            "target_seconds": _number(pool["blockTimeConfigured"]),
            "observed_at": int(time()),
        }
    except (KeyError, TypeError, OverflowError, RecursionError) as error:
        raise ValueError("Malformed NEB network statistics.") from error


def _fetch():
    deadline = monotonic() + _TIMEOUT
    with closing(HTTPSConnection("neb.suprnova.cc", timeout=_TIMEOUT)) as conn:
        conn.request("GET", "/api/pools/neb", headers={"Accept": "application/json"})
        with conn.getresponse() as response:
            content_type = response.getheader("Content-Type", "").partition(";")[0].strip().lower()
            if response.status != 200 or content_type != "application/json":
                raise ValueError("Unexpected response from Suprnova.")
            body = bytearray()
            while monotonic() < deadline:
                chunk = response.read1(min(8192, _MAX_BODY + 1 - len(body)))
                if monotonic() >= deadline:
                    break
                if not chunk:
                    return _parse(body)
                body.extend(chunk)
                if len(body) > _MAX_BODY:
                    raise ValueError("Suprnova response exceeded the body limit.")
    raise TimeoutError("Suprnova response timed out.")


@dataclass
class _Cache:
    started: float | None = None
    updated: float = 0
    data: dict | None = None
    loading: bool = False
    failed: bool = False

    def retry_after(self, now):
        return 0 if self.started is None else max(0, ceil(self.started + _RETRY_SECONDS - now))

    def status(self, now):
        if self.loading:
            state = "loading"
        elif self.data is not None:
            state = "stale" if self.failed or now - self.updated >= _FRESH_SECONDS else "fresh"
        else:
            state = "unavailable" if self.failed else "unchecked"
        return {"state": state, "data": self.data.copy() if self.data is not None else None,
                "retry_after": self.retry_after(now)}


class Network:
    """A single, on-demand cache for all public-network lookups in this process."""

    _lock = Lock()
    _cache = _Cache()

    @classmethod
    def status(cls):
        with cls._lock:
            return cls._cache.status(monotonic())

    @classmethod
    def refresh(cls):
        with cls._lock:
            now = monotonic()
            cache = cls._cache
            if cache.loading or cache.retry_after(now):
                return cache.status(now)
            cache.started = now
            cache.loading = True

        data = None
        try:
            data = _fetch()
        except (OSError, HTTPException, ValueError):
            pass
        finally:
            with cls._lock:
                now = monotonic()
                cache.loading = False
                cache.failed = data is None
                if data is not None:
                    cache.data = data
                    cache.updated = now
                result = cache.status(now)
        return result
