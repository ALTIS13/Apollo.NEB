"""Bounded local telemetry and event archive; no external data source."""
import math
import sqlite3
import time
from contextlib import closing

SAMPLE = 15
RETAIN = 31 * 86400
RANGES = {"30m": (1800, 15), "6h": (21600, 60), "24h": (86400, 300),
          "7d": (604800, 1800), "30d": (2592000, 7200)}
FIELDS = ("rate", "watts", "edge", "hot", "hbm", "active", "efficiency")


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

    def connect(self):
        return sqlite3.connect(self.path, timeout=4)

    def tick(self, at, workers, events):
        with closing(self.connect()) as db, db:
            db.execute("INSERT OR IGNORE INTO trend VALUES (?,?,?,?,?,?,?,?,?)",
                       (at // SAMPLE, at, *observations(workers)))
            db.execute("DELETE FROM trend WHERE slot<?", ((at - RETAIN) // SAMPLE,))
            for event in events:
                db.execute("INSERT INTO event(t,kind,worker,detail,status) VALUES (?,?,?,?,?)",
                           (event["time"], event["type"], str(event["worker"] or "")[:32],
                            str(event["detail"])[:200], str(event["status"])[:40]))
            if events:
                db.execute("DELETE FROM event WHERE t<? OR id<(SELECT MAX(id)-4999 FROM event)", (at - RETAIN,))

    @staticmethod
    def pack(rows):
        return [dict(zip(("id", "time", "type", "worker", "detail", "status"), row)) for row in rows]

    def events(self):
        with closing(self.connect()) as db:
            return self.pack(db.execute("SELECT * FROM event ORDER BY id DESC LIMIT 200"))

    def history(self, period, *, now=None):
        if period not in RANGES:
            raise ValueError("Неизвестный период.")
        width, step = RANGES[period]
        at = int(time.time()) if now is None else int(now)
        start = at - width
        with closing(self.connect()) as db:
            where = "slot BETWEEN ? AND ? AND t BETWEEN ? AND ?"
            bounds = (start // SAMPLE, at // SAMPLE, start, at)
            count = db.execute("SELECT COUNT(*) FROM trend WHERE " + where, bounds).fetchone()[0]
            fields = ",".join(f"AVG({field})" for field in FIELDS)
            rows = db.execute("SELECT (t/?)*? AS bucket," + fields + ",COUNT(*) FROM trend WHERE " + where
                              + " GROUP BY bucket ORDER BY bucket", (step, step, *bounds)).fetchall()
            incidents = self.pack(db.execute("SELECT * FROM event WHERE kind='thermal' AND t BETWEEN ? AND ?"
                                             " ORDER BY id DESC LIMIT 150", (start, at)))
        return {"range": period, "from": start, "to": at, "step": step, "count": count,
                "series": [{"t": max(row[0], start),
                            **dict(zip(FIELDS, (round(v, 4) if v is not None else None for v in row[1:-1]))),
                            "samples": row[-1]} for row in rows], "incidents": incidents}
