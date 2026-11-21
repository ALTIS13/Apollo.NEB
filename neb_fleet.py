"""Pull-only fleet. Transport never grants shell, file or arbitrary URL authority."""

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
KEY = re.compile(r"[A-Za-z0-9_-]{43}")
CODE = re.compile(r"[A-Za-z0-9_-]{24}")
STATES = {"running", "stopped", "failed", "blocked", "protection"}
ACTION = {"start", "stop"}
ACK = {"acknowledged", "unknown"}
LIMITS = {"edge": 160, "hot": 160, "hbm": 160, "watts": 3000, "rate_hs": 1e18,
          "accepted": 10**16, "rejected": 10**16}
INVITE_SECONDS = 300
COMMAND_SECONDS = 30
ONLINE_SECONDS = 30
POLL_MAX_BYTES = 65536


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


def _workers(rows):
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

    @staticmethod
    def _expire(db, now):
        db.execute("UPDATE commands SET status='unknown' WHERE status IN ('queued','sent') AND expires<=?", (now,))
        db.execute("DELETE FROM commands WHERE expires<?", (now - 7 * 86400,))

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

    def poll(self, id, key, workers, acks, *, now=None):
        refuse = FleetAuthError("Ключ узла не принят.")
        if not _matches(NODE_ID, id) or not _matches(KEY, key):
            raise refuse
        now = _now(now)
        with self.write() as db:
            node = db.execute("SELECT key_hash,revoked FROM nodes WHERE id=?", (id,)).fetchone()
            if (node is None or node["revoked"] is not None or node["key_hash"] is None or
                    not hmac.compare_digest(node["key_hash"], _digest(b"key", key))):
                raise refuse
            reported, finished = _workers(workers), _acks(acks)
            self._expire(db, now)
            db.execute("UPDATE nodes SET last_seen=?, workers=? WHERE id=?", (now, _json(list(reported.values())), id))
            for command_id, outcome in finished.items():
                db.execute("UPDATE commands SET status=? WHERE id=? AND node=? AND status='sent'", (outcome, command_id, id))
            items = []
            for row in db.execute("SELECT id,action,workers,expires FROM commands WHERE node=? "
                                  "AND status='queued' ORDER BY created, id LIMIT 16", (id,)).fetchall():
                selected = json.loads(row["workers"])
                if not set(selected) <= reported.keys():
                    db.execute("UPDATE commands SET status='unknown' WHERE id=?", (row["id"],))
                    continue
                db.execute("UPDATE commands SET status='sent' WHERE id=?", (row["id"],))
                items.append(dict(row, workers=selected))
            return {"commands": items}

    def command(self, id, action, ids, *, now=None):
        if not _matches(NODE_ID, id) or not isinstance(action, str) or action not in ACTION:
            raise ValueError("Допустимы только запуск и остановка воркеров.")
        selected, now = set(_worker_ids(ids)), _now(now)
        ident = secrets.token_hex(16)
        with self.write() as db:
            self._expire(db, now)
            node = db.execute("SELECT * FROM nodes WHERE id=?", (id,)).fetchone()
            if (node is None or node["revoked"] is not None or node["key_hash"] is None or
                    node["last_seen"] is None or not 0 <= now - node["last_seen"] < ONLINE_SECONDS):
                raise ValueError("Узел не в сети.")
            if not selected <= {item["id"] for item in json.loads(node["workers"])}:
                raise ValueError("Нельзя управлять незарегистрированным воркером.")
            active = db.execute("SELECT workers FROM commands WHERE node=? AND status IN ('queued','sent')", (id,)).fetchall()
            if len(active) >= 16 or any(selected.intersection(json.loads(row[0])) for row in active):
                raise ValueError("Дождитесь результата предыдущей команды для воркера.")
            if db.execute("SELECT COUNT(*) FROM commands WHERE node=?", (id,)).fetchone()[0] >= 512:
                raise ValueError("Достигнут предел недавних команд узла.")
            db.execute("INSERT INTO commands VALUES (?,?,?,?,?,?,?)",
                       (ident, id, action, _json(ids), "queued", now, now + COMMAND_SECONDS))
        return {"id": ident, "status": "queued"}

    def revoke(self, id, *, now=None):
        if not _matches(NODE_ID, id):
            raise ValueError("Нет такого узла.")
        with self.write() as db:
            if not db.execute("UPDATE nodes SET revoked=COALESCE(revoked,?), invite=NULL, workers='[]' WHERE id=?",
                              (_now(now), id)).rowcount:
                raise ValueError("Нет такого узла.")
            db.execute("UPDATE commands SET status='unknown' WHERE node=? AND status IN ('queued','sent')", (id,))
        return {"ok": True}

    def list(self, *, now=None):
        now = _now(now)
        with self.write() as db:
            self._expire(db, now)
            result = []
            for node in db.execute("SELECT * FROM nodes ORDER BY label, id"):
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
                result.append(dict(id=node["id"], label=node["label"], status=status, last_seen=seen,
                                   workers=json.loads(node["workers"]), commands=commands))
            return {"nodes": result}


class Client(_Database):
    """Fixed-origin outbound client; execute is the local start/stop authority."""

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
        super().__init__(state_file)
        self.id, self._key = id, key
        self._scheme, self._host, self._port = parts.scheme, parts.hostname, port
        self._context = ssl.create_default_context() if self._scheme == "https" else None
        binding = _digest(b"client", origin + "\n" + id + "\n" + key)
        with self.write() as db:
            db.execute("CREATE TABLE IF NOT EXISTS client_binding (id INTEGER PRIMARY KEY CHECK(id=1), binding TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO client_binding VALUES (1,?)", (binding,))
            if db.execute("SELECT binding FROM client_binding WHERE id=1").fetchone()[0] != binding:
                raise ValueError("Журнал привязан к другой регистрации узла.")
            db.execute("CREATE TABLE IF NOT EXISTS claims (id TEXT PRIMARY KEY, status TEXT NOT NULL, "
                       "expires INTEGER NOT NULL, reported INTEGER NOT NULL DEFAULT 0)")

    def _post(self, route, data):
        if route not in {"/api/node/pair", "/api/node/poll"}:
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

    def cycle(self, snapshot, execute, *, now=None):
        report = _workers(snapshot)
        with self.write() as db:
            db.execute("DELETE FROM claims WHERE expires<?", (_now(now) - 600,))
            acks = [dict(row) for row in db.execute("SELECT id,status FROM claims WHERE reported=0 ORDER BY expires LIMIT 32")]
        response = self._post("/api/node/poll", {"id": self.id, "key": self._key, "workers": list(report.values()), "acks": acks})
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
            if not set(item["workers"]) <= report.keys() or _now(now) >= expires:
                continue
            try:
                if execute(item["action"], list(item["workers"])) is False:
                    continue
            except (ValueError, RuntimeError, OSError, sqlite3.Error):
                continue
            with self.write() as db:
                db.execute("UPDATE claims SET status='acknowledged',reported=0 WHERE id=?", (ident,))
