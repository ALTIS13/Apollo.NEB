"""Read-only access to an explicitly configured Atlas source."""

from contextlib import closing
from copy import deepcopy
import hashlib
from http.client import HTTPConnection, HTTPException, HTTPResponse
from io import BufferedReader, RawIOBase
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import struct
import sys
import threading
import time


HOST = "127.0.0.1"
FEED_SOCKET = Path("/run/atlas-neb-read/feed.sock")
FEED_PROTOCOL = 1
TLS_READ_PREFIX = "/_neb/atlas/read"
TLS_NETWORKS = tuple(ipaddress.IPv4Network(network) for network in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "127.0.0.0/8",
))
DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
PEER_CREDENTIALS = struct.Struct("iII")
REQUEST_TIMEOUT = 1.5
SNAPSHOT_TTL = 5
HISTORY_TTL = 30
FRESH_SECONDS = 30
CONFIG_LIMIT = 256
MAX_BODY = 512 * 1024
MAX_WIRE = MAX_BODY + 64 * 1024
MAX_TIME = 253402300799
MAX_COUNT = 2**53 - 1
MAX_GPU = 64
MAX_RATE = 1e20
MAX_WATTS = 3000
MAX_SERIES = 2048
MAX_INCIDENTS = 150
TEMPERATURES = ("edge", "hot", "hbm")
SHARES = ("accepted", "rejected")
PCI = re.compile(r"(?:[0-9a-f]{4}:)?[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]", re.IGNORECASE)
GPU_STATES = {"mining", "starting", "retrying", "stopping", "failed", "idle", "busy", "unavailable"}
RANGES = {"30m": 1800, "6h": 21600, "24h": 86400, "7d": 604800, "30d": 2592000}
SERIES_LIMITS = {"rate": MAX_RATE / 1e6, "watts": MAX_GPU * MAX_WATTS, "active": MAX_GPU,
                 "edge": 160, "hot": 160, "hbm": 160, "efficiency": MAX_RATE / 1e6}
THERMAL_REASONS = {
    "edge-hot": "Перегрев ядра",
    "hotspot-hot": "Перегрев Hotspot",
    "hbm-hot": "Перегрев HBM2",
}


def _settings(path):
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags), "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or not os.path.samestat(before, info):
            raise ValueError
        contents = file.read(CONFIG_LIMIT + 1)
    if len(contents) > CONFIG_LIMIT:
        raise ValueError
    return _json(contents)


def _unique(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError
    return result


def _reject_constant(_):
    raise ValueError


def _json(contents):
    return json.loads(contents.decode("utf-8"), object_pairs_hook=_unique, parse_constant=_reject_constant)


class _TimedInput(RawIOBase):
    def __init__(self, peer, deadline):
        self.peer = peer
        self.deadline = deadline
        self.read_bytes = 0
        self.stream = peer.makefile("rb", buffering=0)

    def makefile(self, _mode):
        # HTTPResponse needs a buffered binary reader. The raw reads share one deadline.
        return BufferedReader(self)

    def readable(self):
        return True

    def readinto(self, buffer):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        self.peer.settimeout(remaining)
        size = self.stream.readinto(buffer)
        self.read_bytes += size
        if self.read_bytes > MAX_WIRE:
            raise ValueError
        return size

    def close(self):
        if not self.closed:
            try:
                self.stream.close()
            finally:
                super().close()


class _DirectHTTP(HTTPConnection):
    def __init__(self, port, *, address=HOST, name=HOST, context=None, timeout=None):
        timeout = REQUEST_TIMEOUT if timeout is None else timeout
        super().__init__(name, port=port, timeout=timeout)
        self.address = address
        self.context = context
        self.deadline = time.monotonic() + timeout

    def connect(self):
        # A numeric AF_INET address avoids resolver and proxy configuration.
        peer = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            peer.settimeout(self.timeout)
            peer.connect((self.address, self.port))
            if self.context is not None:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                peer.settimeout(remaining)
                peer = self.context.wrap_socket(peer, server_hostname=self.host)
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                peer.settimeout(remaining)
        except (OSError, ValueError):
            peer.close()
            raise
        self.sock = peer

    def response_class(self, peer, *args, **kwargs):
        return HTTPResponse(_TimedInput(peer, self.deadline), *args, **kwargs)


def _fetch(port, path):
    return _http_json(_DirectHTTP(port), path)


def _http_json(connection, path):
    with closing(connection):
        connection.request("GET", path, headers={"Accept": "application/json", "Connection": "close"})
        with connection.getresponse() as reply:
            media = reply.getheader("Content-Type", "").partition(";")[0].strip().lower()
            encoding = reply.getheader("Content-Encoding", "identity").strip().lower()
            transfer = reply.getheader("Transfer-Encoding", "").strip().lower()
            lengths = reply.headers.get_all("Content-Length", [])
            if (reply.status != 200 or media != "application/json" or encoding != "identity" or
                    transfer not in ("", "chunked") or (transfer and lengths)):
                raise ValueError
            expected = None
            if lengths:
                if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,6}", lengths[0]):
                    raise ValueError
                expected = int(lengths[0])
                if expected > MAX_BODY:
                    raise ValueError
            contents = reply.read(MAX_BODY + 1)
            if len(contents) > MAX_BODY or (expected is not None and len(contents) != expected):
                raise ValueError
            return _json(contents)


