"""Bounded local telemetry and event archive; no external data source."""
import math
import sqlite3
import time
from contextlib import closing

SAMPLE = 15
RETAIN = 31 * 86400
RANGES = {"30m": (1800, 15), "6h": (21600, 60), "24h": (86400, 300),
          "7d": (604800, 1800), "30d": (2592000, 7200), "31d": (RETAIN, 7200)}
FIELDS = ("rate", "watts", "edge", "hot", "hbm", "active", "efficiency")
LIMIT_FIELDS = ("edge_limit", "hot_limit", "hbm_limit")


def value(number):
    return number if type(number) in (int, float) and math.isfinite(number) and number >= 0 else None


def observations(rows):
    active = [r for r in rows if r["state"] == "running"]
    rates = [value(r.get("rate_hs")) for r in active]
    rate = sum(rates) / 1e6 if None not in rates and rows else None
    devices = [r for r in rows if r.get("device")]
    power = [value(r.get("watts")) for r in devices]
    watts = sum(power) if power and None not in power else None

    def sample(name, aggregate):
        got = [number for r in devices if (number := value(r.get(name))) is not None]
        return aggregate(got) if got else None

    def avg(entries):
        return sum(entries) / len(entries)

    return (rate, watts, sample("edge", avg), sample("hot", max), sample("hbm", avg),
            len(active), rate / watts if rate is not None and rate > 0 and watts
            and all(r.get("device") for r in active) else None)


