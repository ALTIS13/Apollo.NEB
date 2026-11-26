"""Pull-only fleet for registered workers and an opt-in diagnostic projection."""

from contextlib import closing, contextmanager
import hashlib
import hmac
import http.client
import json
import math
from pathlib import Path
import re
import secrets
import sqlite3
import ssl
import time
from urllib.parse import urlsplit


NODE_ID = re.compile(r"[0-9a-f]{24}")
COMMAND_ID = re.compile(r"[0-9a-f]{32}")
WORKER_ID = re.compile(r"[A-Za-z0-9_-]{1,32}")
PCI = re.compile(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]")
WINDOWS_PCI = re.compile(r"(?:[0-9a-f]{4}|[0-9a-f]{8}):[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]", re.IGNORECASE)
SYSTEM_LABEL = re.compile(r"[A-Za-z0-9._+~()-]{1,80}")
KEY = re.compile(r"[A-Za-z0-9_-]{43}")
CODE = re.compile(r"[A-Za-z0-9_-]{24}")
RELEASE_VERSION_PART = r"(?:0|[1-9][0-9]{0,3})"
RELEASE_VERSION = re.compile(
    rf"{RELEASE_VERSION_PART}\.{RELEASE_VERSION_PART}\.{RELEASE_VERSION_PART}(?:-rc\.{RELEASE_VERSION_PART})?"
)
SHA256 = re.compile(r"[0-9a-f]{64}")
STATES = {"running", "stopped", "failed", "blocked", "protection"}
ACTION = {"start", "stop", "restart"}
ACK = {"acknowledged", "unknown"}
EVENT_TYPES = {"start", "stop", "exit", "thermal"}
EVENT_STATES = {"running", "stopped", "failed", "expired", "unconfirmed"}
EVENT_LIMIT = 24
SYSTEM_LIMITS = {"cpu_count": 65536, "memory_bytes": 2**53 - 1, "boot_time": 253402300799}
LIMITS = {"edge": 160, "hot": 160, "hbm": 160, "watts": 3000, "rate_hs": 1e18,
          "accepted": 10**16, "rejected": 10**16}
GPU_SENSORS = {"edge": LIMITS["edge"], "watts": LIMITS["watts"], "utilization": 100,
               "memory_used_mib": 2**24, "memory_total_mib": 2**24}
POWER_ACTIVE = ("queued", "sent", "claimed")
POWER_OUTCOMES = ("applied", "refused", "unknown")
POWER_LEASE_SECONDS = 15
INVITE_SECONDS = 300
COMMAND_SECONDS = 30
ONLINE_SECONDS = 30
POLL_MAX_BYTES = 65536
UPGRADE_SECONDS = 15 * 60
UPGRADE_ACTIVE = ("queued", "sent", "preparing", "applying")
UPGRADE_TRANSITIONS = {
    "sent": {"preparing"},
    "preparing": {"applying", "failed"},
    "applying": {"installed", "rolled_back", "failed", "unknown"},
}
UPGRADE_RECEIPTS = set().union(*UPGRADE_TRANSITIONS.values())


class FleetAuthError(ValueError):
    """No fleet enrollment or node credential matched the request."""


def _matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _digest(kind, value):
    return hashlib.sha256(b"Apollo.NEB fleet v1\0" + kind + b"\0" + value.encode("ascii")).hexdigest()


def _now(now):
    return int(time.time() if now is None else now)


def _json(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))


def _workers(rows, *, inventory=False):
    if not isinstance(rows, list) or len(rows) > 64:
        raise ValueError("Неверный список воркеров.")
    cleaned = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Неверное состояние воркера.")
        ident, state = row.get("id"), row.get("state")
        if not _matches(WORKER_ID, ident) or ident in cleaned or not isinstance(state, str) or state not in STATES:
            raise ValueError("Неверное состояние воркера.")
        entry = {"id": ident, "state": state}
        for name, maximum in LIMITS.items():
            value = row.get(name)
            if (type(value) is int or (type(value) is float and math.isfinite(value))) and 0 <= value <= maximum:
                entry[name] = value
        if inventory:
            if _matches(PCI, row.get("device")):
                entry["device"] = row["device"]
            if _matches(SYSTEM_LABEL, row.get("adapter")):
                entry["adapter"] = row["adapter"]
        cleaned[ident] = entry
    return cleaned


def _worker_ids(ids):
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 16 or
            any(not _matches(WORKER_ID, item) for item in ids) or len(set(ids)) != len(ids)):
        raise ValueError("Укажите от 1 до 16 уникальных воркеров.")
    return ids


def _acks(acks):
    if not isinstance(acks, list) or len(acks) > 32:
        raise ValueError("Неверный список подтверждений.")
    out = {}
    for row in acks:
        if not isinstance(row, dict) or set(row) != {"id", "status"} or not _matches(COMMAND_ID, row["id"]) or not (
                isinstance(row["status"], str) and row["status"] in ACK) or row["id"] in out:
            raise ValueError("Неверное подтверждение команды.")
        out[row["id"]] = row["status"]
    return out


