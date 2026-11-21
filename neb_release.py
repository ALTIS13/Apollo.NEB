"""Check and explicitly apply releases from the public Portable repository."""
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from neb_core import read, store
from neb_setup import Asset, download, file_digest

VERSION = "0.9.0"
MINIMUM_RELEASE = "0.7.1-rc.1"
REPO = "ALTIS13/Apollo.NEB"
RELEASES = f"https://github.com/{REPO}/releases"
REMOTE = f"https://api.github.com/repos/{REPO}/releases?per_page=100"
RAW_CATALOG = f"https://raw.githubusercontent.com/{REPO}/main/docs/ota-catalog.json"
METADATA_TIMEOUT = 3
MAX_RAW_CATALOG = 256 * 1024
_metadata_slots = {url: threading.BoundedSemaphore() for url in (REMOTE, RAW_CATALOG)}
MAX_DOWNLOAD = 20 * 1024**2
MAX_METADATA = 2 * 1024**2
CHANNEL_FILE = "release-channel.json"
CHANNELS = ("stable", "testing")
SUFFIXES = {"linux": "linux.tar.gz", "windows": "windows-x64.zip"}
LINUX_KERNELS = ("6.6", "6.12")
TAG = re.compile(r"v([0-9]{1,4})\.([0-9]{1,4})\.([0-9]{1,4})(?:-rc\.([0-9]{1,4}))?")
STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


@dataclass(frozen=True)
class Candidate:
    order: tuple[int, int, int, int, int]
    tag: str
    name: str
    asset: Asset
    release_url: str
    published_at: str
    notes: str


def version(value):
    if not isinstance(value, str):
        return None
    tag = value if value.startswith("v") else "v" + value
    match = TAG.fullmatch(tag)
    if not match:
        return None
    major, minor, patch, rc = match.groups()
    order = int(major), int(minor), int(patch), int(rc is None), int(rc or 0)
    canonical = "v" + ".".join(map(str, order[:3]))
    if rc is not None:
        canonical += f"-rc.{order[-1]}"
    return order if canonical == tag else None


def host_platform():
    if sys.platform == "linux":
        return "linux"
    if sys.platform == "win32" and platform.machine().lower() in {"amd64", "x86_64"}:
        return "windows"
    return "unsupported"


def host_kernel():
    if host_platform() == "linux" and platform.machine().lower() in {"amd64", "x86_64"}:
        match = re.match(r"^(6\.(?:6|12))(?:[.-]|$)", platform.release())
        return match[1] if match else None
    return None


def channel_value(value):
    if value not in CHANNELS:
        raise ValueError("Выберите канал stable или testing.")
    return value


def read_channel(config):
    if config is None:
        return "stable"
    try:
        saved = read(config / CHANNEL_FILE, {"channel": "stable"})
        if isinstance(saved, dict) and set(saved) == {"channel"}:
            return channel_value(saved["channel"])
    except (ValueError, RecursionError):
        pass
    raise ValueError("Настройка канала повреждена. Выберите канал заново.")


