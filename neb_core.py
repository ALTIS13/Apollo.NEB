"""Configuration, local admin sessions and opt-in support accounting."""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit


B32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
SESSION_SECONDS = 12 * 3600
LIMITS = {"edge_limit": 80, "hot_limit": 100, "hbm_limit": None}
POOL_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
DEFAULT_POOLS = [{"id": "suprnova-eu", "label": "Suprnova · EU · TLS",
                  "url": "stratum+ssl://neb.suprnova.cc:7806"}]


def read(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def store(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix="." + path.name + ".",
                                     mode="w", encoding="utf-8", delete=False) as out:
        temp = Path(out.name)
        try:
            with out:
                json.dump(value, out, ensure_ascii=False, allow_nan=False, indent=2)
                out.write("\n")
                out.flush()
                os.fsync(out.fileno())
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)


def wallet(raw):
    """Validate the mainnet NEB Bech32m address; signing keys are not inputs."""
    if not isinstance(raw, str) or len(raw) != 62 or not raw.startswith("neb1") or any(c not in B32 for c in raw[4:]):
        raise ValueError("Требуется публичный адрес NEB Mainnet (neb1…).")
    values = [B32.index(c) for c in raw[4:]]
    check = 1
    for value in [3, 3, 3, 0, 14, 5, 2] + values:
        top = check >> 25
        check = ((check & 0x1ffffff) << 5) ^ value
        for bit, generator in enumerate((0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3)):
            if (top >> bit) & 1:
                check ^= generator
    if check != 0x2bc830a3 or values[-7] & 15:
        raise ValueError("Контрольная сумма адреса NEB не совпадает.")
    return raw


def pool_url(value):
    if not isinstance(value, str) or len(value) > 256 or not re.fullmatch(r"stratum\+(?:ssl|tcp)://[A-Za-z0-9.\-\[\]:]+", value):
        raise ValueError("Укажите адрес пула stratum+ssl://хост:порт без данных входа.")
    parsed = urlsplit(value)
    if not parsed.hostname or not parsed.port:
        raise ValueError("Нужен хост и порт пула.")
    return value


def pools(raw):
    if not isinstance(raw, list) or not 1 <= len(raw) <= 20:
        raise ValueError("Укажите от одного до двадцати пулов.")
    result = {}
    for row in raw:
        if not isinstance(row, dict):
            raise ValueError("Неверная запись пула.")
        id_, label = row.get("id"), row.get("label")
        if (not isinstance(id_, str) or not POOL_ID.fullmatch(id_) or id_ in result
                or not isinstance(label, str) or not 1 <= len(label) <= 80 or not label.isprintable()):
            raise ValueError("Пулу нужны уникальный id и короткая подпись.")
        result[id_] = {"id": id_, "label": label, "url": pool_url(row.get("url"))}
    return result


def chosen_profile(data, pool_ids, *, allow_empty=False):
    if not isinstance(data, dict):
        raise ValueError("Профиль должен быть объектом.")
    worker, pool, address = (data.get(k) for k in ("worker", "pool", "wallet"))
    if not isinstance(worker, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,24}", worker):
        raise ValueError("Имя воркера: 1–24 латинских буквы, цифры, дефисы или подчёркивания.")
    if not isinstance(pool, str) or pool not in pool_ids:
        raise ValueError("Пул не настроен на этом сервере.")
    return {"wallet": "" if allow_empty and address == "" else wallet(address), "worker": worker, "pool": pool}


def limits(data):
    if not isinstance(data, dict):
        raise ValueError("Некорректные пороги защиты.")
    edge, hot, hbm = (data.get(k) for k in LIMITS)
    if (type(edge) is not int or not 55 <= edge <= 80 or type(hot) is not int or not 65 <= hot <= 100
            or edge > hot or (hbm is not None and (type(hbm) is not int or not 60 <= hbm <= 90))):
        raise ValueError("Пределы: ядро 55–80 °C, Hotspot 65–100 °C, HBM2 60–90 °C или выключено.")
    return {"edge_limit": edge, "hot_limit": hot, "hbm_limit": hbm}


def support_config(data):
    if data is None or data == {"enabled": False}:
        return {"enabled": False, "wallet": None, "basis_points": 0}
    if not isinstance(data, dict) or data.get("enabled") is not True:
        raise ValueError("Сначала подтвердите участие явно.")
    bps = data.get("basis_points")
    if type(bps) is not int or not 1 <= bps <= 100:
        raise ValueError("Поддержка автора может занимать не более 1% рабочего времени.")
    return {"enabled": True, "wallet": wallet(data.get("wallet")), "basis_points": bps}