def _diagnostics(value):
    if value is None:
        return None
    if not isinstance(value, dict) or value.keys() != {"system", "events"}:
        raise ValueError("Неверный снимок диагностики ноды.")
    system, events = value["system"], value["events"]
    labels = {"platform", "kernel", "arch"}
    if (not isinstance(system, dict) or system.keys() != labels | SYSTEM_LIMITS.keys() or
            any(system[field] is not None and not _matches(SYSTEM_LABEL, system[field]) for field in labels) or
            any(system[field] is not None and not (type(system[field]) is int and 1 <= system[field] <= maximum)
                for field, maximum in SYSTEM_LIMITS.items())):
        raise ValueError("Неверные сведения о системе ноды.")
    if not isinstance(events, list) or len(events) > EVENT_LIMIT:
        raise ValueError("Неверный журнал ноды.")
    for event in events:
        if (not isinstance(event, dict) or event.keys() != {"time", "worker", "type", "status"} or
                type(event["time"]) is not int or not 1 <= event["time"] <= SYSTEM_LIMITS["boot_time"] or
                not _matches(WORKER_ID, event["worker"]) or not isinstance(event["type"], str) or
                event["type"] not in EVENT_TYPES or not isinstance(event["status"], str) or
                event["status"] not in EVENT_STATES):
            raise ValueError("Неверное событие ноды.")
    return value


def _release_metadata(value):
    if value is None:
        return None
    fields = {"version", "platform", "arch", "kernel"}
    if not isinstance(value, dict) or value.keys() not in (fields, fields | {"upgrade"}):
        raise ValueError("Неверные сведения о выпуске ноды.")
    platform, kernel = value["platform"], value["kernel"]
    if (not _matches(RELEASE_VERSION, value["version"]) or platform not in ("linux", "windows") or
            value["arch"] != "x64" or kernel not in (None, "6.6", "6.12") or
            platform == "windows" and kernel is not None):
        raise ValueError("Неверные сведения о выпуске ноды.")
    if "upgrade" in value and (type(value["upgrade"]) is not bool or value["upgrade"] and platform != "linux"):
        raise ValueError("Неверные сведения о выпуске ноды.")
    return dict(value)


def _hardware(value, release, workers):
    if value is None:
        return None
    if (not release or release["platform"] != "windows" or workers
            or not isinstance(value, dict) or value.keys() not in ({"devices"}, {"devices", "power_control"})
            or "power_control" in value and value["power_control"] is not True
            or not isinstance(value["devices"], list) or len(value["devices"]) > 32):
        raise ValueError("Неверная телеметрия GPU Windows.")
    devices = {}
    for row in value["devices"]:
        fields = {"id", "label", "vendor"} | GPU_SENSORS.keys()
        if (not isinstance(row, dict) or row.keys() not in (fields, fields | {"power_limit"})
                or not _matches(WINDOWS_PCI, row["id"]) or not isinstance(row["label"], str)
                or not row["label"].isprintable() or not 1 <= len(row["label"].strip()) <= 80
                or row["vendor"] != "nvidia"):
            raise ValueError("Неверные сведения о GPU Windows.")
        ident = _windows_pci(row["id"])
        if ident in devices or any(
            row[name] is not None and not (type(row[name]) in (int, float) and 0 <= row[name] <= maximum)
            for name, maximum in GPU_SENSORS.items()
        ):
            raise ValueError("Неверные значения датчиков GPU Windows.")
        used, total = row["memory_used_mib"], row["memory_total_mib"]
        if total is not None and (total < 1 or used is not None and used > total):
            raise ValueError("Неверные значения памяти GPU Windows.")
        if "power_limit" in row and not _power_limits(row["power_limit"]):
            raise ValueError("Неверные пределы мощности GPU Windows.")
        devices[ident] = {**row, "id": ident, "label": row["label"].strip()}
    snapshot = {"devices": list(devices.values())}
    if value.get("power_control") is True:
        if not any("power_limit" in item for item in devices.values()):
            raise ValueError("У окна управления нет подтверждённого диапазона мощности.")
        snapshot["power_control"] = True
    return snapshot


def _windows_pci(value):
    if not _matches(WINDOWS_PCI, value):
        return None
    domain, address = value.lower().split(":", 1)
    return domain.zfill(8) + ":" + address


def _power_limits(value):
    return (isinstance(value, dict) and value.keys() == {"min", "max", "current"}
            and all(type(watts) in (int, float) and 1 <= watts <= LIMITS["watts"] for watts in value.values())
            and value["min"] <= value["current"] <= value["max"])


def _power_permitted(snapshot, device_id, watts):
    if not snapshot or snapshot.get("power_control") is not True:
        return False
    limits = next((gpu.get("power_limit") for gpu in snapshot["devices"] if gpu["id"] == device_id), None)
    return bool(limits and limits["min"] <= watts <= limits["max"])


def _power_receipt(receipt):
    if not isinstance(receipt, dict) or not _matches(COMMAND_ID, receipt.get("id")):
        return False
    phase = receipt.get("phase")
    if phase == "claim":
        return receipt.keys() == {"id", "phase"}
    observed = receipt.get("observed_watts")
    return (receipt.keys() == {"id", "phase", "observed_watts"} and phase in POWER_OUTCOMES
            and (observed is None or type(observed) in (int, float) and 1 <= observed <= LIMITS["watts"]))