def checked_asset(assets, tag, name):
    matches = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == name]
    if len(matches) != 1:
        return None
    item = matches[0]
    digest, size = item.get("digest"), item.get("size")
    url = f"{RELEASES}/download/{tag}/{name}"
    if (item.get("state") == "uploaded" and item.get("browser_download_url") == url
            and isinstance(digest, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
            and type(size) is int and 1 <= size <= MAX_DOWNLOAD):
        return Asset(url, digest[7:], size)
    return None


def candidate(release, channel, suffixes):
    if not isinstance(release, dict) or release.get("draft") is not False:
        return None
    tag = release.get("tag_name")
    order = version(tag)
    if order is None or not tag.startswith("v") or order < version(MINIMUM_RELEASE):
        return None
    testing = channel == "testing"
    if release.get("prerelease") is not testing or testing != (order[3] == 0):
        return None
    published_at = release.get("published_at")
    if not isinstance(published_at, str) or not STAMP.fullmatch(published_at):
        return None
    try:
        datetime.fromisoformat(published_at[:-1] + "+00:00")
    except ValueError:
        return None
    release_url = f"{RELEASES}/tag/{tag}"
    if release.get("html_url") != release_url:
        return None
    base = ".".join(map(str, order[:3]))
    assets = release.get("assets")
    if not isinstance(assets, list):
        return None
    for suffix in suffixes:
        name = f"apollo-neb-{base}-{suffix}"
        asset = checked_asset(assets, tag, name)
        if asset:
            body = release.get("body")
            notes = "".join(c for c in body[:6000] if c in "\n\t" or c.isprintable()).strip() if isinstance(body, str) else ""
            return Candidate(order, tag, name, asset, release_url, published_at, notes)
    return None


def select(entries, *, channel="stable", platform="linux", kernel=None, tag=None):
    channel_value(channel)
    suffix = SUFFIXES.get(platform)
    if not suffix or not isinstance(entries, list):
        return None
    suffixes = (f"linux-x64-kernel-{kernel}.tar.gz", suffix) if platform == "linux" and kernel in LINUX_KERNELS else (suffix,)
    candidates = []
    for release in entries:
        record = candidate(release, channel, suffixes)
        if record and (tag is None or record.tag == tag):
            candidates.append(record)
    return max(candidates, key=lambda item: item.order, default=None)


class NoMetadataRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_catalog(url):
    request = Request(url, headers={"User-Agent": "Apollo.NEB", "Accept": "application/json"})
    limit = MAX_METADATA if url == REMOTE else MAX_RAW_CATALOG
    try:
        with build_opener(NoMetadataRedirect()).open(request, timeout=METADATA_TIMEOUT) as response:
            if response.geturl() != url:
                raise ValueError("Источник каталога изменился.")
            raw = response.read(limit + 1)
    except HTTPError as error:
        if error.fp:
            error.close()
        raise
    if len(raw) > limit:
        raise ValueError("Каталог превышает допустимый размер.")
    catalog = json.loads(raw)
    if not isinstance(catalog, list):
        raise ValueError("Ожидался список выпусков.")
    return catalog


def _request_catalog(url):
    # A socket timeout alone does not bound the elapsed time of a slow response.
    gate = _metadata_slots[url]
    if not gate.acquire(blocking=False):
        raise TimeoutError("Предыдущая проверка каталога ещё выполняется.")
    result = Future()

    def fetch():
        try:
            data = _read_catalog(url)
        except Exception as error:
            gate.release()
            result.set_exception(error)
        else:
            gate.release()
            result.set_result(data)

    worker = threading.Thread(target=fetch, name="neb-release-metadata", daemon=True)
    try:
        worker.start()
    except RuntimeError:
        gate.release()
        raise TimeoutError("Не удалось запустить проверку каталога.") from None
    return result.result(timeout=METADATA_TIMEOUT)


def records():
    failures = (ValueError, OSError, HTTPException, RecursionError)
    rate_limited = False
    try:
        return _request_catalog(REMOTE)
    except HTTPError as error:
        rate_limited = error.code == 429 or (
            error.headers and error.headers.get("X-RateLimit-Remaining") == "0"
        )
    except failures:
        pass
    try:
        return _request_catalog(RAW_CATALOG)
    except failures:
        if rate_limited:
            raise ValueError("GitHub ограничил проверку релизов, а резервный публичный каталог недоступен.") from None
        raise ValueError("Основной и резервный публичные каталоги релизов GitHub недоступны.") from None


class Releases:
    def __init__(self, *, platform=None, kernel=None):
        self.platform = host_platform() if platform is None else platform
        self.kernel = host_kernel() if platform is None else kernel
        self.checked = 0.0
        self.catalog = None
        self.message = None
        self.lock = threading.Lock()

    def set_channel(self, config, channel):
        choice = {"channel": channel_value(channel)}
        with self.lock:
            store(config / CHANNEL_FILE, choice)
        return choice

    def status(self, config=None, *, check=True):
        with self.lock:
            channel = read_channel(config)
            row = {"channel": channel, "current": VERSION, "url": RELEASES, "state": "unchecked",
                   "platform": self.platform, "can_apply": self.platform == "linux"}
            if not check:
                return row
            if self.platform not in SUFFIXES:
                row.update(state="unavailable", message="Для этой платформы нет пакета Apollo.NEB.")
                return row
            now = time.monotonic()
            if self.catalog is None or now - self.checked >= 120:
                try:
                    self.catalog, self.message = records(), None
                except ValueError as error:
                    self.catalog, self.message = [], str(error)
                self.checked = time.monotonic()
            latest = select(self.catalog, channel=channel, platform=self.platform, kernel=self.kernel)
            if latest is None:
                row.update(state="unavailable", message=self.message or "В этом канале нет проверяемого выпуска для платформы.")
            elif latest.order <= version(VERSION):
                row.update(state="current", latest=latest.tag)
            else:
                row.update(state="available", latest=latest.tag, release_url=latest.release_url,
                           published_at=latest.published_at, notes=latest.notes,
                           asset={"name": latest.name, "url": latest.asset.url,
                                  "sha256": latest.asset.digest, "size": latest.asset.size})
            return row


def safe_unpack(archive, dest):
    seen, count, total, root = set(), 0, 0, None
    with tarfile.open(archive,"r:gz") as bundle:
        for member in bundle:
            count += 1
            total += member.size
            if count > 1000 or total > 80 * 1024**2:
                raise ValueError("Архив обновления превысил допустимый размер.")
            name = member.name.rstrip("/")
            parts = name.split("/")
            if (not 1 <= len(parts) <= 10 or not re.fullmatch(r"Apollo\.NEB-[A-Za-z0-9.-]{1,40}",parts[0])
                    or any(not re.fullmatch(r"[A-Za-z0-9._-]{1,80}",x) or x in (".","..") for x in parts)
                    or name in seen or not (member.isdir() or member.isfile())):
                raise ValueError("Небезопасный путь или тип файла в архиве.")
            if root is None:
                root = parts[0]
            if root != parts[0]:
                raise ValueError("Ожидался один каталог Apollo.NEB.")
            seen.add(name)
            target = dest.joinpath(*parts)
            if member.isdir():
                target.mkdir(mode=0o700,parents=True,exist_ok=True)
            else:
                target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
                with bundle.extractfile(member) as src, target.open("xb") as dst:
                    shutil.copyfileobj(src,dst)
                target.chmod(0o700 if member.mode & 0o100 else 0o600)
    if not root or not (dest/root/"install.sh").is_file():
        raise ValueError("Установщик не найден в архиве.")
    return dest / root


def package_order(path):
    with path.open(encoding="utf-8") as source:
        text = source.read(16384)
    tags = re.findall(r"""(?m)^VERSION\s*=\s*["']([^"'\n]+)["'][ \t]*$""", text)
    order = version(tags[0]) if len(tags) == 1 else None
    if order is None:
        raise ValueError("Не удалось проверить версию кода Apollo.NEB.")
    return order


def installed_order(prefix):
    current = prefix / "lib" / "apollo-neb" / "current"
    try:
        name = os.readlink(current)
    except FileNotFoundError:
        return version(VERSION)
    if not re.fullmatch(r"release\.[A-Za-z0-9]{8}", name):
        raise ValueError("Не удалось проверить текущую установку Apollo.NEB.")
    return max(version(VERSION), package_order(current.parent / name / "neb_release.py"))


@contextmanager
def lock_upgrade(state):
    import fcntl

    with (state / "upgrade.lock").open("a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Обновление этого экземпляра уже выполняется.") from None
        yield


def upgrade(config, state, *, prefix, apply=False, service=False, tag=None):
    target_os = host_platform()
    if apply and target_os != "linux":
        raise ValueError("Установка обновления этой командой доступна только на Linux.")
    if apply and tag is None:
        raise ValueError("Укажите проверенный тег выпуска в --version, затем повторите команду.")
    if tag is not None and (version(tag) is None or not tag.startswith("v")):
        raise ValueError("Укажите точный тег GitHub в --version.")
    if service and (config != Path.home()/".config"/"apollo-neb" or state != Path.home()/".local/state"/"apollo-neb"
                    or prefix != Path.home()/".local"):
        raise ValueError("--service требует стандартные каталоги Apollo.NEB. Для выбранных путей обновляйте без этого флага.")
    channel = read_channel(config)
    release = select(records(), channel=channel, platform=target_os, kernel=host_kernel(), tag=tag)
    if release is None or release.order <= version(VERSION):
        if apply:
            raise ValueError("В выбранном канале нет подтверждённого обновления до указанного выпуска.")
        print("Более новый подтверждаемый релиз не найден.")
        return
    print(f"Доступен {release.tag} из {REPO}. SHA-256: {release.asset.digest}")
    if not apply:
        if target_os == "linux":
            print(f"Остановите панель и запустите apollo-neb upgrade --apply --version {release.tag}.")
        else:
            print(f"Загрузите и проверьте архив на странице выпуска {release.release_url}.")
        return
    with lock_upgrade(state), tempfile.TemporaryDirectory(prefix="apollo-neb-upgrade-") as scratch:
        if release.order <= installed_order(prefix):
            raise ValueError("Уже установлена такая же или более новая версия Apollo.NEB.")
        path = Path(scratch)
        archive = path / release.name
        download(release.asset, archive)
        if archive.stat().st_size != release.asset.size or file_digest(archive) != release.asset.digest:
            raise ValueError("Загруженный архив не совпал с опубликованной контрольной суммой.")
        target = safe_unpack(archive, path / "unpacked")
        if package_order(target / "neb_release.py") != release.order:
            raise ValueError("Версия кода в архиве не совпала с выбранным тегом.")
        if read_channel(config) != channel or release.order <= installed_order(prefix):
            raise ValueError("Канал или установленная версия изменились. Проверьте обновление заново.")
        args = ["/bin/sh", str(target / "install.sh"), "--prefix", str(prefix)]
        if service:
            args += ["--service"]
        else:
            args += ["--config-dir", str(config), "--state-dir", str(state)]
        subprocess.run(args, check=True, timeout=50, cwd=target)