class SupportClock:
    """Reserve a whole support window before switching. Never replay after a crash."""

    def __init__(self, config, path, *, window=300):
        self.cfg, self.path, self.window = config, path, window
        state = read(path, {}) if config["enabled"] else {}
        self.credit = state.get("credit", {}) if state.get("config") == config else {}
        self.previous, self.deadline = {}, {}
        self.flushed_at = 0.

    @property
    def exchange(self):
        return self.cfg["basis_points"] / (10000 - self.cfg["basis_points"])

    def active(self):
        return sorted(self.deadline)

    def flush(self):
        if self.cfg["enabled"]:
            store(self.path, {"config": self.cfg, "credit": self.credit})

    def observe(self, id_, running, now):
        if not self.cfg["enabled"]:
            return None
        last, was_running = self.previous.get(id_, (now, False))
        self.previous[id_] = (now, running)
        if id_ in self.deadline:
            return "operator" if now >= self.deadline[id_] else None
        if running and was_running and 0 <= now - last <= 5:
            self.credit[id_] = min(2 * self.window, self.credit.get(id_, 0.) + (now - last) * self.exchange)
        if now - self.flushed_at >= 60:
            self.flush()
            self.flushed_at = now
        return "support" if running and self.credit.get(id_, 0.) + 1e-7 >= self.window else None

    def fund(self, id_, now):
        self.credit[id_] = max(0., self.credit[id_] - self.window)
        self.flush()
        self.deadline[id_] = now + self.window

    def finish(self, id_):
        self.deadline.pop(id_, None)
        self.previous.pop(id_, None)


def totp(secret, now):
    counter = int(now // 30).to_bytes(8, "big")
    digest = hmac.digest(base64.b32decode(secret), counter, "sha1")
    offset = digest[-1] & 15
    return f"{(int.from_bytes(digest[offset:offset + 4], 'big') & 0x7fffffff) % 1000000:06d}"


def password_hash(password, salt):
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1).hex()


def new_login(password, secret=None):
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise ValueError("Пароль должен содержать 12–256 символов.")
    salt = secrets.token_bytes(16)
    return {"salt": salt.hex(), "password_hash": password_hash(password, salt), "totp": secret}


class Auth:
    def __init__(self, config, path):
        self.config, self.path, self.lock, self.attempts = config, path, threading.Lock(), []
        with closing(self.connect()) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, expires INTEGER NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS auth (id INTEGER PRIMARY KEY CHECK (id=1), last_tick INTEGER NOT NULL)")
            db.execute("INSERT OR IGNORE INTO auth VALUES (1,-1)")

    def connect(self):
        return sqlite3.connect(self.path, timeout=3)

    @staticmethod
    def fingerprint(token):
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    def enter(self, password, code, *, now=None):
        now = int(time.time() if now is None else now)
        with self.lock:
            self.attempts = [at for at in self.attempts if 0 <= now - at < 60]
            if len(self.attempts) >= 5:
                raise ValueError("Слишком много попыток, повторите через минуту.")
            self.attempts.append(now)
            error = ValueError("Пароль или код не принят.")
            if not isinstance(password, str) or len(password) > 256:
                raise error
            computed = password_hash(password, bytes.fromhex(self.config["salt"]))
            if not hmac.compare_digest(computed, self.config["password_hash"]):
                raise error
            secret = self.config["totp"]
            with closing(self.connect()) as db, db:
                if secret is not None:
                    if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
                        raise error
                    tick = next((t for t in (now // 30, now // 30 - 1)
                                 if t >= 0 and hmac.compare_digest(totp(secret, t * 30), code)), None)
                    if tick is None or not db.execute(
                        "UPDATE auth SET last_tick=? WHERE id=1 AND last_tick<?", (tick, tick)
                    ).rowcount:
                        raise error
                token = secrets.token_urlsafe(32)
                db.execute("DELETE FROM sessions WHERE expires<=?", (now,))
                db.execute("INSERT INTO sessions VALUES (?,?)", (self.fingerprint(token), now + SESSION_SECONDS))
                self.attempts.clear()
                return token

    def valid(self, token, *, now=None):
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            return False
        now = int(time.time() if now is None else now)
        with closing(self.connect()) as db:
            return db.execute("SELECT 1 FROM sessions WHERE token=? AND expires>?",
                              (self.fingerprint(token), now)).fetchone() is not None

    def revoke(self, token):
        if isinstance(token, str) and re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            with closing(self.connect()) as db, db:
                db.execute("DELETE FROM sessions WHERE token=?", (self.fingerprint(token),))
