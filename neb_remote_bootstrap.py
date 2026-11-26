"""Install the supplied, verified Linux release into an unused user account."""

import ast
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zlib


INPUT_ERROR = 2
TARGET_ERROR = 3
ARCHIVE_ERROR = 4
INSTALL_ERROR = 5
HEADER_LIMIT = 4096
DOWNLOAD_LIMIT = 20 * 1024**2
UNPACKED_LIMIT = 80 * 1024**2
TAR_LIMIT = UNPACKED_LIMIT + 2 * 1024**2
MEMBER_LIMIT = 1000
CHUNK = 64 * 1024
INSTALL_TIMEOUT = 180
FIELDS = {"origin", "key", "tag", "kernel", "size", "sha256"}
ORIGIN = re.compile(r"https://([A-Za-z0-9.-]{1,253})(?::([0-9]{1,5}))?")
HOST_PART = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
PAIR_KEY = re.compile(r"[0-9a-f]{24}\.[A-Za-z0-9_-]{24}")
VERSION_PART = r"(?:0|[1-9][0-9]{0,3})"
TAG = re.compile(rf"v{VERSION_PART}\.{VERSION_PART}\.{VERSION_PART}(?:-rc\.[1-9][0-9]{{0,3}})?")
SHA256 = re.compile(r"[0-9a-f]{64}")
COMPONENT = re.compile(r"[A-Za-z0-9._-]{1,80}")
INSTALL_TARGETS = (
    ".config/apollo-neb", ".local/state/apollo-neb", ".local/lib/apollo-neb", ".local/bin/apollo-neb",
    ".config/systemd/user/apollo-neb.service", ".config/systemd/user/apollo-neb.service.d",
    ".local/share/systemd/user/apollo-neb.service", ".local/share/systemd/user/apollo-neb.service.d",
)


def _matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _unique(pairs):
    data = {}
    for name, value in pairs:
        if name in data:
            raise ValueError
        data[name] = value
    return data


def _origin(value):
    match = ORIGIN.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        return False
    host, port = match.groups()
    return (port is None or 0 < int(port) <= 65535) and all(
        HOST_PART.fullmatch(label) for label in host.removesuffix(".").split(".")
    )


def _read(stream):
    raw = stream.readline(HEADER_LIMIT + 1)
    if len(raw) > HEADER_LIMIT or not raw.endswith(b"\n"):
        raise ValueError
    data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique)
    if (not isinstance(data, dict) or data.keys() != FIELDS or not _origin(data["origin"]) or
            not _matches(PAIR_KEY, data["key"]) or not _matches(TAG, data["tag"]) or
            data["kernel"] not in (None, "6.6", "6.12") or
            type(data["size"]) is not int or not 0 < data["size"] <= DOWNLOAD_LIMIT or
            not _matches(SHA256, data["sha256"])):
        raise ValueError
    payload = stream.read(data["size"])
    if len(payload) != data["size"] or stream.read(1):
        raise ValueError
    return data, payload


def _target_home(kernel):
    if (sys.platform != "linux" or sys.version_info < (3, 10) or
            platform.machine().lower() not in ("amd64", "x86_64")):
        raise ValueError
    uid = os.geteuid()
    if not uid or os.getuid() != uid:
        raise ValueError
    if kernel is not None and not re.match(re.escape(kernel) + r"(?:[.-]|$)", platform.release()):
        raise ValueError
    if os.environ.get("XDG_CONFIG_HOME") or os.environ.get("XDG_STATE_HOME"):
        raise ValueError
    raw = os.environ.get("HOME", "")
    home = Path(raw)
    if not home.is_absolute() or home == home.parent or str(home) != raw or not raw.isprintable():
        raise ValueError
    return home, uid


@contextmanager
def _lock_home(home):
    import fcntl

    descriptor = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def _entry(path):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _directory(path, uid, *, required=False, ancestor=False):
    info = _entry(path)
    if info is None and not required:
        return
    if info is None or not stat.S_ISDIR(info.st_mode):
        raise ValueError
    if os.name == "posix":
        trusted = (0, uid) if ancestor else (uid,)
        sticky_parent = ancestor and info.st_uid == 0 and info.st_mode & stat.S_ISVTX
        if info.st_uid not in trusted or (info.st_mode & 0o022 and not sticky_parent):
            raise ValueError


def _clean_targets(home, uid):
    if home.resolve(strict=True) != home:
        raise ValueError
    for parent in reversed(home.parents):
        _directory(parent, uid, required=True, ancestor=True)
    _directory(home, uid, required=True)
    checked = set()
    for relative in INSTALL_TARGETS:
        path = home / relative
        for parent in reversed(path.parents):
            if home in parent.parents and parent not in checked:
                _directory(parent, uid)
                checked.add(parent)
        if _entry(path) is not None:
            raise ValueError