def _upgrade_selection(current, tag, sha256, channel):
    if (not _matches(RELEASE_VERSION, current) or not isinstance(tag, str) or
            not tag.startswith("v") or not _matches(RELEASE_VERSION, tag[1:]) or
            not _matches(SHA256, sha256) or channel not in ("stable", "testing")):
        raise ValueError("Неверное описание обновления ноды.")

    def order(version):
        base, prerelease, revision = version.partition("-rc.")
        return (*map(int, base.split(".")), not prerelease, int(revision or 0))

    if (channel == "testing") != ("-rc." in tag) or order(tag[1:]) <= order(current):
        raise ValueError("Выпуск не подходит для обновления ноды.")


def _upgrade_receipt(receipt):
    if (not isinstance(receipt, dict) or receipt.keys() != {"id", "status"} or
            not _matches(COMMAND_ID, receipt["id"]) or not isinstance(receipt["status"], str) or
            receipt["status"] not in UPGRADE_RECEIPTS):
        raise ValueError("Неверное подтверждение обновления ноды.")


def _running(reported, selected):
    return all(reported.get(worker, {}).get("state") == "running" for worker in selected)


class _Database:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def write(self):
        with closing(sqlite3.connect(self.path, timeout=3)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            yield db


class Fleet(_Database):
    def __init__(self, path):
        super().__init__(path)
        with self.write() as db:
            db.execute("CREATE TABLE IF NOT EXISTS nodes (id TEXT PRIMARY KEY, label TEXT NOT NULL, "
                       "invite TEXT, invite_expiry INTEGER NOT NULL, key_hash TEXT UNIQUE, "
                       "revoked INTEGER, last_seen INTEGER, workers TEXT NOT NULL DEFAULT '[]')")
            db.execute("CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY, node TEXT NOT NULL "
                       "REFERENCES nodes(id) ON DELETE CASCADE, action TEXT NOT NULL, workers TEXT NOT NULL, "
                       "status TEXT NOT NULL, created INTEGER NOT NULL, expires INTEGER NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS commands_node ON commands(node, created)")
            db.execute("CREATE TABLE IF NOT EXISTS diagnostics (node TEXT PRIMARY KEY REFERENCES nodes(id) "
                       "ON DELETE CASCADE, snapshot TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS node_releases (node TEXT PRIMARY KEY REFERENCES nodes(id) "
                       "ON DELETE CASCADE, snapshot TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS node_hardware (node TEXT PRIMARY KEY REFERENCES nodes(id) "
                       "ON DELETE CASCADE, snapshot TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS node_power_jobs (node TEXT PRIMARY KEY REFERENCES nodes(id) "
                       "ON DELETE CASCADE, id TEXT NOT NULL UNIQUE, gpu TEXT NOT NULL, watts INTEGER NOT NULL, "
                       "status TEXT NOT NULL, time INTEGER NOT NULL, expires INTEGER NOT NULL, observed REAL)")
            db.execute("CREATE TABLE IF NOT EXISTS node_upgrades (node TEXT PRIMARY KEY REFERENCES nodes(id) "
                       "ON DELETE CASCADE, id TEXT NOT NULL UNIQUE, current TEXT NOT NULL, tag TEXT NOT NULL, "
                       "sha256 TEXT NOT NULL, channel TEXT NOT NULL, status TEXT NOT NULL, time INTEGER NOT NULL, "
                       "expires INTEGER NOT NULL)")

    @staticmethod
    def _expire(db, now):
        db.execute("UPDATE commands SET status='unknown' WHERE status IN ('queued','sent') AND expires<=?", (now,))
        db.execute("DELETE FROM commands WHERE expires<?", (now - 7 * 86400,))
        db.execute("UPDATE node_upgrades SET status=CASE status WHEN 'applying' THEN 'unknown' ELSE 'expired' END,time=expires "
                   "WHERE expires<=? AND status IN (?,?,?,?)", (now, *UPGRADE_ACTIVE))
        db.execute("UPDATE node_power_jobs SET status=CASE status WHEN 'claimed' THEN 'unknown' ELSE 'expired' END, "
                   "time=expires WHERE expires<=? AND status IN (?,?,?)", (now, *POWER_ACTIVE))

    def enroll(self, label, *, now=None):
        if not isinstance(label, str) or not label.isprintable() or not 1 <= len(label.strip()) <= 64 or len(label) > 64:
            raise ValueError("Нужна короткая подпись узла.")
        now = _now(now)
        ident, code = secrets.token_hex(12), secrets.token_urlsafe(18)
        expires = now + INVITE_SECONDS
        with self.write() as db:
            db.execute("DELETE FROM nodes WHERE key_hash IS NULL AND invite_expiry<?", (now - 86400,))
            if (db.execute("SELECT COUNT(*) FROM nodes WHERE revoked IS NULL AND (key_hash IS NOT NULL "
                           "OR invite_expiry>?)", (now,)).fetchone()[0] >= 64 or
                    db.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] >= 1024):
                raise ValueError("Достигнут предел числа узлов.")
            db.execute("INSERT INTO nodes(id,label,invite,invite_expiry) VALUES (?,?,?,?)",
                       (ident, label.strip(), _digest(b"invite", code), expires))
        return {"id": ident, "code": code, "expires": expires}

    def pair(self, id, code, key, *, now=None):
        refuse = FleetAuthError("Код подключения или ключ узла не принят.")
        if not _matches(NODE_ID, id) or not _matches(CODE, code) or not _matches(KEY, key):
            raise refuse
        now = _now(now)
        candidate = _digest(b"key", key)
        with self.write() as db:
            node = db.execute("SELECT invite,invite_expiry,key_hash,revoked FROM nodes WHERE id=?", (id,)).fetchone()
            if (node is None or node["revoked"] is not None or node["invite"] is None or node["invite_expiry"] <= now
                    or not hmac.compare_digest(node["invite"], _digest(b"invite", code))):
                raise refuse
            if node["key_hash"] is not None:
                if not hmac.compare_digest(node["key_hash"], candidate):
                    raise refuse
            elif db.execute("SELECT 1 FROM nodes WHERE key_hash=?", (candidate,)).fetchone() is not None:
                raise refuse
            else:
                db.execute("UPDATE nodes SET key_hash=? WHERE id=?", (candidate, id))
        return {"ok": True}

    @staticmethod
    def _authenticated_node(db, id, key):
        refuse = FleetAuthError("Ключ узла не принят.")
        if not _matches(NODE_ID, id) or not _matches(KEY, key):
            raise refuse
        node = db.execute("SELECT key_hash,revoked,last_seen FROM nodes WHERE id=?", (id,)).fetchone()
        if (node is None or node["revoked"] is not None or node["key_hash"] is None or
                not hmac.compare_digest(node["key_hash"], _digest(b"key", key))):
            raise refuse
        return node

    def check(self, id, key):
        with closing(sqlite3.connect(self.path, timeout=3)) as db:
            db.row_factory = sqlite3.Row
            node = self._authenticated_node(db, id, key)
            return {"ok": True, "last_seen": node["last_seen"]}

    def poll(self, id, key, workers, acks, *, now=None, details=None, release=None, hardware=None):
        with self.write() as db:
            now = _now(now)
            self._authenticated_node(db, id, key)
            diagnostic = _diagnostics(details)
            release_info = _release_metadata(release)
            reported, finished = _workers(workers, inventory=diagnostic is not None), _acks(acks)
            devices = _hardware(hardware, release_info, reported)
            self._expire(db, now)
            db.execute("UPDATE nodes SET last_seen=?, workers=? WHERE id=?", (now, _json(list(reported.values())), id))
            if diagnostic is None:
                db.execute("DELETE FROM diagnostics WHERE node=?", (id,))
            else:
                db.execute("INSERT INTO diagnostics VALUES (?,?) ON CONFLICT(node) DO UPDATE "
                           "SET snapshot=excluded.snapshot", (id, _json(diagnostic)))
            if release_info is None:
                db.execute("DELETE FROM node_releases WHERE node=?", (id,))
            else:
                db.execute("INSERT INTO node_releases VALUES (?,?) ON CONFLICT(node) DO UPDATE "
                           "SET snapshot=excluded.snapshot", (id, _json(release_info)))
            if devices is None:
                db.execute("DELETE FROM node_hardware WHERE node=?", (id,))
            else:
                db.execute("INSERT INTO node_hardware VALUES (?,?) ON CONFLICT(node) DO UPDATE "
                           "SET snapshot=excluded.snapshot", (id, _json(devices)))
            reserved = db.execute("SELECT gpu,watts FROM node_power_jobs WHERE node=? AND status IN ('queued','sent')",
                                  (id,)).fetchone()
            if reserved and not _power_permitted(devices, reserved["gpu"], reserved["watts"]):
                db.execute("UPDATE node_power_jobs SET status='cancelled',time=? WHERE node=?", (now, id))
            for command_id, outcome in finished.items():
                db.execute("UPDATE commands SET status=? WHERE id=? AND node=? AND status='sent'", (outcome, command_id, id))
            items = []
            for row in db.execute("SELECT id,action,workers,expires FROM commands WHERE node=? "
                                  "AND status='queued' ORDER BY created, id LIMIT 16", (id,)).fetchall():
                selected = json.loads(row["workers"])
                if not set(selected) <= reported.keys() or (row["action"] == "restart" and
                        (diagnostic is None or not _running(reported, selected))):
                    db.execute("UPDATE commands SET status='unknown' WHERE id=?", (row["id"],))
                    continue
                db.execute("UPDATE commands SET status='sent' WHERE id=?", (row["id"],))
                items.append(dict(row, workers=selected))
            return {"commands": items}

    def command(self, id, action, ids, *, now=None):
        if not _matches(NODE_ID, id) or not isinstance(action, str) or action not in ACTION:
            raise ValueError("Команда для воркера недоступна.")
        selected, now = set(_worker_ids(ids)), _now(now)
        ident = secrets.token_hex(16)
        with self.write() as db:
            self._expire(db, now)
            node = db.execute("SELECT * FROM nodes WHERE id=?", (id,)).fetchone()
            if (node is None or node["revoked"] is not None or node["key_hash"] is None or
                    node["last_seen"] is None or not 0 <= now - node["last_seen"] < ONLINE_SECONDS):
                raise ValueError("Узел не в сети.")
            if action in ("start", "restart") and self._active_upgrade(db, id):
                raise ValueError("Узел занят обновлением.")
            reported = {item["id"]: item for item in json.loads(node["workers"])}
            if not selected <= reported.keys():
                raise ValueError("Нельзя управлять незарегистрированным воркером.")
            if action == "restart" and (not _running(reported, selected) or
                    not db.execute("SELECT 1 FROM diagnostics WHERE node=?", (id,)).fetchone()):
                raise ValueError("Перезапуск доступен для работающих воркеров с подключённой диагностикой.")
            active = db.execute("SELECT workers FROM commands WHERE node=? AND status IN ('queued','sent')", (id,)).fetchall()
            if len(active) >= 16 or any(selected.intersection(json.loads(row[0])) for row in active):
                raise ValueError("Дождитесь результата предыдущей команды для воркера.")
            if db.execute("SELECT COUNT(*) FROM commands WHERE node=?", (id,)).fetchone()[0] >= 512:
                raise ValueError("Достигнут предел недавних команд узла.")
            db.execute("INSERT INTO commands VALUES (?,?,?,?,?,?,?)",
                       (ident, id, action, _json(ids), "queued", now, now + COMMAND_SECONDS))
        return {"id": ident, "status": "queued"}

    @staticmethod
    def _active_upgrade(db, node_id):
        return db.execute("SELECT 1 FROM node_upgrades WHERE node=? AND status IN (?,?,?,?)",
                          (node_id, *UPGRADE_ACTIVE)).fetchone() is not None

    @staticmethod
    def _online_release(db, node_id, now):
        return db.execute(
            "SELECT node_releases.snapshot, nodes.last_seen, nodes.workers "
            "FROM nodes JOIN node_releases ON node_releases.node=nodes.id "
            "WHERE nodes.id=? AND revoked IS NULL AND key_hash IS NOT NULL AND last_seen>? AND last_seen<=?",
            (node_id, now - ONLINE_SECONDS, now),
        ).fetchone()

    def _ready_to_upgrade(self, db, node_id, current, now):
        node = self._online_release(db, node_id, now)
        if node is None:
            return False
        release = json.loads(node["snapshot"])
        if release.get("upgrade") is not True or release["version"] != current:
            return False
        # A protection state can still have a miner that has not exited.
        if any(row["state"] not in {"stopped", "failed", "blocked"} for row in json.loads(node["workers"])):
            return False
        return db.execute("SELECT 1 FROM commands WHERE node=? AND status IN ('queued','sent') LIMIT 1",
                          (node_id,)).fetchone() is None

    def queue_upgrade(self, node_id, *, current, tag, sha256, channel, now=None):
        """Reserve one catalog-selected update for an enrolled, idle node."""
        if not _matches(NODE_ID, node_id):
            raise ValueError("Нет такого узла.")
        _upgrade_selection(current, tag, sha256, channel)
        now, job_id = _now(now), secrets.token_hex(16)
        with self.write() as db:
            self._expire(db, now)
            if self._active_upgrade(db, node_id):
                raise ValueError("Узел занят обновлением.")
            if not self._ready_to_upgrade(db, node_id, current, now):
                raise ValueError("Для обновления нужен подключённый узел с исходной версией, без работы и команд.")
            db.execute("INSERT INTO node_upgrades(node,id,current,tag,sha256,channel,status,time,expires) "
                       "VALUES (?,?,?,?,?,?,'queued',?,?) ON CONFLICT(node) DO UPDATE SET id=excluded.id, "
                       "current=excluded.current,tag=excluded.tag,sha256=excluded.sha256,channel=excluded.channel, "
                       "status=excluded.status,time=excluded.time,expires=excluded.expires",
                       (node_id, job_id, current, tag, sha256, channel, now, now + UPGRADE_SECONDS))
        return {"id": job_id, "tag": tag, "status": "queued", "time": now}

    def _offer_upgrade(self, db, job, now):
        if (job is None or job["status"] not in ("queued", "sent") or
                not self._ready_to_upgrade(db, job["node"], job["current"], now)):
            return None
        if job["status"] == "queued":
            db.execute("UPDATE node_upgrades SET status='sent',time=? WHERE id=?", (now, job["id"]))
        return {field: job[field] for field in ("id", "current", "tag", "sha256", "channel", "expires")}

    def _accept_upgrade_receipt(self, db, job, status, now):
        if status == job["status"]:
            return True
        if status not in UPGRADE_TRANSITIONS.get(job["status"], ()):
            return False
        if status in ("preparing", "applying") and not self._ready_to_upgrade(db, job["node"], job["current"], now):
            return False
        if status in ("installed", "rolled_back", "unknown"):
            node = self._online_release(db, job["node"], now)
            expected = {job["tag"][1:]} if status == "installed" else {job["current"]}
            if status == "unknown":
                expected.add(job["tag"][1:])
            if node is None or node["last_seen"] <= job["time"] or json.loads(node["snapshot"])["version"] not in expected:
                return False
        db.execute("UPDATE node_upgrades SET status=?,time=? WHERE id=?", (status, now, job["id"]))
        return True

    def upgrade_poll(self, node_id, key, receipt=None, *, now=None):
        """Offer or confirm one job without updating node liveness or handling worker commands."""
        now = _now(now)
        with self.write() as db:
            self._authenticated_node(db, node_id, key)
            if receipt is not None:
                _upgrade_receipt(receipt)
            self._expire(db, now)
            job = db.execute("SELECT * FROM node_upgrades WHERE node=?", (node_id,)).fetchone()
            if receipt is None:
                return {"job": self._offer_upgrade(db, job, now)}
            if job is not None and job["id"] == receipt["id"] and self._accept_upgrade_receipt(
                    db, job, receipt["status"], now):
                return {"job": None}
        raise ValueError("Этап обновления ноды не подтверждён.")

    @staticmethod
    def _power_ready(db, node_id, device_id, watts, now):
        row = db.execute(
            "SELECT node_hardware.snapshot FROM nodes JOIN node_hardware ON node_hardware.node=nodes.id "
            "WHERE nodes.id=? AND revoked IS NULL AND key_hash IS NOT NULL AND last_seen>? AND last_seen<=?",
            (node_id, now - ONLINE_SECONDS, now),
        ).fetchone()
        return row is not None and _power_permitted(json.loads(row["snapshot"]), device_id, watts)

    def queue_power(self, node_id, gpu, watts, *, now=None):
        device = _windows_pci(gpu)
        if not _matches(NODE_ID, node_id) or device is None or type(watts) is not int or not 1 <= watts <= LIMITS["watts"]:
            raise ValueError("Укажите GPU и лимит мощности в целых ваттах.")
        ident = secrets.token_hex(16)
        with self.write() as db:
            now = _now(now)
            self._expire(db, now)
            previous = db.execute("SELECT status,time FROM node_power_jobs WHERE node=?", (node_id,)).fetchone()
            if previous and previous["status"] in POWER_ACTIVE:
                raise ValueError("Предыдущая команда ещё выполняется.")
            if previous and previous["status"] == "unknown" and not db.execute(
                "SELECT 1 FROM nodes WHERE id=? AND last_seen>?", (node_id, previous["time"])
            ).fetchone():
                raise ValueError("Дождитесь нового показания после предыдущей команды.")
            if not self._power_ready(db, node_id, device, watts, now):
                raise ValueError("Нужна доступная нода с разрешённым управлением и подходящим пределом мощности.")
            db.execute("INSERT INTO node_power_jobs(node,id,gpu,watts,status,time,expires,observed) "
                       "VALUES (?,?,?,?,'queued',?,?,NULL) ON CONFLICT(node) DO UPDATE SET id=excluded.id, "
                       "gpu=excluded.gpu,watts=excluded.watts,status=excluded.status,time=excluded.time,"
                       "expires=excluded.expires,observed=NULL", (node_id, ident, device, watts, now, now + COMMAND_SECONDS))
        return {"id": ident, "gpu": device, "watts": watts, "status": "queued", "time": now}

    def power_poll(self, node_id, key, receipt=None, *, now=None):
        with self.write() as db:
            now = _now(now)
            self._authenticated_node(db, node_id, key)
            if receipt is not None and not _power_receipt(receipt):
                raise ValueError("Неверное подтверждение мощности GPU.")
            self._expire(db, now)
            job = db.execute("SELECT * FROM node_power_jobs WHERE node=?", (node_id,)).fetchone()
            if receipt is None:
                if job is None or job["status"] != "queued" or not self._power_ready(db, node_id, job["gpu"], job["watts"], now):
                    return {"job": None}
                db.execute("UPDATE node_power_jobs SET status='sent',time=? WHERE id=?", (now, job["id"]))
                return {"job": {field: job[field] for field in ("id", "gpu", "watts", "expires")}}
            if job is not None and receipt["id"] == job["id"]:
                response = self._accept_power(db, job, receipt, now)
                if response is not None:
                    return response
        raise ValueError("Этап изменения мощности GPU не подтверждён.")

    def _accept_power(self, db, job, receipt, now):
        phase, observed = receipt["phase"], receipt.get("observed_watts")
        if phase == "claim":
            if job["status"] != "sent" or not self._power_ready(db, job["node"], job["gpu"], job["watts"], now):
                return None
            lease = now + POWER_LEASE_SECONDS
            db.execute("UPDATE node_power_jobs SET status='claimed',time=?,expires=? WHERE id=?", (now, lease, job["id"]))
            return {"ok": True, "lease_expires": lease}
        elif phase == job["status"] and observed == job["observed"]:
            return {"ok": True}
        elif job["status"] != "claimed" or phase == "applied" and observed != job["watts"]:
            return None
        db.execute("UPDATE node_power_jobs SET status=?,time=?,observed=? WHERE id=?",
                   (phase, now, observed, job["id"]))
        return {"ok": True}

    def revoke(self, id, *, now=None):
        if not _matches(NODE_ID, id):
            raise ValueError("Нет такого узла.")
        now = _now(now)
        with self.write() as db:
            self._expire(db, now)
            if not db.execute("UPDATE nodes SET revoked=COALESCE(revoked,?), invite=NULL, workers='[]' WHERE id=?",
                              (now, id)).rowcount:
                raise ValueError("Нет такого узла.")
            db.execute("UPDATE commands SET status='unknown' WHERE node=? AND status IN ('queued','sent')", (id,))
            db.execute("UPDATE node_upgrades SET status='cancelled',time=? WHERE node=? AND status IN (?,?,?,?)",
                       (now, id, *UPGRADE_ACTIVE))
            db.execute("DELETE FROM diagnostics WHERE node=?", (id,))
            db.execute("DELETE FROM node_releases WHERE node=?", (id,))
            db.execute("DELETE FROM node_hardware WHERE node=?", (id,))
            db.execute("UPDATE node_power_jobs SET status='cancelled',time=? WHERE node=? AND status IN ('queued','sent')",
                       (now, id))
        return {"ok": True}

    def release_target(self, node_id, *, now=None):
        """Return the last release advertisement only while its registered node is online."""
        if not _matches(NODE_ID, node_id):
            return None
        now = _now(now)
        with closing(sqlite3.connect(self.path, timeout=3)) as db:
            row = self._online_release(db, node_id, now)
        return json.loads(row[0]) if row is not None else None

    def observations(self, *, now=None):
        now = _now(now)
        with closing(sqlite3.connect(self.path, timeout=3)) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT id,last_seen,workers,diagnostics.snapshot IS NOT NULL AS inventory, "
                "node_hardware.snapshot AS hardware "
                "FROM nodes LEFT JOIN diagnostics ON diagnostics.node=nodes.id "
                "LEFT JOIN node_hardware ON node_hardware.node=nodes.id "
                "WHERE revoked IS NULL AND key_hash IS NOT NULL"
            )
            nodes = []
            for row in rows:
                seen = row["last_seen"]
                status = "online" if seen is not None and 0 <= now - seen < ONLINE_SECONDS else "offline"
                node = {"id": row["id"], "status": status, "last_seen": seen}
                if status == "online":
                    node["workers"] = json.loads(row["workers"])
                    if row["inventory"]:
                        node["details"] = {}
                    if row["hardware"] is not None:
                        node["hardware"] = json.loads(row["hardware"])
                nodes.append(node)
            return {"nodes": nodes}

    def list(self, *, now=None):
        now = _now(now)
        with self.write() as db:
            self._expire(db, now)
            result = []
            for node in db.execute(
                "SELECT nodes.*,diagnostics.snapshot,node_releases.snapshot AS release_snapshot, "
                "node_hardware.snapshot AS hardware, "
                "node_upgrades.id AS upgrade_id,node_upgrades.tag AS upgrade_tag,node_upgrades.status AS upgrade_status, "
                "node_upgrades.time AS upgrade_time FROM nodes "
                "LEFT JOIN diagnostics ON diagnostics.node=nodes.id "
                "LEFT JOIN node_releases ON node_releases.node=nodes.id "
                "LEFT JOIN node_hardware ON node_hardware.node=nodes.id "
                "LEFT JOIN node_upgrades ON node_upgrades.node=nodes.id ORDER BY nodes.label,nodes.id"
            ):
                seen = node["last_seen"]
                if node["revoked"] is not None:
                    status = "revoked"
                elif node["key_hash"] is not None:
                    status = "online" if seen is not None and 0 <= now - seen < ONLINE_SECONDS else "offline"
                else:
                    status = "pending" if node["invite_expiry"] > now else "expired"
                commands = [dict(row, workers=json.loads(row["workers"])) for row in db.execute(
                    "SELECT id,action,workers,status,expires FROM commands WHERE node=? ORDER BY created DESC,id LIMIT 10",
                    (node["id"],))]
                projection = dict(id=node["id"], label=node["label"], status=status, last_seen=seen,
                                  workers=json.loads(node["workers"]), commands=commands)
                if node["snapshot"] is not None:
                    projection["details"] = json.loads(node["snapshot"])
                if status == "online" and node["release_snapshot"] is not None:
                    projection["release"] = json.loads(node["release_snapshot"])
                if status == "online" and node["hardware"] is not None:
                    projection["hardware"] = json.loads(node["hardware"])
                if node["upgrade_id"] is not None:
                    projection["upgrade"] = {field: node[f"upgrade_{field}"] for field in ("id", "tag", "status", "time")}
                power = db.execute("SELECT id,gpu,watts,status,time,observed AS observed_watts FROM node_power_jobs WHERE node=?",
                                   (node["id"],)).fetchone()
                if power is not None:
                    projection["power_job"] = dict(power)
                result.append(projection)
            return {"nodes": result}