class Archive:
    def __init__(self, path):
        self.path = path
        with closing(self.connect()) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS trend (slot INTEGER PRIMARY KEY, t INTEGER,"
                       "rate REAL, watts REAL, edge REAL, hot REAL, hbm REAL, active INTEGER, efficiency REAL)")
            db.execute("CREATE TABLE IF NOT EXISTS event (id INTEGER PRIMARY KEY, t INTEGER,"
                       "kind TEXT, worker TEXT, detail TEXT, status TEXT)")
            db.execute("CREATE INDEX IF NOT EXISTS event_time ON event(t)")
            db.execute("CREATE INDEX IF NOT EXISTS event_kind ON event(kind,id)")
            db.execute("CREATE TABLE IF NOT EXISTS threshold (t INTEGER PRIMARY KEY,"
                       "edge_limit INTEGER NOT NULL, hot_limit INTEGER NOT NULL, hbm_limit INTEGER)")
            db.execute("CREATE TABLE IF NOT EXISTS host_boot (slot INTEGER PRIMARY KEY CHECK (slot=1),"
                       "boot_id TEXT NOT NULL, observed_at INTEGER NOT NULL)")

    def connect(self):
        return sqlite3.connect(self.path, timeout=4)

    @staticmethod
    def _record_sample(db, at, sample):
        db.execute("INSERT OR IGNORE INTO trend VALUES (?,?,?,?,?,?,?,?,?)", (at // SAMPLE, at, *sample))
        db.execute("DELETE FROM trend WHERE slot<?", ((at - RETAIN) // SAMPLE,))

    def tick(self, at, workers, events, *, limits=None):
        cutoff = at - RETAIN
        with closing(self.connect()) as db, db:
            self._record_sample(db, at, observations(workers))
            if limits is not None:
                settings = tuple(limits[key] for key in LIMIT_FIELDS)
                previous = db.execute("SELECT edge_limit, hot_limit, hbm_limit FROM threshold"
                                      " WHERE t<=? ORDER BY t DESC LIMIT 1", (at,)).fetchone()
                if settings != previous:
                    db.execute("INSERT OR REPLACE INTO threshold VALUES (?,?,?,?)", (at, *settings))
            # Keep the settings at the retention boundary as well as newer changes.
            db.execute("DELETE FROM threshold WHERE t<(SELECT MAX(t) FROM threshold WHERE t<=?)", (cutoff,))
            self._record_events(db, cutoff, events)

    def farm_sample(self, at, overview):
        """Record a farm observation only when every configured source is online."""
        nodes = overview["nodes"]
        with closing(self.connect()) as db, db:
            if not nodes["total"] or nodes["online"] != nodes["total"]:
                db.execute("DELETE FROM trend WHERE slot<?", ((at - RETAIN) // SAMPLE,))
                return
            rate = overview["hashrate_hs"] / 1e6 if overview["hashrate_hs"] is not None else None
            watts = overview["watts"]
            temperatures = overview["temperatures"]
            self._record_sample(db, at, (rate, watts, temperatures["edge"], temperatures["hot"],
                                         temperatures["hbm"], overview["workers"]["running"],
                                         rate / watts if rate is not None and watts else None))

    def has_samples(self):
        with closing(self.connect()) as db:
            return db.execute("SELECT 1 FROM trend LIMIT 1").fetchone() is not None

    def note_start(self, at, *, boot_id=None, reboot_at=None):
        identity = boot_id.strip() if isinstance(boot_id, str) else ""
        events = []
        with closing(self.connect()) as db, db:
            # Read and replace the boot identity under one write transaction.
            db.execute("BEGIN IMMEDIATE")
            if 0 < len(identity) <= 128:
                previous = db.execute("SELECT boot_id,observed_at FROM host_boot WHERE slot=1").fetchone()
                if previous is not None and identity != previous[0]:
                    when = reboot_at if type(reboot_at) is int and previous[1] < reboot_at <= at else at
                    events.append({"time": when, "type": "host-reboot", "worker": "",
                                   "detail": "Зафиксирована перезагрузка Linux.", "status": "observed"})
                db.execute("INSERT OR REPLACE INTO host_boot VALUES (1,?,?)", (identity, at))
            events.append({"time": at, "type": "service-start", "worker": "",
                           "detail": "Панель запущена.", "status": "started"})
            self._record_events(db, at - RETAIN, events)

    @staticmethod
    def _record_events(db, cutoff, events):
        for event in events:
            db.execute("INSERT INTO event(t,kind,worker,detail,status) VALUES (?,?,?,?,?)",
                       (event["time"], event["type"], str(event["worker"] or "")[:32],
                        str(event["detail"])[:200], str(event["status"])[:40]))
        db.execute("DELETE FROM event WHERE t<? OR id<(SELECT MAX(id)-4999 FROM event)", (cutoff,))

    @staticmethod
    def pack(rows):
        return [dict(zip(("id", "time", "type", "worker", "detail", "status"), row)) for row in rows]

    def events(self, kind=None):
        with closing(self.connect()) as db:
            if kind is None:
                return self.pack(db.execute("SELECT * FROM event ORDER BY id DESC LIMIT 200"))
            return self.pack(db.execute("SELECT * FROM event WHERE kind=? ORDER BY id DESC LIMIT 200", (kind,)))

    def recent_rate_hs(self, *, now=None):
        at = int(time.time()) if now is None else int(now)
        start = at - 1800
        with closing(self.connect()) as db:
            rate, count, last = db.execute(
                "SELECT AVG(rate), COUNT(rate), MAX(t) FROM trend WHERE slot BETWEEN ? AND ?"
                " AND t BETWEEN ? AND ? AND rate IS NOT NULL", (start // SAMPLE, at // SAMPLE, start, at)
            ).fetchone()
        if count >= 90 and at - last <= 45:
            return rate * 1e6
        return None

    def history(self, period, *, now=None):
        if period not in RANGES:
            raise ValueError("Неизвестный период.")
        width, step = RANGES[period]
        at = int(time.time()) if now is None else int(now)
        start = at - width
        with closing(self.connect()) as db, db:
            db.execute("BEGIN")
            where = "slot BETWEEN ? AND ? AND t BETWEEN ? AND ?"
            bounds = (start // SAMPLE, at // SAMPLE, start, at)
            count = db.execute("SELECT COUNT(*) FROM trend WHERE " + where, bounds).fetchone()[0]
            fields = ",".join(f"AVG({field})" for field in FIELDS)
            rows = db.execute("SELECT (t/?)*? AS bucket," + fields + ",COUNT(*) FROM trend WHERE " + where
                              + " GROUP BY bucket ORDER BY bucket", (step, step, *bounds)).fetchall()
            incidents = self.pack(db.execute("SELECT * FROM event WHERE kind='thermal' AND t BETWEEN ? AND ?"
                                             " ORDER BY id DESC LIMIT 150", (start, at)))
            markers = self.pack(db.execute(
                "SELECT * FROM event WHERE t BETWEEN ? AND ? AND"
                " (kind IN ('thermal','host-reboot','service-start') OR (kind='exit' AND status='failed'))"
                " ORDER BY t DESC,id DESC LIMIT 150", (start, at)))
            thresholds = db.execute("SELECT t,edge_limit,hot_limit,hbm_limit FROM threshold WHERE"
                                    " t>=COALESCE((SELECT MAX(t) FROM threshold WHERE t<=?),?) AND t<=?"
                                    " ORDER BY t", (start, start, at)).fetchall()
        return {"range": period, "from": start, "to": at, "step": step, "count": count,
                "thresholds": [dict(zip(("t", *LIMIT_FIELDS), row)) for row in thresholds],
                "series": [{"t": max(row[0], start),
                            **dict(zip(FIELDS, (round(v, 4) if v is not None else None for v in row[1:-1]))),
                            "samples": row[-1]} for row in rows], "incidents": incidents, "markers": markers}
