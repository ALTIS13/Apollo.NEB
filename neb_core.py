"""Configuration, local admin sessions and DevFee time accounting."""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import tempfile
import time
from contextlib import closing, contextmanager
from pathlib import Path
from urllib.parse import urlencode, urlsplit


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
        return "support" if running and self.credit.get(id_, 0.) + 1e-7 >= self.window else None

    def checkpoint(self, now):
        if self.cfg["enabled"] and now - self.flushed_at >= 60:
            self.flush()
            self.flushed_at = now

    def fund(self, id_, now):
        previous = self.credit[id_]
        self.credit[id_] = max(0., previous - self.window)
        try:
            self.flush()
        except OSError:
            self.credit[id_] = previous
            raise
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
    ENROLLMENT_SECONDS = 300
    LIMIT_ATTEMPTS = 5
    ATTEMPT_SECONDS = 60
    RECOVERY_COUNT = 8
    BAD_CREDENTIALS = "Пароль или код не принят."
    NO_PENDING = "Привязка недоступна. Начните её заново."

    def __init__(self, config, path):
        self.config, self.path = config, path
        with self.write() as db:
            db.execute("CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, expires INTEGER NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS auth (id INTEGER PRIMARY KEY CHECK (id=1), "
                       "last_tick INTEGER NOT NULL, totp_secret TEXT)")
            legacy = not any(row[1] == "totp_secret" for row in db.execute("PRAGMA table_info(auth)"))
            if legacy:
                db.execute("ALTER TABLE auth ADD COLUMN totp_secret TEXT")
            present = db.execute("SELECT 1 FROM auth WHERE id=1").fetchone()
            if legacy or not present:
                secret = self._validated_secret(config.get("totp"))
                if present:
                    db.execute("UPDATE auth SET totp_secret=? WHERE id=1", (secret,))
                else:
                    db.execute("INSERT INTO auth (id,last_tick,totp_secret) VALUES (1,-1,?)", (secret,))
            db.execute("CREATE TABLE IF NOT EXISTS auth_pending (id INTEGER PRIMARY KEY CHECK (id=1), "
                       "session TEXT NOT NULL REFERENCES sessions(token) ON DELETE CASCADE, "
                       "secret TEXT NOT NULL, expires INTEGER NOT NULL, failures INTEGER NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS auth_failures (attempted_at INTEGER NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS auth_recovery (digest TEXT PRIMARY KEY)")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=3)
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA secure_delete=ON")
        return db

    @contextmanager
    def write(self):
        # One database-wide serialization boundary even across Auth instances.
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    @staticmethod
    def fingerprint(token):
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    @staticmethod
    def _now(now):
        return int(time.time() if now is None else now)

    @staticmethod
    def _valid_token(token):
        return isinstance(token, str) and re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is not None

    @staticmethod
    def _validated_secret(secret):
        if secret is not None and (not isinstance(secret, str) or not re.fullmatch(r"[A-Z2-7]{32}", secret)):
            raise ValueError("Некорректный ключ TOTP.")
        return secret

    def _require_session(self, db, token, now):
        if not self._valid_token(token) or not self._valid_session(db, token, now):
            raise ValueError("Войдите в панель.")
        return self.fingerprint(token)

    def _valid_session(self, db, token, now):
        return db.execute("SELECT 1 FROM sessions WHERE token=? AND expires>?",
                          (self.fingerprint(token), now)).fetchone() is not None

    def _check_limit(self, db, now):
        db.execute("DELETE FROM auth_failures WHERE attempted_at<=?", (now - self.ATTEMPT_SECONDS,))
        if db.execute("SELECT COUNT(*) FROM auth_failures").fetchone()[0] >= self.LIMIT_ATTEMPTS:
            raise ValueError("Слишком много попыток, повторите через минуту.")

    @staticmethod
    def _failure(db, now):
        db.execute("INSERT INTO auth_failures VALUES (?)", (now,))

    def _password_ok(self, password):
        if not isinstance(password, str) or not 12 <= len(password) <= 256:
            return False
        try:
            computed = password_hash(password, bytes.fromhex(self.config["salt"]))
        except UnicodeError:
            return False
        return hmac.compare_digest(computed, self.config["password_hash"])

    @staticmethod
    def _secret(db):
        return db.execute("SELECT totp_secret FROM auth WHERE id=1").fetchone()[0]

    @staticmethod
    def _tick(secret, code, now):
        if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
            return None
        return next((t for t in (now // 30, now // 30 - 1)
                     if t >= 0 and hmac.compare_digest(totp(secret, t * 30), code)), None)

    @staticmethod
    def _recovery_digest(code):
        if not isinstance(code, str) or len(code) > 64:
            return None
        code = code.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{8}){3}", code):
            return None
        return hashlib.sha256(b"Apollo.NEB recovery v1\0" + code.encode("ascii")).hexdigest()

    def _factor_ok(self, db, secret, code, now):
        tick = self._tick(secret, code, now)
        if tick is not None:
            return db.execute("UPDATE auth SET last_tick=? WHERE id=1 AND last_tick<?", (tick, tick)).rowcount == 1
        digest = self._recovery_digest(code)
        return digest is not None and db.execute("DELETE FROM auth_recovery WHERE digest=?", (digest,)).rowcount == 1

    def _session(self, db, now, *, rotate=False):
        token = secrets.token_urlsafe(32)
        if rotate:
            db.execute("DELETE FROM sessions")
        else:
            db.execute("DELETE FROM sessions WHERE expires<=?", (now,))
        db.execute("INSERT INTO sessions VALUES (?,?)", (self.fingerprint(token), now + SESSION_SECONDS))
        db.execute("DELETE FROM auth_failures")
        return token

    def is_totp_enabled(self):
        with closing(self.connect()) as db:
            return self._secret(db) is not None

    def enter(self, password, code, *, now=None):
        now = self._now(now)
        with self.write() as db:
            self._check_limit(db, now)
            secret = self._secret(db)
            if self._password_ok(password) and (secret is None or self._factor_ok(db, secret, code, now)):
                return self._session(db, now)
            self._failure(db, now)
        raise ValueError(self.BAD_CREDENTIALS)

    def begin_enrollment(self, token, password, *, now=None):
        now = self._now(now)
        with self.write() as db:
            session = self._require_session(db, token, now)
            if self._secret(db) is not None:
                raise ValueError("TOTP уже подключён.")
            self._check_limit(db, now)
            if self._password_ok(password):
                secret = base64.b32encode(secrets.token_bytes(20)).decode("ascii")
                expires = min(now + self.ENROLLMENT_SECONDS, db.execute(
                    "SELECT expires FROM sessions WHERE token=?", (session,)).fetchone()[0])
                db.execute("DELETE FROM auth_pending")
                db.execute("INSERT INTO auth_pending VALUES (1,?,?,?,0)", (session, secret, expires))
                query = urlencode(dict(secret=secret, issuer="Apollo.NEB", algorithm="SHA1", digits=6, period=30))
                return {"secret": secret, "uri": "otpauth://totp/Apollo.NEB:admin?" + query,
                        "expires_in": expires - now}
            self._failure(db, now)
        raise ValueError(self.BAD_CREDENTIALS)

    def confirm_enrollment(self, token, code, *, now=None):
        now = self._now(now)
        failure = self.NO_PENDING
        with self.write() as db:
            session = self._require_session(db, token, now)
            db.execute("DELETE FROM auth_pending WHERE expires<=?", (now,))
            pending = db.execute("SELECT secret, failures FROM auth_pending WHERE id=1 AND session=?", (session,)).fetchone()
            if pending is not None and self._secret(db) is None:
                self._check_limit(db, now)
                secret, failures = pending
                tick = self._tick(secret, code, now)
                if tick is None:
                    self._failure(db, now)
                    if failures + 1 >= self.LIMIT_ATTEMPTS:
                        db.execute("DELETE FROM auth_pending")
                    else:
                        db.execute("UPDATE auth_pending SET failures=failures+1 WHERE id=1")
                    failure = self.BAD_CREDENTIALS
                else:
                    codes = []
                    db.execute("DELETE FROM auth_recovery")
                    for _ in range(self.RECOVERY_COUNT):
                        value = secrets.token_hex(16)
                        recovery = "-".join(value[i:i + 8] for i in range(0, 32, 8))
                        db.execute("INSERT INTO auth_recovery VALUES (?)", (self._recovery_digest(recovery),))
                        codes.append(recovery)
                    db.execute("UPDATE auth SET totp_secret=?, last_tick=? WHERE id=1", (secret, tick))
                    db.execute("DELETE FROM auth_pending")
                    return self._session(db, now, rotate=True), codes
        raise ValueError(failure)

    def disable_enrollment(self, token, password, code, *, now=None):
        now = self._now(now)
        with self.write() as db:
            self._require_session(db, token, now)
            self._check_limit(db, now)
            secret = self._secret(db)
            if secret is None:
                raise ValueError("TOTP не подключён.")
            if self._password_ok(password) and self._factor_ok(db, secret, code, now):
                db.execute("UPDATE auth SET totp_secret=NULL,last_tick=-1 WHERE id=1")
                db.execute("DELETE FROM auth_pending")
                db.execute("DELETE FROM auth_recovery")
                return self._session(db, now, rotate=True)
            self._failure(db, now)
        raise ValueError(self.BAD_CREDENTIALS)

    def reset_after_password_change(self, new_secret=None):
        """Called only by the offline CLI while holding the state-directory lock."""
        new_secret = self._validated_secret(new_secret)
        with self.write() as db:
            if new_secret is not None:
                if self._secret(db) is not None:
                    raise ValueError("TOTP уже подключён: смена пароля не заменяет второй фактор.")
                db.execute("UPDATE auth SET totp_secret=?,last_tick=-1 WHERE id=1", (new_secret,))
                db.execute("DELETE FROM auth_recovery")
            db.execute("DELETE FROM sessions")
            db.execute("DELETE FROM auth_pending")
            db.execute("DELETE FROM auth_failures")

    def valid(self, token, *, now=None):
        if not self._valid_token(token):
            return False
        with closing(self.connect()) as db:
            return self._valid_session(db, token, self._now(now))

    def revoke(self, token):
        if self._valid_token(token):
            with self.write() as db:
                db.execute("DELETE FROM sessions WHERE token=?", (self.fingerprint(token),))