def _tls_connection(options, *, credentials=None, prefix="atlas-read", timeout=None):
    if (sys.platform != "linux" or not isinstance(options, dict)
            or options.keys() != {"address", "port", "server_name"}):
        raise ValueError("Канал Atlas по TLS требует настроек Linux.")
    address, port, name = (options[key] for key in ("address", "port", "server_name"))
    if (not isinstance(address, str) or type(port) is not int or not 1024 <= port <= 65535
            or not isinstance(name, str) or len(name) > 128
            or not all(DNS_LABEL.fullmatch(label) for label in name.split("."))
            or not re.search("[A-Za-z]", name)):
        raise ValueError("Недопустимый адрес защищённого источника Atlas.")
    address = ipaddress.IPv4Address(address)
    if not any(address in network for network in TLS_NETWORKS):
        raise ValueError("Защищённый источник Atlas требует частного адреса.")
    directory = credentials if credentials is not None else Path(os.environ.get("CREDENTIALS_DIRECTORY", ""))
    if not directory.is_absolute():
        raise ValueError("Отсутствуют учётные данные службы Atlas.")
    if credentials is not None:
        info = directory.lstat()
        if (not stat.S_ISDIR(info.st_mode) or directory.resolve() != directory
                or (os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o077))):
            raise ValueError("Каталог TLS Atlas должен принадлежать службе и иметь права 0700.")
    ca, certificate, key = (directory / filename for filename in (
        f"{prefix}-ca.pem", f"{prefix}-client.pem", f"{prefix}-client.key",
    ))
    modes = [path.lstat().st_mode for path in (ca, certificate, key)]
    if not all(stat.S_ISREG(mode) for mode in modes) or (os.name == "posix" and modes[-1] & 0o077):
        raise ValueError("Недопустимые файлы учётных данных Atlas.")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.hostname_checks_common_name = False
    context.load_verify_locations(cafile=os.fspath(ca))
    context.load_cert_chain(certfile=os.fspath(certificate), keyfile=os.fspath(key), password="")

    def connect():
        return _DirectHTTP(port, address=str(address), name=name, context=context, timeout=timeout)

    return connect


def _tls_reader(options, credentials=None):
    connect = _tls_connection(options, credentials=credentials)

    def read(path):
        frame = _http_json(connect(), TLS_READ_PREFIX + path.removeprefix("/api"))
        return _feed_projection(frame, path)

    return read


def _fetch_feed(path):
    directory = FEED_SOCKET.parent.lstat()
    entry = FEED_SOCKET.lstat()
    if (not stat.S_ISDIR(directory.st_mode) or directory.st_mode & 0o022
            or not stat.S_ISSOCK(entry.st_mode) or entry.st_mode & 0o007 or entry.st_uid != directory.st_uid):
        raise ValueError("Недопустимый сокет чтения Atlas.")
    deadline = time.monotonic() + REQUEST_TIMEOUT
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.settimeout(REQUEST_TIMEOUT)
        peer.connect(os.fspath(FEED_SOCKET))
        pid, uid, _gid = PEER_CREDENTIALS.unpack(
            peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, PEER_CREDENTIALS.size))
        if pid <= 0 or uid != entry.st_uid:
            raise ValueError("Сокет чтения не принадлежит службе Atlas.")
        peer.sendall((path + "\n").encode("ascii"))
        with BufferedReader(_TimedInput(peer, deadline)) as stream:
            body = stream.read(MAX_BODY + 1)
    if len(body) > MAX_BODY:
        raise ValueError("Снимок Atlas слишком большой.")
    return _feed_projection(_json(body), path)


