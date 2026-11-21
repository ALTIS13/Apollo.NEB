"""Configured-device onboarding and pinned optional miner downloads."""
import hashlib
import os
import platform
import stat
import sys
import tarfile
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import neb_core
import neb_runtime


@dataclass(frozen=True)
class Asset:
    url: str
    digest: str
    size: int


MINER_RELEASE = "https://github.com/ocminer/suprminer/releases/download/v1.9.48/"
DATA = Asset("https://github.com/SilasEigen/neb/releases/download/v1.0.2/neb-datasets.bin",
             "57e22a4b8aacecb2ebae62f76307ddccb04198ed5c4ba15c1dee90ca7a7c2174", 16_087_037)
MINERS = {
    "suprminer-opencl": ("AMD · Suprminer OpenCL 1.9.48", "amd",
        Asset(MINER_RELEASE + "suprminer-neptune-1.9.48_opencl_u2204.tar.gz",
              "36309c360a819e65a70931f6bf5b96473e51b4f3365a040e871209ceb286be6f", 2_732_243),
        "suprminer-neptune/suprminer-neptune"),
    "suprminer-cuda": ("NVIDIA · Suprminer CUDA 1.9.48", "nvidia",
        Asset(MINER_RELEASE + "suprminer-neptune-1.9.48-linux-x86_64-u2204",
              "419c4d9eeae208e80c879cae8b0169c120da9342a58d8af79b6965705bde3c30", 215_218_384),
        None),
}
VENDORS = {entry[1]: key for key, entry in MINERS.items()}
ALLOWED = frozenset(("github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"))


def checked_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc not in ALLOWED:
        raise ValueError("Адрес загрузки не относится к проверенному релизу.")


class RestrictedRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        checked_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def opener(url):
    checked_url(url)
    return build_opener(RestrictedRedirect()).open(Request(url, headers={"User-Agent":"Apollo.NEB installer"}), timeout=15)


def file_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download(asset, dest):
    # Large pinned CUDA packages need enough time on slower links.
    # The opener still bounds idle reads, and size and SHA-256 are mandatory.
    budget = max(180, asset.size / (64 * 1024))
    digest, remaining, expires = hashlib.sha256(), asset.size, time.monotonic() + budget
    partial = dest.with_name("." + dest.name + ".partial")
    try:
        with opener(asset.url) as source, partial.open("xb") as target:
            while block := source.read(min(1024 * 1024, remaining + 1)):
                remaining -= len(block)
                if remaining < 0 or time.monotonic() > expires:
                    raise ValueError("Загрузка превысила допустимый размер или время.")
                digest.update(block)
                target.write(block)
        if remaining or digest.hexdigest() != asset.digest:
            raise ValueError("Не совпала контрольная сумма официального файла.")
        partial.replace(dest)
    finally:
        partial.unlink(missing_ok=True)


def local_host():
    if sys.platform != "linux" or platform.machine().lower() not in ("x86_64","amd64"):
        return False
    name, version = platform.libc_ver()
    try:
        return name == "glibc" and tuple(int(x) for x in version.split(".")[:2]) >= (2, 35)
    except ValueError:
        return False