def _user_unit_missing():
    result = subprocess.run(
        ["systemctl", "--user", "show", "apollo-neb.service", "--property=LoadState", "--value"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, check=False,
    )
    return result.returncode in (0, 1) and result.stdout.strip() == b"not-found"


def _inflate(payload, raw):
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    size = 0
    for offset in range(0, len(payload), CHUNK):
        block = payload[offset:offset + CHUNK]
        while block:
            inflated = inflater.decompress(block, CHUNK)
            size += len(inflated)
            if size > TAR_LIMIT or inflater.unused_data:
                raise ValueError
            raw.write(inflated)
            block = inflater.unconsumed_tail
    if not inflater.eof or size % tarfile.BLOCKSIZE:
        raise ValueError
    raw.seek(0)
    return size


def _unpack(payload, stage, header):
    version = header["tag"][1:]
    kernel = header["kernel"]
    suffix = "linux" if kernel is None else "linux-x64-kernel-" + kernel
    name = "Apollo.NEB-" + version.split("-rc.", 1)[0] + "-" + suffix
    seen, total, end = set(), 0, 0
    with tempfile.TemporaryFile(dir=stage) as raw:
        size = _inflate(payload, raw)
        with tarfile.open(fileobj=raw, mode="r:") as archive:
            for index, member in enumerate(archive, 1):
                total += member.size
                parts = member.name.split("/")
                if (index > MEMBER_LIMIT or member.size < 0 or total > UNPACKED_LIMIT or
                        not 1 <= len(parts) <= 10 or parts[0] != name or member.name in seen or
                        any(part in (".", "..") or not COMPONENT.fullmatch(part) for part in parts) or
                        member.pax_headers or member.issparse() or member.type not in (
                            tarfile.DIRTYPE, tarfile.REGTYPE, tarfile.AREGTYPE
                        ) or (member.isdir() and member.size) or (not member.isdir() and len(parts) == 1)):
                    raise ValueError
                seen.add(member.name)
                target = stage.joinpath(*parts)
                if member.isdir():
                    target.mkdir(mode=0o700, parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    with archive.extractfile(member) as source, target.open("xb") as dest:
                        shutil.copyfileobj(source, dest, CHUNK)
                    target.chmod(0o700 if member.mode & 0o100 else 0o600)
                end = member.offset_data + (member.size + 511) // 512 * 512
        if size - end < 2 * tarfile.BLOCKSIZE:
            raise ValueError
        raw.seek(end)
        while True:
            padding = raw.read(CHUNK)
            if not padding:
                break
            if padding.strip(b"\0"):
                raise ValueError
    root = stage / name
    _check_package(root, version, kernel)
    return root


def _text(path, limit):
    with path.open("rb") as source:
        raw = source.read(limit + 1)
    if len(raw) > limit:
        raise ValueError
    return raw.decode("utf-8")


def _check_package(root, version, kernel):
    if not all((root / required).is_file() for required in ("install.sh", "apollo-neb", "apollo-neb.service")):
        raise ValueError
    metadata = json.loads(_text(root / "compatibility.json", HEADER_LIMIT), object_pairs_hook=_unique)
    if (not isinstance(metadata, dict) or metadata.get("version") != version or metadata.get("platform") != "linux" or
            metadata.get("kernel_family") != kernel or metadata.get("architecture") not in (None, "x64") or
            (kernel is not None and metadata.get("architecture") != "x64")):
        raise ValueError
    tree = ast.parse(_text(root / "neb_release.py", 64 * 1024))
    versions = [node.value for node in tree.body if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == "VERSION" for target in node.targets)]
    if len(versions) != 1 or not isinstance(versions[0], ast.Constant) or versions[0].value != version:
        raise ValueError


def _stage_install(payload, header):
    try:
        with tempfile.TemporaryDirectory(prefix="apollo-neb-bootstrap-") as stage:
            root = _unpack(payload, Path(stage), header)
            password = secrets.token_urlsafe(32)
            args = ["sh", "./install.sh", "--service", "--join", header["origin"], "--join-stdin", "--allow-upgrades"]
            try:
                result = subprocess.run(args, cwd=root, input=(password + "\n" + header["key"] + "\n").encode("ascii"),
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        timeout=INSTALL_TIMEOUT, check=False)
            except (OSError, subprocess.SubprocessError):
                return INSTALL_ERROR
            return INSTALL_ERROR if result.returncode else 0
    except (OSError, ValueError, SyntaxError, EOFError, RecursionError, tarfile.TarError, zlib.error):
        return ARCHIVE_ERROR


def bootstrap(stream):
    try:
        header, payload = _read(stream)
    except (OSError, ValueError, RecursionError):
        return INPUT_ERROR
    if hashlib.sha256(payload).hexdigest() != header["sha256"]:
        return ARCHIVE_ERROR
    try:
        home, uid = _target_home(header["kernel"])
        with _lock_home(home):
            _clean_targets(home, uid)
            if not _user_unit_missing():
                return TARGET_ERROR
            return _stage_install(payload, header)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return TARGET_ERROR


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(bootstrap(sys.stdin.buffer))
