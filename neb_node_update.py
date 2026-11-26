"""Supervise one explicitly enabled Linux node update through user systemd."""
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from neb_core import read, store


TERMINAL = {"installed", "rolled_back", "failed", "unknown"}
PENDING = {"preparing", "scheduled", "applying", "unconfirmed"}
JOB_ID = re.compile(r"[a-f0-9]{32}")


def supported(config, state, entry, link):
    home = Path.home()
    if (link.get("allow_upgrades") is not True or not link["origin"].startswith("https://")
            or config != home / ".config/apollo-neb" or state != home / ".local/state/apollo-neb"):
        return False
    try:
        current = home / ".local/lib/apollo-neb/current/neb.py"
        if current.resolve(strict=True) != entry.resolve(strict=True):
            return False
        probe = subprocess.run(
            ["systemctl", "--user", "show", "apollo-neb.service", "--property=MainPID", "--value"],
            capture_output=True, text=True, timeout=3, check=False,
        )
        return probe.returncode == 0 and probe.stdout.strip() == str(os.getpid())
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return False


def _record(path, job, status):
    store(path, {"job": job, "status": status, "changed": int(time.time())})


def _ack(client, job, status):
    client.upgrade_exchange({"id": job["id"], "status": status})


def _unit(job):
    if not JOB_ID.fullmatch(job.get("id", "")):
        raise ValueError("Неверный номер задания обновления.")
    return "apollo-neb-update-" + job["id"] + ".service"


class Updates:
    def __init__(self, path, entry, service, client):
        self.path, self.entry, self.service, self.client = path, entry, service, client
        saved = read(path)
        if saved and saved["status"] in PENDING and not service.reserve_upgrade():
            raise ValueError("Можно возобновить обновление только при простое ноды.")

    def cycle(self, *, reported=True):
        saved = read(self.path)
        if not saved:
            if not reported:
                return
            job = self.client.upgrade_exchange()["job"]
            if job is None:
                return
            if not self.service.reserve_upgrade():
                _ack(self.client, job, "preparing")
                _ack(self.client, job, "failed")
                return
            try:
                _record(self.path, job, "preparing")
            except (ValueError, OSError):
                self.service.finish_upgrade()
                raise
        else:
            job, status = saved["job"], saved["status"]
            if status in {"scheduled", "applying", "unconfirmed"}:
                status = self._check_helper(saved)
                if status is None:
                    return
            if status in TERMINAL:
                self.service.finish_upgrade()
                if int(time.time()) < job["expires"]:
                    if not reported:
                        return
                    _ack(self.client, job, status)
                self.path.unlink(missing_ok=True)
                return
            if status != "preparing":
                raise ValueError("Неизвестное состояние локального обновления.")
        if not 0 < job["expires"] - int(time.time()) <= 900:
            self.service.finish_upgrade()
            self.path.unlink(missing_ok=True)
            return
        if not reported:
            return
        _ack(self.client, job, "preparing")
        _record(self.path, job, "scheduled")
        try:
            subprocess.run(
                ["systemd-run", "--user", "--collect", "--property=Type=exec", "--unit=" + _unit(job), "--",
                 sys.executable, "-I", str(self.entry), "node-update-helper", job["id"]],
                capture_output=True, timeout=8, check=True,
            )
        except (OSError, subprocess.CalledProcessError):
            _record(self.path, job, "failed")
        except subprocess.TimeoutExpired:
            # The systemd request may have been accepted. Inspect its unit before
            # deciding the job failed and allow no duplicate helper.
            pass

    def _check_helper(self, saved):
        phase = saved["status"]
        if phase == "scheduled" and int(time.time()) - saved["changed"] < 15:
            return
        job = saved["job"]
        probe = subprocess.run(
            ["systemctl", "--user", "show", _unit(job), "--property=LoadState", "--property=ActiveState"],
            capture_output=True, text=True, timeout=3, check=False,
        )
        fields = probe.stdout.splitlines()
        if probe.returncode not in (0, 1):
            return
        collected = "ActiveState=inactive" in fields and "LoadState=not-found" in fields
        if phase == "scheduled":
            if not collected:
                return
            outcome = "failed"
        else:
            stopped = "LoadState=loaded" in fields and (
                "ActiveState=inactive" in fields or "ActiveState=failed" in fields)
            if not (collected or stopped) or not self._running_expected_release(job):
                return
            outcome = "unknown"
        if read(self.path) == saved:
            _record(self.path, job, outcome)
            return outcome

    def _running_expected_release(self, job):
        status = self.service.runtime_status()
        if status["idle"] is not True or status["version"] not in (job["current"], job["tag"][1:]):
            return False
        try:
            current = Path.home() / ".local/lib/apollo-neb/current/neb.py"
            return current.resolve(strict=True) == self.entry.resolve(strict=True)
        except (OSError, RuntimeError):
            return False


def run_helper(path, ident, *, config, state, prefix, port, client, apply):
    import fcntl

    flags = os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW
    descriptor = os.open(path.with_suffix(".lock"), flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Помощник обновления уже запущен.") from None
        return _apply_saved(path, ident, config=config, state=state, prefix=prefix, port=port, client=client, apply=apply)


def _apply_saved(path, ident, *, config, state, prefix, port, client, apply):
    saved = read(path)
    if (not saved or not JOB_ID.fullmatch(ident) or saved["job"]["id"] != ident
            or saved["status"] != "scheduled"):
        raise ValueError("Подготовленное задание обновления не найдено.")
    job = saved["job"]
    entered = False

    def progress(status):
        nonlocal entered
        if status != "applying":
            raise ValueError("Неизвестный этап установки.")
        _ack(client, job, status)
        _record(path, job, status)
        entered = True

    try:
        outcome = apply(job, config=config, state=state, prefix=prefix, port=port, progress=progress)
        if outcome not in TERMINAL:
            raise RuntimeError("Итог обновления не подтверждён.")
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError):
        # A failure after stopping the old unit is not a proof that it was restored.
        _record(path, job, "unconfirmed" if entered else "failed")
        raise
    _record(path, job, outcome)
    return outcome
