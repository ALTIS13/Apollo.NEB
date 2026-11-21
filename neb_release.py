"""Check and explicitly apply releases from the one official repository."""
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from neb_setup import Asset, download, file_digest

VERSION = "0.7.1-rc.1"
REPO = "ALTIS13/Apollo.NEB"
RELEASES = f"https://github.com/{REPO}/releases"
REMOTE = f"repos/{REPO}/releases?per_page=20"
MAX_DOWNLOAD = 20 * 1024**2


def version(s):
    match = re.fullmatch(r"v?(\d{1,4})\.(\d{1,4})\.(\d{1,4})(?:-rc\.(\d{1,4}))?", s or "") if isinstance(s,str) else None
    if match:
        major, minor, patch, rc = match.groups()
        return int(major), int(minor), int(patch), 1 if rc is None else 0, int(rc or 0)
    return None


def select(records):
    releases = []
    for release in records if isinstance(records, list) else []:
        if not isinstance(release, dict) or release.get("draft") is not False:
            continue
        tag = release.get("tag_name")
        order = version(tag)
        if order is None:
            continue
        base = ".".join(str(n) for n in order[:3])
        name = f"apollo-neb-{base}-linux.tar.gz"
        for row in release.get("assets") if isinstance(release.get("assets"), list) else []:
            if not isinstance(row,dict) or row.get("name") != name:
                continue
            digest, size = row.get("digest"), row.get("size")
            if (isinstance(digest,str) and re.fullmatch(r"sha256:[a-f0-9]{64}",digest)
                    and type(size) is int and 1 <= size <= MAX_DOWNLOAD):
                releases.append((order,tag,name,Asset(f"{RELEASES}/download/{tag}/{name}",digest[7:],size)))
    return max(releases, key=lambda record: record[0], default=None)


def records():
    try:
        request = Request("https://api.github.com/" + REMOTE, headers={"User-Agent":"Apollo.NEB",
                          "Accept":"application/vnd.github+json"})
        with urlopen(request, timeout=5) as response:
            return json.loads(response.read(2 * 1024**2))
    except (URLError, ValueError, OSError):
        exe = shutil.which("gh")
        if not exe:
            raise ValueError("Закрытый репозиторий: откройте релизы в GitHub или авторизуйте gh на сервере.") from None
        got = subprocess.run([exe,"api","--hostname","github.com",REMOTE], stdin=subprocess.DEVNULL,
                             capture_output=True, timeout=10, check=False)
        if got.returncode:
            raise ValueError("Для закрытых релизов нужен доступ к репозиторию в GitHub CLI.") from None
        return json.loads(got.stdout[:2 * 1024**2])


class Releases:
    def __init__(self):
        self.checked = 0.
        self.cache = None
        self.lock = threading.Lock()

    def status(self):
        with self.lock:
            now = time.monotonic()
            if self.cache is not None and now - self.checked < 120:
                return self.cache
            row = {"current":VERSION,"url":RELEASES,"state":"unavailable"}
            try:
                latest = select(records())
                if latest:
                    row["latest"] = latest[1]
                    row["state"] = "available" if latest[0] > version(VERSION) else "current"
            except (ValueError, OSError, subprocess.SubprocessError):
                row["message"] = "Не удалось проверить закрытые релизы. Откройте репозиторий, войдя в GitHub."
            self.cache, self.checked = row, now
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


def upgrade(config, state, *, prefix, apply=False, service=False):
    if service and (config != Path.home()/".config"/"apollo-neb" or state != Path.home()/".local/state"/"apollo-neb"
                    or prefix != Path.home()/".local"):
        raise ValueError("--service требует стандартные каталоги Apollo.NEB. Для выбранных путей обновляйте без этого флага.")
    release = select(records())
    if not release or release[0] <= version(VERSION):
        print("Более новый подтверждаемый релиз не найден.")
        return
    _, tag, filename, asset = release
    print(f"Доступен {tag} из {REPO}. SHA-256: {asset.digest}")
    if not apply:
        print("Остановите работающую панель и запустите apollo-neb upgrade --apply.")
        return
    with tempfile.TemporaryDirectory(prefix="apollo-neb-upgrade-") as scratch:
        path = Path(scratch)
        archive = path / filename
        try:
            download(asset, archive)
        except (URLError,OSError):
            exe = shutil.which("gh")
            if not exe:
                raise ValueError("Нет доступа к архиву: авторизуйте gh для этого репозитория.") from None
            got = subprocess.run([exe,"release","download",tag,"--repo",REPO,"--pattern",filename,
                                  "--dir",str(path)], stdin=subprocess.DEVNULL,capture_output=True,
                                 timeout=100,check=False)
            if got.returncode or not archive.is_file():
                raise ValueError("GitHub не выдал архив обновления.")
        if archive.stat().st_size != asset.size or file_digest(archive) != asset.digest:
            raise ValueError("Загруженный архив не совпал с опубликованной контрольной суммой.")
        target = safe_unpack(archive,path/"unpacked")
        args = ["/bin/sh",str(target/"install.sh"),"--prefix",str(prefix)]
        if service:
            args += ["--service"]
        else:
            args += ["--config-dir",str(config),"--state-dir",str(state)]
        subprocess.run(args, check=True, timeout=50, cwd=target)