class Client(_Database):
    """Fixed-origin outbound client; execute is the local worker authority."""

    def __init__(self, origin, id, key, state_file, *, allow_loopback=False):
        try:
            if not isinstance(origin, str) or not re.fullmatch(r"https?://[A-Za-z0-9.-]+(?::[0-9]{1,5})?", origin):
                raise ValueError
            parts = urlsplit(origin)
            port = parts.port
            if port == 0 or (parts.scheme != "https" and not (allow_loopback and parts.hostname == "127.0.0.1")):
                raise ValueError
        except ValueError:
            raise ValueError("Укажите HTTPS-адрес панели или явно разрешённый туннель 127.0.0.1.") from None
        if not _matches(NODE_ID, id) or not _matches(KEY, key):
            raise ValueError("Неверный идентификатор узла или ключ.")
        self.id, self._key = id, key
        self._scheme, self._host, self._port = parts.scheme, parts.hostname, port
        self._context = ssl.create_default_context() if self._scheme == "https" else None
        if state_file is None:
            self.path = None
            return
        super().__init__(state_file)
        binding = _digest(b"client", origin + "\n" + id + "\n" + key)
        with self.write() as db:
            db.execute("CREATE TABLE IF NOT EXISTS client_binding (id INTEGER PRIMARY KEY CHECK(id=1), binding TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO client_binding VALUES (1,?)", (binding,))
            if db.execute("SELECT binding FROM client_binding WHERE id=1").fetchone()[0] != binding:
                raise ValueError("Журнал привязан к другой регистрации узла.")
            db.execute("CREATE TABLE IF NOT EXISTS claims (id TEXT PRIMARY KEY, status TEXT NOT NULL, "
                       "expires INTEGER NOT NULL, reported INTEGER NOT NULL DEFAULT 0)")

    def _post(self, route, data):
        if route not in {"/api/node/pair", "/api/node/poll", "/api/node/check", "/api/node/upgrade"}:
            raise ValueError("Недопустимый адрес fleet.")
        payload = _json(data).encode("ascii")
        if len(payload) > POLL_MAX_BYTES:
            raise ValueError("Пакет fleet превышает лимит.")
        if self._scheme == "https":
            conn = http.client.HTTPSConnection(self._host, self._port, timeout=5, context=self._context)
        else:
            conn = http.client.HTTPConnection(self._host, self._port, timeout=5)
        try:
            conn.request("POST", route, body=payload, headers={"Content-Type": "application/json", "Accept": "application/json"})
            reply = conn.getresponse()
            if reply.status != 200 or reply.getheader("Content-Type", "").split(";", 1)[0].strip() != "application/json":
                raise ValueError("Fleet отклонил запрос или вернул неподдерживаемый ответ.")
            body = reply.read(POLL_MAX_BYTES + 1)
            if len(body) > POLL_MAX_BYTES:
                raise ValueError("Ответ fleet превышает лимит.")
            return json.loads(body)
        except (OSError, http.client.HTTPException, UnicodeError, json.JSONDecodeError):
            raise ValueError("Ответ fleet не подтверждён.") from None
        finally:
            conn.close()

    def join(self, code):
        if not _matches(CODE, code):
            raise ValueError("Неверный код подключения.")
        response = self._post("/api/node/pair", {"id": self.id, "code": code, "key": self._key})
        if response != {"ok": True}:
            raise ValueError("Привязка узла не подтверждена.")
        return response

    def check(self):
        response = self._post("/api/node/check", {"id": self.id, "key": self._key})
        if (not isinstance(response, dict) or response.keys() != {"ok", "last_seen"} or response["ok"] is not True or
                (response["last_seen"] is not None and not (type(response["last_seen"]) is int and response["last_seen"] > 0))):
            raise ValueError("Центральная панель не подтвердила связь.")
        return response

    def upgrade_exchange(self, receipt=None):
        if receipt is not None:
            _upgrade_receipt(receipt)
        response = self._post("/api/node/upgrade", {"id": self.id, "key": self._key, "receipt": receipt})
        error = ValueError("Неверный ответ по обновлению ноды.")
        if not isinstance(response, dict) or response.keys() != {"job"}:
            raise error
        job = response["job"]
        if job is not None:
            if (receipt is not None or not isinstance(job, dict) or
                    job.keys() != {"id", "current", "tag", "sha256", "channel", "expires"} or
                    not _matches(COMMAND_ID, job["id"]) or type(job["expires"]) is not int or job["expires"] < 1):
                raise error
            _upgrade_selection(job["current"], job["tag"], job["sha256"], job["channel"])
        return response

    def cycle(self, snapshot, execute, *, now=None, details=None, release=None):
        if self.path is None:
            raise RuntimeError("Для обработки команд нужен локальный журнал агента.")
        diagnostic = _diagnostics(details)
        release_info = _release_metadata(release)
        report = _workers(snapshot, inventory=diagnostic is not None)
        with self.write() as db:
            db.execute("DELETE FROM claims WHERE expires<?", (_now(now) - 600,))
            acks = [dict(row) for row in db.execute("SELECT id,status FROM claims WHERE reported=0 ORDER BY expires LIMIT 32")]
        payload = {"id": self.id, "key": self._key, "workers": list(report.values()), "acks": acks}
        if diagnostic is not None:
            payload["details"] = diagnostic
        if release_info is not None:
            payload["release"] = release_info
        response = self._post("/api/node/poll", payload)
        commands = response.get("commands") if isinstance(response, dict) and set(response) == {"commands"} else None
        if not isinstance(commands, list) or len(commands) > 16:
            raise ValueError("Неверный ответ по командам fleet.")
        checked = []
        all_ids = set()
        for item in commands:
            if (not isinstance(item, dict) or set(item) != {"id", "action", "workers", "expires"} or
                    not _matches(COMMAND_ID, item["id"]) or item["id"] in all_ids or
                    not isinstance(item["action"], str) or item["action"] not in ACTION or type(item["expires"]) is not int):
                raise ValueError("Неверная команда fleet.")
            _worker_ids(item["workers"])
            checked.append(item)
            all_ids.add(item["id"])
        with self.write() as db:
            for ack in acks:
                db.execute("UPDATE claims SET reported=1 WHERE id=? AND status=?", (ack["id"], ack["status"]))
        for item in checked:
            ident, expires = item["id"], item["expires"]
            current = _now(now)
            if not 0 < expires - current <= COMMAND_SECONDS * 2:
                continue
            with self.write() as db:
                if db.execute("SELECT COUNT(*) FROM claims").fetchone()[0] >= 512:
                    raise ValueError("Достигнут предел журнала команд агента.")
                if not db.execute("INSERT OR IGNORE INTO claims(id,status,expires) VALUES (?,?,?)",
                                  (ident, "unknown", expires)).rowcount:
                    continue
            if (not set(item["workers"]) <= report.keys() or _now(now) >= expires or
                    (item["action"] == "restart" and (diagnostic is None or not _running(report, item["workers"])))):
                continue
            try:
                if execute(item["action"], list(item["workers"])) is False:
                    continue
            except (ValueError, RuntimeError, OSError, sqlite3.Error):
                continue
            with self.write() as db:
                db.execute("UPDATE claims SET status='acknowledged',reported=0 WHERE id=?", (ident,))