def _feed_projection(frame, path):
    if (not isinstance(frame, dict) or frame.keys() != {"protocol", "projection"}
            or type(frame["protocol"]) is not int or frame["protocol"] != FEED_PROTOCOL
            or not isinstance(frame["projection"], dict)):
        raise ValueError("Неподдерживаемый формат снимка Atlas.")
    value = frame["projection"]
    if path == "/api/status":
        if value.keys() != _offline().keys() or value["status"] != "online" or not _integer(value["time"], MAX_TIME):
            raise ValueError("Некорректный снимок Atlas.")
    elif (value.keys() != {"range", "from", "to", "step", "count", "series", "incidents"}
          or value["range"] != path.removeprefix("/api/history?range=")):
        raise ValueError("Некорректный архив Atlas.")
    return value


def _integer(value, maximum=MAX_COUNT):
    return value if type(value) is int and 0 <= value <= maximum else None


def _metric(value, maximum):
    return value if type(value) in (int, float) and 0 <= value <= maximum else None


def _gpu_id(raw):
    return raw.lower() if isinstance(raw, str) and PCI.fullmatch(raw) else None


def matched_power_limit(row):
    current = _metric(row.get("cap"), MAX_WATTS)
    selected = _integer(row.get("target_watts"), MAX_WATTS)
    low = _metric(row.get("min_watts"), MAX_WATTS)
    high = _metric(row.get("max_watts"), MAX_WATTS)
    if None not in (current, selected, low, high) and current == selected and low <= current <= high:
        return selected
    return None


def _gpu_rows(rows, *, guarded_start=False):
    projected, seen, complete = [], set(), True
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or type(row.get("present")) is not bool:
            complete = False
            continue
        if not row["present"]:
            continue
        ident = _gpu_id(row.get("id"))
        if ident is None:
            complete = False
            continue
        address = "0000:" + ident if ident.count(":") == 1 else ident
        if address in seen:
            complete = False
            continue
        seen.add(address)
        state = row.get("state")
        limit = matched_power_limit(row) if guarded_start else None
        projected.append({
            "id": ident, "label": f"GPU {index}",
            "state": state if isinstance(state, str) and state in GPU_STATES else "unknown",
            "rate_hs": _metric(row.get("rate"), MAX_RATE),
            "watts": _metric(row.get("watts"), MAX_WATTS),
            **({"limit_watts": limit} if limit is not None else {}),
            **{key: _metric(row.get(key), 160) for key in TEMPERATURES},
            **{key: _integer(row.get(key)) for key in SHARES},
        })
    return projected, complete


def _snapshot(data):
    if (not isinstance(data, dict) or not (stamp := _integer(data.get("time"), MAX_TIME)) or
            not isinstance(data.get("gpus"), list) or len(data["gpus"]) > MAX_GPU):
        raise ValueError
    gpus, complete = _gpu_rows(data["gpus"], guarded_start=data.get("cap_preserving_start") is True)
    power = [row["watts"] for row in gpus]
    effective_states = [row["state"] for row in gpus]
    local = data["local"] if isinstance(data.get("local"), dict) else {}
    return {
        "status": "online", "time": stamp,
        "rate_hs": _metric(local.get("rate"), MAX_RATE),
        "watts": sum(power) if complete and power and None not in power else None,
        "temperatures": {key: max((row[key] for row in gpus if row[key] is not None), default=None)
                         for key in TEMPERATURES},
        "shares": {key: _integer(local.get(key)) for key in SHARES},
        "gpu_counts": {
            "active": effective_states.count("mining") if complete and "unknown" not in effective_states else None,
            "total": len(gpus) if complete else None,
        },
        "gpus": gpus,
    }


def _offline():
    return {
        "status": "offline", "time": None, "rate_hs": None, "watts": None,
        "temperatures": dict.fromkeys(TEMPERATURES), "shares": dict.fromkeys(SHARES),
        "gpu_counts": {"active": None, "total": None}, "gpus": [],
    }


def _incidents(rows, start, stop):
    if not isinstance(rows, list):
        return []
    incidents = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        stamp = _integer(row.get("time"), MAX_TIME)
        reasons = row.get("reasons")
        if stamp is None or not start <= stamp <= stop or not isinstance(reasons, list):
            continue
        detail = " · ".join(label for reason, label in THERMAL_REASONS.items() if reason in reasons)
        if not detail:
            continue
        worker = _gpu_id(row.get("gpu")) or ""
        status = "stopped" if row.get("status") == "stopped" else "unconfirmed"
        ident = _integer(row.get("id"))
        if ident is None:
            safe = f"{stamp}|{worker}|{status}|{detail}".encode("utf-8")
            ident = "atlas-" + hashlib.sha256(safe).hexdigest()[:16]
        incidents[ident] = {"id": ident, "time": stamp, "type": "thermal", "worker": worker,
                            "detail": detail, "status": status}
    return sorted(incidents.values(), key=lambda item: item["time"], reverse=True)[:MAX_INCIDENTS]


