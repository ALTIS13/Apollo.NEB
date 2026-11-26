"""Install Portable on an operator-approved Linux host through pinned SSH."""
import json
import logging
import os
import re
import shlex
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from neb_core import read, store
from neb_release import LINUX_KERNELS, download, file_digest, version
from neb_setup import Asset


TARGET_ID = re.compile(r"neb-[a-z0-9][a-z0-9-]{0,39}")
ACTIVE = {"downloading", "connecting", "waiting"}
SSH_TIMEOUT = 205


class RemoteInstaller:
    def __init__(self, directory, journal, origin, releases, fleet):
        self.directory, self.journal, self.origin = directory, journal, origin
        self.releases, self.fleet = releases, fleet
        self.lock = threading.Lock()
        rows = read(directory / "targets.json")
        if not isinstance(rows, list) or not 1 <= len(rows) <= 16:
            raise ValueError("Ожидается список подготовленных SSH-нод.")
        for name in ("ssh_config", "known_hosts"):
            file = directory / name
            if not file.is_file() or not 1 <= file.stat().st_size <= 128 * 1024:
                raise ValueError("Нужны отдельные настройки SSH и проверенный ключ хоста.")
        self.targets = {}
        for row in rows:
            if (not isinstance(row, dict) or row.keys() != {"id", "label", "kernel"}
                    or not isinstance(row["id"], str) or not TARGET_ID.fullmatch(row["id"])
                    or row["id"] in self.targets or not isinstance(row["label"], str)
                    or not row["label"].isprintable() or not 1 <= len(row["label"].strip()) <= 60
                    or row["kernel"] not in (None, *LINUX_KERNELS)):
                raise ValueError("Неверное описание подготовленной SSH-ноды.")
            self.targets[row["id"]] = {**row, "label": row["label"].strip()}
        self.jobs = read(journal, {})
        if not isinstance(self.jobs, dict):
            raise ValueError("Журнал установки должен быть объектом.")
        for ident, row in list(self.jobs.items()):
            if row["stage"] == "downloading":
                self._update(ident, "failed")
            elif row["stage"] == "connecting":
                self._update(ident, "waiting")

    @classmethod
    def load(cls, config, state, origin, releases, fleet):
        location = config / "remote-install"
        if not origin or not origin.startswith("https://") or not (location / "targets.json").is_file():
            return None
        return cls(location, state / "remote-install.json", origin, releases, fleet)

    def _save(self, ident, row):
        snapshot = {**self.jobs, ident: row}
        store(self.journal, snapshot)
        self.jobs = snapshot

    def _update(self, ident, stage, **fields):
        self._save(ident, {**self.jobs[ident], **fields, "stage": stage, "time": int(time.time())})

    def _refresh(self):
        for ident, row in self.jobs.items():
            if row["stage"] not in {"waiting", "unconfirmed"}:
                continue
            target = self.fleet.release_target(row["node"])
            if target and version(target["version"]) == version(row["tag"]) and target.get("upgrade") is True:
                self._update(ident, "ready")
            elif row["stage"] == "waiting" and time.time() >= row["deadline"]:
                self._update(ident, "unconfirmed")

    def list(self):
        with self.lock:
            self._refresh()
            return [{**target, "install": self.jobs.get(ident)} for ident, target in self.targets.items()]

    def catalog(self, config, ident, channel, *, check=True):
        target = self.targets.get(ident)
        if target is None:
            raise ValueError("SSH-нода отсутствует в настройках панели.")
        if not check:
            return target, []
        # Only builds with the one-shot enrollment input can be installed this way.
        return target, self.releases.versions(config, current="0.9.1", channel=channel, platform="linux",
                                              kernel=target["kernel"])

    def start(self, config, ident, channel, tag, sha256):
        target, choices = self.catalog(config, ident, channel)
        match = next((row for row in choices if row["tag"] == tag and row["asset"]["sha256"] == sha256), None)
        if match is None:
            raise ValueError("Выбранный пакет больше не подтверждается каталогом.")
        with self.lock:
            self._refresh()
            previous = self.jobs.get(ident)
            if previous and previous["stage"] != "failed":
                raise ValueError("Проверьте результат предыдущей установки на эту ноду.")
            if any(row["stage"] in ACTIVE for row in self.jobs.values()):
                raise ValueError("Дождитесь завершения текущей установки.")
            row = {"stage": "downloading", "tag": tag, "time": int(time.time())}
            self._save(ident, row)
            worker = threading.Thread(target=self._install, args=(target, match), name="neb-remote-install", daemon=True)
            try:
                worker.start()
            except RuntimeError:
                self._update(ident, "failed")
                raise
            return row

    def _ssh(self, ident, payload):
        source = Path(__file__).with_name("neb_remote_bootstrap.py").read_text(encoding="utf-8")
        pinned = (self.directory / "known_hosts").as_posix()
        if any(character in pinned for character in '"\r\n%$'):
            raise ValueError("Недопустимый путь к файлу ключей хостов.")
        remote = "/usr/bin/python3 -I -c " + shlex.quote(source)
        args = ["ssh", "-F", str(self.directory / "ssh_config"), "-T", "-S", "none",
                "-oBatchMode=yes", "-oStrictHostKeyChecking=yes", "-oUpdateHostKeys=no",
                "-oVerifyHostKeyDNS=no", "-oKnownHostsCommand=none",
                "-oGlobalKnownHostsFile=" + os.devnull,
                '-oUserKnownHostsFile="' + pinned + '"',
                "-oClearAllForwardings=yes", "-oForwardAgent=no", "-oForwardX11=no",
                "-oPermitLocalCommand=no", "-oConnectTimeout=8", "-oIdentitiesOnly=yes",
                "-oServerAliveInterval=5", "-oServerAliveCountMax=2", "--", ident, remote]
        return subprocess.run(args, input=payload, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=SSH_TIMEOUT, check=False)

    def _install(self, target, candidate):
        ident = target["id"]
        archive = candidate["asset"]
        asset = Asset(archive["url"], archive["sha256"], archive["size"])
        try:
            with tempfile.TemporaryDirectory(prefix="neb-install-") as folder:
                bundle = Path(folder) / "portable.tar.gz"
                download(asset, bundle)
                if bundle.stat().st_size != asset.size or file_digest(bundle) != asset.digest:
                    raise ValueError("Не удалось подтвердить архив.")
                payload = bundle.read_bytes()
            invite = self.fleet.enroll(target["label"])
            with self.lock:
                self._update(ident, "connecting", node=invite["id"], deadline=invite["expires"])
            kernel = target["kernel"] if "-kernel-" in archive["name"] else None
            header = {"tag": candidate["tag"], "size": asset.size, "sha256": asset.digest, "kernel": kernel,
                      "origin": self.origin, "key": invite["id"] + "." + invite["code"]}
            result = self._ssh(ident, json.dumps(header).encode("ascii") + b"\n" + payload)
            refused = {2: "protocol", 3: "target", 4: "package"}.get(result.returncode)
            if refused:
                self.fleet.revoke(invite["id"])
            with self.lock:
                if refused:
                    self._update(ident, "failed", reason=refused)
                else:
                    self._update(ident, "waiting")
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            # An interrupted SSH stream can leave the target installed. Confirm from
            # the new node's own report before allowing any further action.
            try:
                with self.lock:
                    stage = "waiting" if self.jobs[ident]["stage"] == "connecting" else "failed"
                    self._update(ident, stage)
            except OSError:
                logging.warning("Apollo.NEB remote install: journal is unavailable")
