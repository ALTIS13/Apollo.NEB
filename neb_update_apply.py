"""Apply a verified official release from an independent Linux user helper."""

from contextlib import closing
from http.client import HTTPConnection, HTTPException
import json
import os
from pathlib import Path
import platform
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import time

import neb_install_check as targets
import neb_release as ota


UNIT = "apollo-neb.service"
HEALTH_LIMIT = 1024
HEALTH_SECONDS = 15
COMMAND_SECONDS = 45
JOB_FIELDS = {"id", "current", "tag", "sha256", "channel", "expires"}
EFFECT_ERRORS = (OSError, RuntimeError, ValueError, subprocess.SubprocessError)


def _linux_uid():
    if sys.platform != "linux" or os.geteuid() == 0:
        raise ValueError("Обновление службы доступно обычному пользователю Linux.")
    return os.geteuid()


def _check_deadline(job):
    if job["expires"] <= time.time():
        raise ValueError("Срок задания обновления истёк.")


def _orders(job):
    if (not isinstance(job, dict) or job.keys() != JOB_FIELDS
            or not isinstance(job["id"], str) or not re.fullmatch(r"[0-9a-f]{32}", job["id"])
            or not isinstance(job["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", job["sha256"])
            or not isinstance(job["tag"], str) or not job["tag"].startswith("v")
            or type(job["expires"]) is not int):
        raise ValueError("Неверное задание обновления.")
    _check_deadline(job)
    ota.channel_value(job["channel"])
    previous, replacement = ota.version(job["current"]), ota.version(job["tag"])
    if previous is None or replacement is None or replacement <= previous:
        raise ValueError("Укажите более новую версию Apollo.NEB.")
    return previous, replacement


def _owned_file(path, uid):
    info = targets._entry(path)
    return info is not None and targets._regular_owned(info, uid)


def _private_directory(path, uid):
    targets._directory(str(path), "configuration or state", uid)
    info = path.stat()
    if info.st_uid != uid or (os.name == "posix" and info.st_mode & 0o077):
        raise ValueError("Каталогу конфигурации и состояния требуются права 0700.")


def _installation(config, state, prefix, port, uid):
    home = Path.home()
    if (config, state, prefix) != (home / ".config" / "apollo-neb",
                                   home / ".local" / "state" / "apollo-neb", home / ".local"):
        raise ValueError("Управляемое обновление требует стандартного расположения пользовательской службы.")
    for path in (config, state):
        _private_directory(path, uid)
    for filename in ("upgrade.lock", "service.lock"):
        path = state / filename
        if targets._entry(path) is not None and not _owned_file(path, uid):
            raise ValueError("Небезопасный файл блокировки установки.")
    settings = config / "settings.json"
    if not _owned_file(settings, uid):
        raise ValueError("Файл настроек панели недоступен.")
    with settings.open("rb") as source:
        data = json.loads(source.read(4096))
    if (not isinstance(data, dict) or type(data.get("port")) is not int or data["port"] != port
            or type(port) is not int or not 1024 <= port <= 65535):
        raise ValueError("Порт проверки не совпадает с настройками панели.")
    base = targets._directory(str(prefix / "lib" / "apollo-neb"), "release directory", uid)
    unit = home / ".config" / "systemd" / "user" / UNIT
    if not _owned_file(unit, uid) or not _owned_file(prefix / "bin" / "apollo-neb", uid):
        raise ValueError("Не найдены файлы установленной пользовательской службы.")
    return home, base, unit


def _current(base, uid):
    link = base / "current"
    info = link.lstat()
    if not stat.S_ISLNK(info.st_mode) or info.st_uid != uid:
        raise ValueError("Недопустимая ссылка текущей версии.")
    name = os.readlink(link)
    if not targets.RELEASE.fullmatch(name):
        raise ValueError("Недопустимое имя каталога версии.")
    root = targets._directory(str(base / name), "current", uid)
    if not _owned_file(root / "neb_release.py", uid) or not _owned_file(root / "neb.py", uid):
        raise ValueError("Файлы текущей версии недоступны.")
    return root


def _systemctl(*args, unit=UNIT, check=True):
    return subprocess.run(["systemctl", "--user", "--no-pager", *args, *([unit] if unit else [])],
                          stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          check=check, timeout=COMMAND_SECONDS)


def _check_unit(unit):
    result = _systemctl("show", "--property=FragmentPath", "--property=LoadState",
                        "--property=ActiveState", "--property=DropInPaths")
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if fields != {"FragmentPath": str(unit), "LoadState": "loaded", "ActiveState": "active", "DropInPaths": ""}:
        raise ValueError("Работающая служба не соответствует установленному пользовательскому юниту Apollo.NEB.")


def _runtime_matches(port, order, *, revision=None):
    try:
        with closing(HTTPConnection("127.0.0.1", port, timeout=2)) as connection:
            connection.request("GET", "/api/runtime", headers={"Accept": "application/json"})
            with connection.getresponse() as response:
                content_type = response.getheader("Content-Type", "").partition(";")[0].strip().lower()
                length = response.getheader("Content-Length", "")
                if (response.status != 200 or content_type != "application/json"
                        or not re.fullmatch(r"[1-9][0-9]{0,3}", length)
                        or int(length) > HEALTH_LIMIT or response.getheader("Transfer-Encoding")):
                    return False
                body = response.read(int(length))
        if len(body) != int(length):
            return False
        data = json.loads(body)
        expected = {"version", "idle"} if revision is None else {"version", "idle", "source_revision"}
        return (isinstance(data, dict) and data.keys() == expected
                and data.get("source_revision") == revision
                and data["idle"] is True and ota.version(data["version"]) == order)
    except (OSError, HTTPException, ValueError, RecursionError):
        return False


def _await_runtime(port, order):
    end = time.monotonic() + HEALTH_SECONDS
    while True:
        if _runtime_matches(port, order):
            _systemctl("is-active", "--quiet")
            return True
        left = end - time.monotonic()
        if left <= 0:
            return False
        time.sleep(min(left, 0.2))


def _compatibility(source, release, kernel):
    with (source / "compatibility.json").open("rb") as file:
        data = json.loads(file.read(4096))
    if not isinstance(data, dict):
        raise ValueError("Некорректный манифест совместимости.")
    minimum = data.get("python_minimum")
    family = kernel if release.name.endswith(f"-linux-x64-kernel-{kernel}.tar.gz") else None
    architecture = data.get("architecture")
    if (data.get("platform") != "linux" or data.get("role") != "worker-and-controller"
            or data.get("requires_pidfd") is not True or ota.version(data.get("version")) != release.order
            or not isinstance(minimum, str) or not re.fullmatch(r"[1-9][0-9]?\.[0-9]{1,2}", minimum)
            or tuple(map(int, minimum.split("."))) > sys.version_info[:2]
            or data.get("kernel_family") != family or (family is not None and architecture != "x64")
            or architecture not in (None, "x64")
            or (architecture == "x64" and platform.machine().lower() not in ("amd64", "x86_64"))):
        raise ValueError("Архив несовместим с этим Linux.")


def _stage(scratch, source, uid, order):
    prefix = scratch / "prefix"
    command = ["/bin/sh", str(source / "install.sh"), "--prefix", str(prefix),
               "--config-dir", str(scratch / "config"), "--state-dir", str(scratch / "state"),
               "--", "--password-stdin"]
    subprocess.run(command, input=secrets.token_urlsafe(24) + "\n", text=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   cwd=source, check=True, timeout=COMMAND_SECONDS)
    result = _current(prefix / "lib" / "apollo-neb", uid)
    if ota.package_order(result / "neb_release.py") != order:
        raise ValueError("Установщик подготовил другую версию.")
    return result


def _lock_service(state):
    from neb import lock_state

    return lock_state(state)


def _switch(base, name, *, link="current"):
    with tempfile.TemporaryDirectory(dir=base, prefix=".update-link-") as temp:
        pointer = Path(temp) / "current"
        pointer.symlink_to(name, target_is_directory=True)
        os.replace(pointer, base / link)
    if os.name == "posix":
        descriptor = os.open(base, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _restore(base, state, uid, previous, replacement, port, old_order):
    actual = _current(base, uid)
    if actual == replacement:
        _systemctl("stop")
        with _lock_service(state):
            if _current(base, uid) != replacement or ota.package_order(previous / "neb_release.py") != old_order:
                raise RuntimeError("Установка изменилась во время отката.")
            _switch(base, previous.name)
    elif actual != previous:
        raise RuntimeError("Текущая версия изменилась вне операции обновления.")
    _systemctl("start")
    if not _await_runtime(port, old_order) or _current(base, uid) != previous:
        raise RuntimeError("Запуск прежней версии не подтверждён.")
    return "rolled_back" if actual == replacement else "failed"


def _activate(staged, base, state, uid, previous, port, old_order, new_order):
    replacement = base / staged.name
    try:
        _systemctl("stop")
        with _lock_service(state):
            if _current(base, uid) != previous or targets._entry(replacement) is not None:
                raise RuntimeError("Установка изменилась после проверки.")
            staged.rename(replacement)
            _switch(base, replacement.name)
        _systemctl("start")
        if not _await_runtime(port, new_order) or _current(base, uid) != replacement:
            raise RuntimeError("Запуск новой версии не подтверждён.")
    except EFFECT_ERRORS:
        try:
            return _restore(base, state, uid, previous, replacement, port, old_order)
        except EFFECT_ERRORS as error:
            raise RuntimeError("Состояние службы Apollo.NEB после восстановления не подтверждено.") from error
    return "installed"


def apply(job, *, config: Path, state: Path, prefix: Path, port: int, progress):
    """Stage and apply a pinned release. Report only an observed terminal result.

    The updater replaces code; the staged install never uses live configuration
    or state. If the new service cannot start, restore its preceding code link.
    """
    old_order, new_order = _orders(job)
    uid = _linux_uid()
    home, base, unit = _installation(config, state, prefix, port, uid)
    previous = _current(base, uid)
    _check_unit(unit)
    if ota.package_order(previous / "neb_release.py") != old_order or not _runtime_matches(port, old_order):
        raise ValueError("Панель должна работать на ожидаемой версии без активных воркеров.")
    kernel = ota.host_kernel()
    release = ota.select(ota.records(), channel=job["channel"], platform="linux", kernel=kernel, tag=job["tag"])
    if release is None or release.asset.digest != job["sha256"]:
        raise ValueError("Официальный архив не совпал с заданием обновления.")
    _check_deadline(job)
    with tempfile.TemporaryDirectory(dir=base, prefix=".neb-update-") as folder:
        scratch = Path(folder)
        archive = scratch / release.name
        ota.download(release.asset, archive)
        if archive.stat().st_size != release.asset.size or ota.file_digest(archive) != job["sha256"]:
            raise ValueError("Размер или SHA-256 архива не совпал.")
        source = ota.safe_unpack(archive, scratch / "unpacked")
        if ota.package_order(source / "neb_release.py") != new_order:
            raise ValueError("Внутренняя версия архива не совпала с тегом.")
        _compatibility(source, release, kernel)
        targets.check(str(prefix), str(home), True, source, uid)
        staged = _stage(scratch, source, uid, new_order)
        with ota.lock_upgrade(state):
            _check_unit(unit)
            if (_current(base, uid) != previous or ota.package_order(previous / "neb_release.py") != old_order
                    or targets._entry(base / staged.name) is not None or not _runtime_matches(port, old_order)):
                raise ValueError("После подготовки изменилась версия или занятость панели.")
            _check_deadline(job)
            progress("applying")
            return _activate(staged, base, state, uid, previous, port, old_order, new_order)
