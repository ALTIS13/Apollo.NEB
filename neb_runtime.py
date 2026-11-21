"""Rootless Linux worker supervisor. No driver, power, or system configuration writes."""

from __future__ import annotations

from collections import deque
import csv
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit


class Refusal(ValueError):
    """Safe diagnostic: never include the supplied path, URL, or address."""


PCI_PATH = Path("/sys/bus/pci/devices")
PCI = re.compile(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]")
SLUG = re.compile(r"[A-Za-z0-9_-]{1,32}")
UUID = re.compile(r"GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
WALLET = re.compile(r"neb1[023456789acdefghjklmnpqrstuvwxyz]{58}")
CUSTOM_ADDRESS = re.compile(r"[A-Za-z0-9_-]+")
PLACEHOLDER = re.compile(r"\{(pool|wallet|worker|device|dataset)\}")
ADAPTERS = {"custom", "suprminer-opencl", "suprminer-cuda"}
WORKER_FIELDS = {"id", "label", "adapter", "binary", "dataset", "device", "device_index", "args", "log_format"}
METRICS = ("edge", "hot", "hbm", "watts")


def _clean(value, max_length):
    return isinstance(value, str) and 0 < len(value) <= max_length and value.isprintable()


def _path(value):
    return _clean(value, 4096) and PurePosixPath(value).is_absolute()


def validate_workers(raw: list) -> list[dict]:
    if not isinstance(raw, list) or len(raw) > 64:
        raise Refusal("Expected up to 64 configured workers.")
    workers, ids, devices = [], set(), set()
    for item in raw:
        if not isinstance(item, dict) or item.keys() - WORKER_FIELDS:
            raise Refusal("Unknown or invalid worker configuration fields.")
        ident, adapter = item.get("id"), item.get("adapter")
        if not isinstance(ident, str) or not SLUG.fullmatch(ident) or ident in ids:
            raise Refusal("Worker IDs must be unique short identifiers.")
        ids.add(ident)
        if not isinstance(adapter, str) or adapter not in ADAPTERS:
            raise Refusal("Unknown worker adapter.")
        label, binary, dataset = item.get("label", ident), item.get("binary"), item.get("dataset")
        if not _clean(label, 80) or not _path(binary) or (dataset is not None and not _path(dataset)):
            raise Refusal("Provide a label and absolute Linux executable/dataset paths.")
        bdf = item.get("device")
        if bdf is not None:
            if not isinstance(bdf, str) or not PCI.fullmatch(bdf.lower()) or bdf.lower() in devices:
                raise Refusal("Each GPU worker must use a unique PCI address.")
            bdf = bdf.lower()
            devices.add(bdf)
        elif adapter != "custom":
            raise Refusal("Built-in GPU adapters require a PCI address.")
        index = item.get("device_index")
        if index is not None and (type(index) is not int or not 0 <= index <= 255 or bdf is None or adapter == "custom"):
            raise Refusal("Device index is optional and only available on built-in GPU adapters.")
        log_format = item.get("log_format", "none" if adapter == "custom" else "suprminer")
        if not isinstance(log_format, str) or log_format not in {"none", "json-lines", "suprminer"}:
            raise Refusal("Unsupported miner log format.")
        args = item.get("args", [])
        if not isinstance(args, list) or len(args) > 64 or (args and adapter != "custom"):
            raise Refusal("Only custom workers accept a literal argv list of up to 64 elements.")
        names = set()
        for arg in args:
            if not isinstance(arg, str) or len(arg) > 4096 or (arg and not arg.isprintable()):
                raise Refusal("Invalid worker argument.")
            rest = PLACEHOLDER.sub("", arg)
            if "{" in rest or "}" in rest:
                raise Refusal("Use only the documented literal argument placeholders.")
            names.update(PLACEHOLDER.findall(arg))
        if adapter == "custom" and ((bdf is None) == ("device" in names)):
            raise Refusal("A custom GPU worker must explicitly select its PCI address with {device}.")
        if adapter == "custom" and not {"wallet", "pool"} <= names:
            raise Refusal("A custom worker must pass both {wallet} and {pool} to its executable.")
        if "dataset" in names and dataset is None:
            raise Refusal("The dataset placeholder requires an explicit dataset path.")
        workers.append(dict(id=ident, label=label, adapter=adapter, binary=binary, dataset=dataset,
                            device=bdf, device_index=index, args=list(args), log_format=log_format))
    return workers


def _read(path):
    try:
        with path.open("r", encoding="ascii") as source:
            return source.read(256).strip()
    except (OSError, UnicodeError):
        return ""


def _number(value, maximum, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        result = float(value)
    except (ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and minimum <= result <= maximum else None


def _scaled(value, divisor, maximum):
    number = _number(value, maximum * divisor)
    return None if number is None else number / divisor


def _hwmon(card, expected_name):
    metrics = dict.fromkeys(METRICS)
    labels = {"edge": "edge", "junction": "hot", "hotspot": "hot", "hot spot": "hot",
              "mem": "hbm", "memory": "hbm", "hbm": "hbm"}
    for sensor in sorted(card.glob("hwmon/hwmon*")):
        if _read(sensor / "name") != expected_name:
            continue
        for label in sensor.glob("temp*_label"):
            metric = labels.get(_read(label).lower())
            if metric:
                value = _scaled(_read(label.with_name(label.name[:-5] + "input")), 1000, 160)
                if value is not None:
                    metrics[metric] = max(metrics[metric] or 0, value)
        for name in ("power1_average", "power1_input"):
            value = _scaled(_read(sensor / name), 1_000_000, 3000)
            if value is not None:
                metrics["watts"] = value
                break
    return metrics


def _nvidia_smi():
    cmd = shutil.which("nvidia-smi")
    if not cmd:
        return {}
    try:
        out = subprocess.run(
            [cmd, "--query-gpu=pci.bus_id,index,uuid,temperature.gpu,power.draw",
             "--format=csv,noheader,nounits"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=1.25, check=True, encoding="ascii",
        ).stdout
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return {}
    results, ambiguous = {}, set()
    for row in csv.reader(out.splitlines()[:256], skipinitialspace=True):
        if len(row) != 5:
            continue
        bus, index, uuid, edge, watts = (v.strip() for v in row)
        parts = bus.lower().split(":", 1)
        if len(parts) != 2 or not re.fullmatch(r"(?:[0-9a-f]{4}|0000[0-9a-f]{4})", parts[0]):
            continue
        bdf = f"{parts[0][-4:]}:{parts[1]}"
        if PCI.fullmatch(bdf) and re.fullmatch(r"[0-9]{1,3}", index) and UUID.fullmatch(uuid):
            if bdf in results:
                ambiguous.add(bdf)
            results[bdf] = dict(index=int(index), _uuid=uuid, edge=_number(edge, 160), watts=_number(watts, 3000))
    return {bdf: row for bdf, row in results.items() if bdf not in ambiguous}


def _scan_devices():
    nvidia = None
    rows = []
    for card in sorted(PCI_PATH.glob("*")):
        if not PCI.fullmatch(card.name) or not _read(card / "class").startswith("0x03"):
            continue
        vendor_id = _read(card / "vendor")
        if vendor_id not in {"0x1002", "0x10de"}:
            continue
        vendor = "amd" if vendor_id == "0x1002" else "nvidia"
        try:
            inode = card.stat().st_ino
        except OSError:
            continue
        driver = card / "driver"
        try:
            driver = driver.readlink().name
        except OSError:
            driver = None
        row = dict(device=card.name, vendor=vendor, _identity=(inode, driver),
                   **_hwmon(card, "amdgpu" if vendor == "amd" else "nvidia"))
        if vendor == "nvidia":
            if nvidia is None:
                nvidia = _nvidia_smi()
            row.update({k: v for k, v in nvidia.get(card.name, {}).items() if v is not None})
        rows.append(row)
    return rows


def discover_devices() -> list[dict]:
    """Read current hardware inventory; underscore-prefixed mapping data is private."""
    return _scan_devices()


def _limits(raw):
    if not isinstance(raw, dict):
        raise Refusal("Invalid temperature limits.")
    limits = {}
    for key, default in (("edge_limit", 80), ("hot_limit", 100), ("hbm_limit", None)):
        value = raw.get(key, default)
        if value is None and default is None:
            limits[key] = None
        elif type(value) in (int, float) and _number(value, default or 90, 1) is not None:
            limits[key] = value
        else:
            raise Refusal("Temperature limit exceeds the supported safety bounds.")
    return limits


def _thermal(card, limits, required=()):
    if not card:
        return "Configured GPU was not found."
    sensors = [("edge", "Edge", True), ("hot", "Hotspot", card["vendor"] == "amd"),
               ("hbm", "Memory", limits["hbm_limit"] is not None)]
    for metric, label, must_read in sensors:
        value = _number(card.get(metric), 160)
        limit = limits[f"{metric}_limit"]
        if (must_read or metric in required) and value is None:
            return f"{label} temperature is unavailable."
        if value is not None and limit is not None and value >= limit:
            return f"{label} temperature reached its limit."
    return None


def _profile(profile, adapter):
    if not isinstance(profile, dict):
        raise Refusal("Mining profile is invalid.")
    pool, wallet, worker = (profile.get(key) for key in ("pool", "wallet", "worker"))
    if not _clean(wallet, 128) or not (WALLET if adapter != "custom" else CUSTOM_ADDRESS).fullmatch(wallet):
        raise Refusal("Provide a public mining address.")
    if not isinstance(worker, str) or not SLUG.fullmatch(worker):
        raise Refusal("Worker name must be a short identifier.")
    if not _clean(pool, 512) or any(c.isspace() for c in pool):
        raise Refusal("Pool must be a Stratum TCP or TLS host and port without credentials.")
    try:
        url = urlsplit(pool)
        host_ok = url.hostname and re.fullmatch(r"[a-z0-9.:-]+", url.hostname)
        if not (url.scheme in {"stratum+tcp", "stratum+ssl"} and host_ok and url.port and
                not any(char in pool for char in "@?#%\\") and not url.path):
            raise ValueError
    except ValueError:
        raise Refusal("Pool must be a Stratum TCP or TLS host and port without credentials.") from None
    return {"pool": pool, "wallet": wallet, "worker": worker}


def _trusted_file(raw, executable=False):
    try:
        path = Path(raw).resolve(strict=True)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise OSError
        if executable and (info.st_uid not in {0, os.getuid()} or info.st_mode & 0o6022 or not os.access(path, os.X_OK)):
            raise OSError
        if not executable and not os.access(path, os.R_OK):
            raise OSError
    except (OSError, RuntimeError, ValueError):
        raise Refusal("Configured miner executable is unavailable or not trusted." if executable
                      else "Configured dataset is unavailable.") from None
    return path


def _miner_env():
    keep = ("HOME", "LANG", "LC_ALL", "PATH", "USER", "LOGNAME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
            "LD_LIBRARY_PATH", "OCL_ICD_VENDORS", "OCL_ICD_FILENAMES", "SSL_CERT_FILE", "SSL_CERT_DIR")
    return {key: os.environ[key] for key in keep if key in os.environ}


def _opencl_devices(env):
    try:
        result = subprocess.run([sys.executable, "-I", str(Path(__file__).resolve()), "--probe-opencl"], env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=3, check=True)
        devices = json.loads(result.stdout)
        return devices if isinstance(devices, list) else []
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def _probe_opencl():
    """Query topology only. Never create an OpenCL context or run a GPU job."""
    import ctypes as c

    try:
        lib = c.CDLL("libOpenCL.so.1")
    except OSError:
        return []
    lib.clGetPlatformIDs.argtypes = [c.c_uint, c.POINTER(c.c_void_p), c.POINTER(c.c_uint)]
    lib.clGetDeviceIDs.argtypes = [c.c_void_p, c.c_ulonglong, c.c_uint, c.POINTER(c.c_void_p), c.POINTER(c.c_uint)]
    lib.clGetDeviceInfo.argtypes = [c.c_void_p, c.c_uint, c.c_size_t, c.c_void_p, c.POINTER(c.c_size_t)]

    def info(device, code, size):
        result = (c.c_ubyte * size)()
        if lib.clGetDeviceInfo(device, code, size, result, None) == 0:
            return bytes(result)
        return b""

    count = c.c_uint()
    if lib.clGetPlatformIDs(0, None, c.byref(count)) != 0 or not 0 < count.value <= 64:
        return []
    platforms = (c.c_void_p * count.value)()
    if lib.clGetPlatformIDs(len(platforms), platforms, None) != 0:
        return []
    rows = []
    for platform in platforms:
        status = lib.clGetDeviceIDs(platform, 4, 0, None, c.byref(count))  # CL_DEVICE_TYPE_GPU
        if status == -1:  # CL_DEVICE_NOT_FOUND: a CPU-only platform is not ambiguous.
            continue
        if status != 0 or not 0 < count.value <= 256:
            return []
        devices = (c.c_void_p * count.value)()
        if lib.clGetDeviceIDs(platform, 4, len(devices), devices, None) != 0:
            return []
        for device in devices:
            size = c.c_size_t()
            extensions = b""
            if lib.clGetDeviceInfo(device, 0x1030, 0, None, c.byref(size)) == 0 and 0 < size.value <= 65536:
                extensions = info(device, 0x1030, size.value).rstrip(b"\x00")
            address = None
            if b"cl_khr_pci_bus_info" in extensions.split():
                import struct

                pci = info(device, 0x410F, 16)
                if pci:
                    domain, bus, slot, function = struct.unpack("=IIII", pci)
                    address = f"{domain:04x}:{bus:02x}:{slot:02x}.{function:x}"
            elif b"cl_amd_device_attribute_query" in extensions.split():
                pci = info(device, 0x4037, 24)  # CL_DEVICE_TOPOLOGY_AMD
                if pci and int.from_bytes(pci[:4], sys.byteorder) == 1:  # CL_DEVICE_TOPOLOGY_TYPE_PCIE_AMD
                    bus, slot, function = pci[21:24]
                    # AMD's legacy structure has no PCI domain; require a unique sysfs match.
                    suffix = f":{bus:02x}:{slot:02x}.{function:x}"
                    matches = [card.name for card in PCI_PATH.glob(f"*{suffix}") if PCI.fullmatch(card.name)]
                    if len(matches) == 1:
                        address = matches[0]
            rows.append(address if address and PCI.fullmatch(address) else None)
    return rows


def _argv(worker, profile, binary, card, env):
    adapter, index = worker["adapter"], worker["device_index"]
    dataset = str(_trusted_file(worker["dataset"])) if worker["dataset"] else ""
    if adapter == "custom":
        fields = {**profile, "device": worker["device"] or "", "dataset": dataset}
        return [str(binary), *(PLACEHOLDER.sub(lambda m: fields[m[1]], arg) for arg in worker["args"])]
    if adapter == "suprminer-cuda":
        if card["vendor"] != "nvidia" or not UUID.fullmatch(card.get("_uuid", "")) or (
                index is not None and index != card.get("index")):
            raise Refusal("NVIDIA PCI-to-CUDA mapping could not be verified.")
        env["CUDA_VISIBLE_DEVICES"] = card["_uuid"]
    else:
        if card["vendor"] != "amd":
            raise Refusal("The OpenCL adapter requires a configured AMD PCI device.")
        candidates = _opencl_devices(env)
        if candidates.count(worker["device"]) != 1:
            raise Refusal("AMD PCI-to-OpenCL mapping could not be verified.")
        ordinal = candidates.index(worker["device"])
        if index is not None and index != ordinal:
            raise Refusal("Configured OpenCL index no longer matches the PCI device.")
        if len(candidates) > 1:
            env["GPU_DEVICE_ORDINAL"] = str(ordinal)
            if _opencl_devices(env) != [worker["device"]]:
                raise Refusal("The OpenCL driver did not isolate the selected PCI device.")
    argv = [str(binary), "-a", "neb", "-o", profile["pool"], "-u", f"{profile['wallet']}.{profile['worker']}",
            "-p", "x", "-d", "0"]
    if dataset:
        argv.extend(["--neb-dataset", dataset])
    return argv


RATE = re.compile(rb"(?<![\d.])([\d]+(?:\.[\d]+)?)\s*([kKmMgGtT]?)H/s\b")
SHARES = re.compile(rb"\baccepted:\s*(\d{1,16})/(\d{1,16})\b", re.IGNORECASE)


def _metrics(line, log_format):
    if log_format == "json-lines":
        try:
            data = json.loads(line)
        except (ValueError, RecursionError):
            return {}
        if not isinstance(data, dict):
            return {}
        found = {}
        rate = data.get("rate_hs")
        if type(rate) in (int, float) and _number(rate, 1e18) is not None:
            found["rate_hs"] = rate
        for key in ("accepted", "rejected"):
            if type(data.get(key)) is int and 0 <= data[key] <= 10**16:
                found[key] = data[key]
        return found
    if log_format == "suprminer":
        found = {}
        rate, shares = RATE.search(line), SHARES.search(line)
        if rate:
            multiplier = {b"": 1, b"k": 1e3, b"m": 1e6, b"g": 1e9, b"t": 1e12}[rate[2].lower()]
            value = _number(rate[1].decode(), 1e18 / multiplier)
            if value is not None:
                found["rate_hs"] = value * multiplier
        if shares and int(shares[2]) >= int(shares[1]):
            found.update(accepted=int(shares[1]), rejected=int(shares[2]) - int(shares[1]))
        return found
    return {}


def _parent_pidfd():
    fd = None
    try:
        fd = os.pidfd_open(os.getpid())
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_READ)
            if selector.select(0):
                raise OSError
        return fd
    except (AttributeError, OSError):
        if fd is not None:
            os.close(fd)
        raise Refusal("A usable Linux pidfd (kernel 5.3 or later) is required to supervise a miner.") from None


def _kill_owned_group(group, signum=None):
    try:
        os.killpg(group, signal.SIGKILL if signum is None else signum)
    except ProcessLookupError:
        pass


def _guard_miner(pidfd, status_fd, deadline, argv):
    # This interpreter, the miner and its descendants share a new group created
    # by the dashboard. Signal that group only, never an externally supplied PID.
    if os.getpid() != os.getpgid(0) or os.getpid() != os.getsid(0):
        return
    group = os.getpid()
    terminated = False

    def stop(_signum, _frame):
        nonlocal terminated
        terminated = True

    signal.signal(signal.SIGTERM, stop)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(pidfd, selectors.EVENT_READ)
            if terminated or selector.select(0) or (deadline and time.monotonic() >= deadline):
                return
            miner = subprocess.Popen(argv, stdin=subprocess.DEVNULL, close_fds=True)
            os.write(status_fd, b"S")
            while True:
                if terminated or selector.select(0):
                    return
                result = miner.poll()
                if result is not None:
                    os.write(status_fd, b"0" if result == 0 else b"1")
                    return
                left = deadline - time.monotonic() if deadline else 0.05
                if left <= 0:
                    os.write(status_fd, b"E")
                    return
                if selector.select(min(left, 0.05)):
                    return
    except OSError:
        pass
    finally:
        _kill_owned_group(group)


def _spawn_guard(argv, cwd, env, capture, deadline):
    pidfd = _parent_pidfd()
    read_fd = write_fd = None
    proc = None
    transferred = False
    try:
        read_fd, write_fd = os.pipe()
        guard_argv = [sys.executable, "-I", str(Path(__file__).resolve()), "--guard-miner",
                      str(pidfd), str(write_fd), str(deadline), *argv]
        proc = subprocess.Popen(guard_argv, cwd=cwd, env=env, start_new_session=True,
                                pass_fds=(pidfd, write_fd), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE if capture else subprocess.DEVNULL, stderr=subprocess.STDOUT)
        os.close(write_fd)
        write_fd = None
        with selectors.DefaultSelector() as selector:
            selector.register(read_fd, selectors.EVENT_READ)
            if not selector.select(2) or os.read(read_fd, 1) != b"S":
                raise OSError("No successful launch acknowledgement.")
        os.set_blocking(read_fd, False)
        transferred = True
        return proc, read_fd
    finally:
        os.close(pidfd)
        if write_fd is not None:
            os.close(write_fd)
        if not transferred:
            if read_fd is not None:
                os.close(read_fd)
            if proc is not None:
                _kill_owned_group(proc.pid)
                if proc.stdout:
                    proc.stdout.close()
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass  # The still-owned group has received SIGKILL; no restart was accepted.


@dataclass
class _Child:
    proc: subprocess.Popen
    log_format: str
    card: dict | None
    status_fd: int
    end_read: threading.Event = field(default_factory=threading.Event)
    stats_lock: threading.Lock = field(default_factory=threading.Lock)
    stats: dict = field(default_factory=dict)
    thread: threading.Thread | None = None

    def observe(self):
        stream = self.proc.stdout
        try:
            with selectors.DefaultSelector() as selector, stream:
                selector.register(stream, selectors.EVENT_READ)
                record, overflow = bytearray(), False
                while not self.end_read.is_set():
                    if not selector.select(0.2):
                        continue
                    chunk = os.read(stream.fileno(), 4096)
                    if not chunk:
                        break
                    for part in chunk.splitlines(keepends=True):
                        if not overflow and len(record) + len(part) <= 4096:
                            record.extend(part)
                        else:
                            overflow = True
                        if part.endswith(b"\n"):
                            if not overflow:
                                values = _metrics(record, self.log_format)
                                now = time.monotonic()
                                with self.stats_lock:
                                    self.stats.update({k: (v, now) for k, v in values.items()})
                            record, overflow = bytearray(), False
        except OSError:
            pass

    def finished(self):
        # Observe without reaping. The still-owned leader's PID prevents group-ID reuse
        # until every group signal has been sent, including after natural leader exit.
        return os.waitid(os.P_PID, self.proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None

    def stop(self, graceful, *, timeout=2, join=True):
        if graceful:
            self._signal(signal.SIGTERM)
            deadline = time.monotonic() + 0.25
            while time.monotonic() < deadline and not self.finished():
                time.sleep(0.01)
        self._signal(signal.SIGKILL)
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError("The worker process has not stopped.") from None
        self.end_read.set()
        if self.thread and join:
            self.thread.join(timeout=0.5)
        try:
            return os.read(self.status_fd, 1)
        except BlockingIOError:
            return b""
        finally:
            os.close(self.status_fd)

    def _signal(self, signum):
        _kill_owned_group(self.proc.pid, signum)


class Manager:
    def __init__(self, workers: list, state_dir: Path):
        self._workers = {item["id"]: item for item in validate_workers(workers)}
        self._state_dir = Path(state_dir).resolve()
        self._state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._state = {ident: ("stopped", "Stopped.") for ident in self._workers}
        self._live = {}
        self._expired = set()
        self._events = deque(maxlen=1024)
        self._lock = threading.RLock()
        self._limits = _limits({})
        self._closed = False

    def _event(self, kind, ident, detail, status):
        self._events.append(dict(type=kind, time=int(time.time()), worker=ident, detail=detail, status=status))

    def _worker(self, ident):
        if not isinstance(ident, str) or ident not in self._workers:
            raise Refusal("Unknown worker.")
        return self._workers[ident]

    def register(self, rows, save):
        """Add new groups without stopping, replacing, or retargeting live workers."""
        with self._lock:
            if self._closed:
                raise Refusal("Worker manager is closed.")
            merged = validate_workers([*self._workers.values(), *rows])
            save(merged)
            for item in merged:
                ident = item["id"]
                if ident not in self._workers:
                    self._workers[ident] = item
                    self._state[ident] = ("stopped", "Stopped.")

    def _exit_if_finished(self, ident):
        child = self._live.get(ident)
        if child and child.finished():
            result = child.stop(graceful=False)
            del self._live[ident]
            if self._state[ident][0] == "protection":
                detail = "Thermal cut-off completed."
                self._state[ident] = ("protection", detail)
                self._event("thermal", ident, detail, "stopped")
                return
            status = "stopped" if result in {b"0", b"E"} else "failed"
            detail = "Maximum worker runtime elapsed." if result == b"E" else (
                "Miner exited normally." if result == b"0" else "Miner or its watchdog exited unexpectedly.")
            self._state[ident] = (status, detail)
            if result == b"E":
                self._expired.add(ident)
            self._event("exit", ident, detail, "expired" if result == b"E" else status)

    def start(self, worker_id, profile: dict, *, limits: dict | None = None, max_seconds: float | None = None):
        with self._lock:
            worker = self._worker(worker_id)
            if self._closed:
                raise Refusal("Worker manager is closed.")
            self._exit_if_finished(worker_id)
            if worker_id in self._live:
                raise Refusal("Worker is already running.")
            self._expired.discard(worker_id)
            try:
                if sys.platform != "linux":
                    raise Refusal("Worker execution requires Linux.")
                if limits is not None:
                    self._limits = _limits(limits)
                if max_seconds is not None and (type(max_seconds) not in {int, float} or
                        _number(max_seconds, 86400, 0.01) is None):
                    raise Refusal("Invalid maximum worker runtime.")
                profile = _profile(profile, worker["adapter"])
                binary = _trusted_file(worker["binary"], executable=True)
                card = None
                if worker["device"] is not None:
                    card = next((row for row in discover_devices() if row["device"] == worker["device"]), None)
                    reason = _thermal(card, self._limits)
                    if reason:
                        raise Refusal(reason)
                env = _miner_env()
                argv = _argv(worker, profile, binary, card, env)
                cwd = self._state_dir / worker_id
                cwd.mkdir(exist_ok=True, mode=0o700)
                deadline = time.monotonic() + max_seconds if max_seconds is not None else 0
                proc, status_fd = _spawn_guard(argv, cwd, env, worker["log_format"] != "none", deadline)
            except Refusal as exc:
                self._state[worker_id] = ("blocked", str(exc))
                raise
            except OSError:
                self._state[worker_id] = ("failed", "Configured miner could not start.")
                raise RuntimeError("Configured miner could not start.") from None
            child = _Child(proc, worker["log_format"], card, status_fd)
            self._live[worker_id] = child
            if proc.stdout:
                child.thread = threading.Thread(target=child.observe, daemon=True)
                try:
                    child.thread.start()
                except RuntimeError:
                    child.thread = None
                    self._stop(worker_id, "stop", "Miner output reader could not start.", "failed")
                    proc.stdout.close()
                    raise RuntimeError("Miner output reader could not start.") from None
            self._state[worker_id] = ("running", "Miner process started.")
            self._event("start", worker_id, "Miner process started.", "running")

    def _stop(self, ident, kind, detail, status):
        child = self._live.get(ident)
        if child:
            child.stop(graceful=True)
            del self._live[ident]
            self._event(kind, ident, detail, status)
        self._state[ident] = (status, detail)

    def stop(self, worker_id):
        with self._lock:
            self._worker(worker_id)
            self._exit_if_finished(worker_id)
            self._stop(worker_id, "stop", "Stopped by request.", "stopped")
            self._expired.discard(worker_id)

    def snapshot(self, limits: dict) -> list[dict]:
        with self._lock:
            self._limits = _limits(limits)
            cards = ({row["device"]: row for row in discover_devices()}
                     if any(w["device"] is not None for w in self._workers.values()) else {})
            unsafe = {}
            # Signal every unsafe group before waiting for any one driver or process.
            for ident, child in self._live.items():
                device = self._workers[ident]["device"]
                if device is None or child.finished():
                    continue
                card = cards.get(device)
                if self._state[ident][0] == "protection":
                    reason = "Thermal cut-off completed."
                else:
                    identity = ("vendor", "_uuid", "_identity")
                    required = tuple(metric for metric in ("hot", "hbm") if child.card.get(metric) is not None
                                     and self._limits[f"{metric}_limit"] is not None)
                    changed = card and any(card.get(key) != child.card.get(key) for key in identity)
                    reason = "GPU mapping changed while mining." if changed else _thermal(card, self._limits, required)
                if reason:
                    child._signal(signal.SIGKILL)
                    unsafe[ident] = reason
            cutoff = time.monotonic() + 0.25
            for ident, reason in unsafe.items():
                try:
                    self._live[ident].stop(graceful=False, timeout=max(0, cutoff - time.monotonic()), join=False)
                except RuntimeError:
                    if self._state[ident][0] != "protection":
                        self._event("thermal", ident, reason, "unconfirmed")
                    self._state[ident] = ("protection", "Thermal cut-off requested; worker has not exited.")
                else:
                    del self._live[ident]
                    self._state[ident] = ("protection", reason)
                    self._event("thermal", ident, reason, "stopped")

            result = []
            for ident, worker in self._workers.items():
                if ident not in unsafe:
                    self._exit_if_finished(ident)
                child, card = self._live.get(ident), cards.get(worker["device"])
                status, message = self._state[ident]
                stats = dict.fromkeys(("rate_hs", "accepted", "rejected"))
                if child and status == "running":
                    with child.stats_lock:
                        stats.update({key: value for key, (value, stamp) in child.stats.items()
                                      if key != "rate_hs" or time.monotonic() - stamp <= 30})
                result.append(dict(id=ident, label=worker["label"], adapter=worker["adapter"],
                                   device=worker["device"], state=status, message=message,
                                   exit_reason="time-limit" if ident in self._expired else None,
                                   **{metric: (card or {}).get(metric) for metric in METRICS}, **stats))
            return result

    def drain_events(self) -> list[dict]:
        with self._lock:
            result = list(self._events)
            self._events.clear()
            return result

    def close(self):
        with self._lock:
            self._closed = True
            failure = False
            for ident in list(self._live):
                try:
                    self._stop(ident, "stop", "Worker manager shut down.", "stopped")
                except RuntimeError:
                    failure = True
            if failure:
                raise RuntimeError("One or more owned workers have not stopped.")


if __name__ == "__main__":
    if sys.argv[1:] == ["--probe-opencl"]:
        print(json.dumps(_probe_opencl()))
    elif len(sys.argv) >= 6 and sys.argv[1] == "--guard-miner":
        _guard_miner(int(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4]), sys.argv[5:])
