"""Project an aggregate-only overview from Portable telemetry and its node registry."""

from neb_fleet import ONLINE_SECONDS
from neb_history import observations, value


TEMPERATURES = ("edge", "hot", "hbm")
SHARES = ("accepted", "rejected")
READINGS = ("rate_hs", "watts", *TEMPERATURES, *SHARES)
ATTENTION = {"failed", "blocked", "protection", "unavailable"}


def _stamp(raw):
    return raw if type(raw) is int and raw > 0 else 0


def _fresh(stamp, now):
    return stamp > 0 and (now is None or 0 <= now - stamp < ONLINE_SECONDS)


def _registered(fleet):
    nodes = fleet.get("nodes") if isinstance(fleet, dict) else None
    registered = {}
    for node in nodes if isinstance(nodes, list) else []:
        if not isinstance(node, dict) or node.get("status") not in ("online", "offline"):
            continue
        ident = node.get("id")
        if not isinstance(ident, str) or not ident:
            continue
        previous = registered.get(ident)
        if previous is None or _stamp(node.get("last_seen")) >= _stamp(previous.get("last_seen")):
            registered[ident] = node
    return registered.values()


def _measurement(raw):
    try:
        return value(raw)
    except OverflowError:
        return None


def _rows(sources):
    if not sources:
        return None
    rows = []
    for _, sample, inventory in sources:
        workers = sample.get("workers")
        if not isinstance(workers, list):
            return None
        for worker in workers:
            if not isinstance(worker, dict) or not isinstance(worker.get("state"), str):
                return None
            readings = {key: _measurement(worker.get(key)) for key in READINGS}
            # A legacy node can report sensors without sharing its device inventory.
            device = bool(worker.get("device")) or not inventory or any(
                readings[key] is not None for key in ("watts", *TEMPERATURES)
            )
            rows.append({"state": worker["state"], "device": device, **readings})
    return rows


def _complete(rows, name, combine):
    readings = [row[name] for row in rows]
    return _measurement(combine(readings)) if readings and None not in readings else None


def summarize(local, fleet, *, now=None, atlas=None):
    """Return live known coverage and complete readings, with no wall-clock reads.

    Fleet.list() already expires remote status. When supplied, now also bounds
    measurement age for both sources instead of accepting any positive sample time.
    """
    registered = _registered(fleet)
    capabilities = local.get("capabilities") if isinstance(local, dict) else None
    has_local = isinstance(local, dict) and "time" in local and not (
        isinstance(capabilities, dict) and capabilities.get("local_workers") is False
    )
    sources = []
    if has_local and _fresh(stamp := _stamp(local["time"]), now):
        sources.append((stamp, local, True))
    for node in registered:
        if node["status"] == "online" and _fresh(stamp := _stamp(node.get("last_seen")), now):
            sources.append((stamp, node, isinstance(node.get("details"), dict)))

    projected = _rows(sources)
    rows = projected if projected is not None else []
    active = [row for row in rows if row["state"] == "running"]
    devices = [row for row in rows if row["device"]]
    monitored = []
    for _, sample, _ in sources:
        hardware = sample.get("hardware")
        if isinstance(hardware, dict) and isinstance(hardware.get("devices"), list):
            monitored.extend({key: _measurement(device.get(key)) for key in ("watts", "edge")}
                             for device in hardware["devices"] if isinstance(device, dict))
    rate, *_ = observations(rows)
    temperatures = {key: _complete(devices + monitored if key == "edge" else devices, key, max)
                    for key in TEMPERATURES}
    readings = {
        "time": max((stamp for stamp, _, _ in sources), default=None),
        "nodes": {"online": len(sources), "total": len(registered) + int(has_local)},
        "workers": {
            "running": len(active) if projected is not None else None,
            "total": len(rows) if projected is not None else None,
            "attention": sum(row["state"] in ATTENTION for row in rows) if projected is not None else None,
        },
        "hashrate_hs": _measurement(rate * 1e6) if rate is not None else None,
        "watts": _complete(devices + monitored, "watts", sum),
        "temperatures": temperatures,
        "shares": {key: _complete(active, key, sum) for key in SHARES},
    }
    if atlas is not None:
        readings["nodes"]["total"] += 1
        if atlas["status"] == "online" and _fresh(_stamp(atlas["time"]), now):
            _include_atlas(readings, atlas, measured=projected is not None or not sources,
                           worker_devices=bool(devices or monitored))
    return readings


def _combined(value, extra, *, present):
    if not present:
        return _measurement(extra)
    return _measurement(value + extra) if value is not None and extra is not None else None


def _include_atlas(readings, atlas, *, measured, worker_devices):
    readings["nodes"]["online"] += 1
    readings["time"] = max(readings["time"] or 0, atlas["time"])
    counts = readings["atlas_gpus"] = atlas["gpu_counts"]
    readings["hashrate_hs"] = _combined(readings["hashrate_hs"], atlas["rate_hs"],
                                        present=not measured or bool(readings["workers"]["total"]))
    if counts["total"]:
        readings["watts"] = _combined(readings["watts"], atlas["watts"], present=worker_devices)
    for name, extra in atlas["temperatures"].items():
        base = readings["temperatures"][name]
        if extra is not None:
            readings["temperatures"][name] = max(base, extra) if base is not None else extra
    if counts["active"]:
        for name, extra in atlas["shares"].items():
            readings["shares"][name] = _combined(readings["shares"][name], extra,
                                                  present=not measured or bool(readings["workers"]["running"]))