def _history(data, period):
    if not isinstance(data, dict) or data.get("range") != period:
        raise ValueError
    start, stop = (_integer(data.get(key), MAX_TIME) for key in ("from", "to"))
    step = _integer(data.get("step"), RANGES[period])
    if (start is None or stop is None or not step or not 0 < stop - start <= RANGES[period] + step or
            not isinstance(data.get("series"), list) or len(data["series"]) > MAX_SERIES):
        raise ValueError
    series = {}
    for row in data["series"]:
        stamp = _integer(row.get("t"), MAX_TIME) if isinstance(row, dict) else None
        if stamp is None or stamp in series:
            raise ValueError
        if start <= stamp <= stop:
            series[stamp] = {
                "t": stamp, "samples": _integer(row.get("samples")),
                **{key: _metric(row.get(key), limit) for key, limit in SERIES_LIMITS.items()},
            }
    return {
        "range": period, "from": start, "to": stop, "step": step, "count": _integer(data.get("count")),
        "series": [series[stamp] for stamp in sorted(series)],
        "incidents": _incidents(data.get("incidents"), start, stop),
    }


class AtlasSource:
    def __init__(self, port=None, *, adjacent=False, tls=None, credentials=None):
        self._fetch_projection = None
        self.tls_options = None
        if tls is not None:
            if adjacent or port is not None:
                raise ValueError("Укажите один источник данных Atlas.")
            self._fetch_projection = _tls_reader(tls, credentials)
            self.tls_options = dict(tls)
        elif adjacent:
            if adjacent is not True or port is not None or sys.platform != "linux":
                raise ValueError("Локальный канал чтения Atlas требует Linux.")
            self._fetch_projection = _fetch_feed
        elif type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError("Недопустимый порт источника Atlas.")
        self.port = port
        self._cache = {}
        self._pending = set()
        self._lock = threading.Lock()

    @classmethod
    def load(cls, config):
        """Create a source only when the operator supplies an opt-in file."""
        config = Path(config)
        try:
            try:
                data = _settings(config / "atlas-source.json")
            except FileNotFoundError:
                return None
            if isinstance(data, dict):
                if data.keys() == {"port"}:
                    return cls(data["port"])
                if data.keys() == {"adjacent"} and data["adjacent"] is True:
                    return cls(adjacent=True)
                if data.keys() == {"tls"} and isinstance(data["tls"], dict):
                    credentials = config / "atlas-read-credentials"
                    try:
                        credentials.lstat()
                    except FileNotFoundError:
                        credentials = None
                    return cls(tls=data["tls"], credentials=credentials)
            raise ValueError
        except (OSError, ValueError, RecursionError):
            raise ValueError("Недопустимые настройки источника Atlas.") from None

    def _cached(self, path, ttl, project):
        with self._lock:
            entry = self._cache.get(path)
            if entry is not None and entry[0] > time.monotonic():
                return deepcopy(entry[1])
            if path in self._pending:
                return None
            self._pending.add(path)
        try:
            try:
                value = self._fetch_projection(path) if self._fetch_projection else project(_fetch(self.port, path))
            except (OSError, ValueError, HTTPException, RecursionError):
                value = None
            with self._lock:
                self._cache[path] = (time.monotonic() + ttl, value)
            return deepcopy(value)
        finally:
            with self._lock:
                self._pending.discard(path)

    def snapshot(self, now=None):
        """Return only a fresh, allowlisted Atlas measurement, or a cleared snapshot."""
        sample = self._cached("/api/status", SNAPSHOT_TTL, _snapshot)
        now = time.time() if now is None else now
        return sample if sample and 0 <= now - sample["time"] <= FRESH_SECONDS else _offline()

    def history(self, period):
        """Return one native archive window, or None when that upstream is unavailable."""
        if not isinstance(period, str) or period not in RANGES:
            raise ValueError("Неизвестный диапазон истории Atlas.")
        return self._cached("/api/history?range=" + period, HISTORY_TTL, lambda data: _history(data, period))