class Setup:
    def __init__(self, state, discover=neb_runtime.discover_devices):
        self.root = state / "miner-tools"
        self.discover = discover
        self.lock = threading.RLock()
        self.jobs, self.verified = {}, {}
        self.cards, self.cards_at = [], 0.

    def detected(self, force=False):
        with self.lock:
            now = time.monotonic()
            if force or now - self.cards_at >= 10 or self.cards_at == 0:
                self.cards = [{key: row.get(key) for key in ("device","vendor","edge","hot","hbm")}
                              for row in self.discover() if row.get("vendor") in VENDORS]
                self.cards_at = now
            return self.cards.copy()

    def paths(self, adapter):
        if adapter not in MINERS:
            raise ValueError("Неизвестный адаптер.")
        directory = self.root / adapter
        binary, dataset = directory / "miner", directory / "neb-datasets.bin"
        try:
            if (directory.is_symlink() or binary.is_symlink() or dataset.is_symlink()
                    or not binary.is_file() or not dataset.is_file()):
                raise ValueError("Сначала подготовьте проверенный майнер выбранного типа.")
            file_stamps = tuple((x.st_ino, x.st_size, x.st_mtime_ns, x.st_mode) for x in (binary.stat(),dataset.stat()))
            if self.verified.get(adapter) != file_stamps:
                marker = neb_core.read(directory / "source.json", {})
                expect = MINERS[adapter][2].digest
                if (not isinstance(marker, dict) or marker.get("asset_sha256") != expect
                        or (MINERS[adapter][3] is None and marker.get("binary_sha256") != expect)
                        or not binary.stat().st_mode & stat.S_IXUSR
                        or marker.get("binary_sha256") != file_digest(binary) or DATA.digest != file_digest(dataset)):
                    raise ValueError("Файлы майнера отличаются от проверенной загрузки.")
                self.verified[adapter] = file_stamps
            return binary, dataset
        except (OSError, TypeError):
            raise ValueError("Файлы майнера не подтверждены.") from None

    def prepared(self, adapter):
        try:
            self.paths(adapter)
            return True
        except ValueError:
            return False

    def status(self, count):
        ready = local_host()
        with self.lock:
            return {"devices": self.detected(), "installed_workers_count": count,
                    "miners": [{"id": name, "label": entry[0], "vendor": entry[1], "available": ready,
                                "installed": self.prepared(name), "size": entry[2].size + DATA.size,
                                **self.jobs.get(name, {})}
                               for name, entry in MINERS.items()]}

    def prepare(self, adapter):
        if not isinstance(adapter, str) or adapter not in MINERS:
            raise ValueError("Выберите AMD или NVIDIA из списка.")
        if not local_host():
            raise ValueError("Пакеты проверены для x86_64 Linux с glibc 2.35 или новее.")
        with self.lock:
            if self.prepared(adapter):
                return {"state":"ready"}
            if self.jobs.get(adapter, {}).get("state") == "downloading":
                return {"state":"downloading"}
            if (self.root / adapter).exists() or (self.root / adapter).is_symlink():
                raise ValueError("Существующие файлы не подтверждены. Автоматическая замена остановлена.")
            self.jobs[adapter] = {"state":"downloading"}
            threading.Thread(target=self.fetch, args=(adapter,), daemon=True, name="neb-download").start()
            return {"state":"downloading"}

    def fetch(self, adapter):
        try:
            if self.root.is_symlink():
                raise ValueError("Каталог майнеров перенаправлен.")
            self.root.mkdir(mode=0o700, exist_ok=True)
            spec = MINERS[adapter]
            with tempfile.TemporaryDirectory(dir=self.root, prefix=".download-") as scratch:
                stage = Path(scratch)
                package = stage / "verified-package"
                result = stage / "tool"
                result.mkdir(mode=0o700)
                binary = result / "miner"
                download(spec[2], package)
                if spec[3] is None:
                    package.replace(binary)
                else:
                    with tarfile.open(package, "r:gz") as bundle:
                        matches = [x for x in bundle if x.name == spec[3]]
                        if len(matches) != 1 or not matches[0].isfile() or not 1 <= matches[0].size <= 30 * 1024**2:
                            raise ValueError("Не найден ожидаемый исполняемый файл майнера.")
                        with bundle.extractfile(matches[0]) as src, binary.open("xb") as dst:
                            for block in iter(lambda: src.read(1024 * 1024), b""):
                                dst.write(block)
                binary.chmod(0o700)
                download(DATA, result / "neb-datasets.bin")
                neb_core.store(result / "source.json", {"asset_sha256":spec[2].digest,"binary_sha256":file_digest(binary)})
                with self.lock:
                    result.rename(self.root / adapter)
                    self.jobs[adapter] = {"state":"ready"}
        except (OSError, ValueError, URLError, tarfile.TarError, KeyError):
            with self.lock:
                self.jobs[adapter] = {"state":"failed","message":"Не удалось получить и проверить пакет. Проверьте сеть и повторите."}

    def worker_configs(self, ids, registered):
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 24
                or any(not isinstance(key,str) for key in ids) or len(set(ids)) != len(ids)):
            raise ValueError("Выберите от 1 до 24 разных устройств.")
        cards = {row["device"]:row for row in self.detected(force=True)}
        known = {row.get("device") for row in registered}
        created = []
        with self.lock:
            for device in ids:
                if device not in cards or device in known:
                    raise ValueError("Устройство не найдено или уже настроено.")
                vendor = cards[device]["vendor"]
                adapter = VENDORS[vendor]
                binary, dataset = self.paths(adapter)
                created.append({"id":"gpu-" + device.replace(":","-").replace(".","-"),
                                "label":f"{vendor.upper()} · {device}", "adapter":adapter,
                                "binary":str(binary), "dataset":str(dataset), "device":device})
        return neb_runtime.validate_workers(created)
