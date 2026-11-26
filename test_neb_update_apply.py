"""Managed Linux updates with a real filesystem and simulated runtime boundary."""

from contextlib import ExitStack, closing, nullcontext
from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

import neb_update_apply as updater


PREVIOUS = "1.0.0"
NEXT = "1.1.0"
OLD_FOLDER = "release.Previous"
NEW_FOLDER = "release.Canary01"
UNIT = "apollo-neb.service"
PORT = 19373
SOURCE = Path(__file__).resolve().parent


class Response:
    status = 200

    def __init__(self, data):
        self.data = json.dumps(data).encode("ascii")

    def getheader(self, name, default=None):
        return {"Content-Type": "application/json", "Content-Length": str(len(self.data))}.get(name, default)

    def read(self, count):
        return self.data[:count]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class UpdateApplyTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.home = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="neb-apply-test-"))).resolve()
        self.prefix = self.home / ".local"
        self.config = self.home / ".config" / "apollo-neb"
        self.state = self.prefix / "state" / "apollo-neb"
        self.base = self.prefix / "lib" / "apollo-neb"
        self.unit = self.home / ".config" / "systemd" / "user" / UNIT
        self.launcher = self.prefix / "bin" / "apollo-neb"
        self.current = self.base / "current"
        for path in (self.config, self.state, self.unit.parent, self.launcher.parent, self.base / OLD_FOLDER):
            path.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.launcher.write_bytes((SOURCE / "apollo-neb").read_bytes())
        self.launcher.chmod(0o700)
        self.unit.write_bytes((SOURCE / UNIT).read_bytes())
        self._write_release(self.base / OLD_FOLDER, PREVIOUS)
        try:
            self.current.symlink_to(OLD_FOLDER, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"Temporary directory symlinks unavailable ({error.errno})")
        (self.config / "settings.json").write_text(json.dumps({"port": PORT, "origin": None}), encoding="utf-8")
        (self.config / "profile.json").write_text('{"worker":"persisted fixture"}', encoding="utf-8")
        (self.config / "release-channel.json").write_text('{"channel":"stable"}', encoding="utf-8")
        self.database = self.state / "history.sqlite"
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("CREATE TABLE samples(number INTEGER NOT NULL)")
            db.execute("INSERT INTO samples VALUES (23)")
        self.saved = {path: path.read_bytes() for path in [*self.config.iterdir(), self.database]}

        self.events = []
        self.running = PREVIOUS
        self.idle = True
        self.loaded_unit = self.unit
        self.drop_ins = ""
        self.bad_candidate = False
        self.fail_old_start = False
        self.fail_first_stop = False
        self.fail_ready_idle = False
        self.status_after = None
        self.tamper = False
        self.configure_archive()
        stack.enter_context(patch.object(Path, "home", return_value=self.home))
        stack.enter_context(patch.object(updater, "_linux_uid", return_value=self.home.stat().st_uid))
        stack.enter_context(patch.object(updater.ota, "host_kernel", return_value=None))
        stack.enter_context(patch.object(updater.ota, "records", side_effect=lambda: [self.record]))
        stack.enter_context(patch.object(updater.ota, "download", side_effect=self.download))
        stack.enter_context(patch.object(updater, "HTTPConnection", side_effect=self.connection))
        stack.enter_context(patch.object(updater.subprocess, "run", side_effect=self.subprocess_run))
        stack.enter_context(patch.object(updater, "HEALTH_SECONDS", 0.04))
        if os.name != "posix":
            stack.enter_context(patch.object(updater.ota, "lock_upgrade", side_effect=lambda path: nullcontext()))
            stack.enter_context(patch.object(updater, "_lock_service", side_effect=lambda path: nullcontext()))

    @staticmethod
    def _write_release(root, version):
        (root / "neb_release.py").write_text(f'VERSION = "{version}"\n', encoding="utf-8")
        (root / "neb.py").write_text("# Fixture installed module\n", encoding="utf-8")

    def configure_archive(self, *, internal=NEXT, platform="linux", minimum="3.10"):
        tag = "v" + NEXT
        name = f"apollo-neb-{NEXT}-linux.tar.gz"
        compatibility = {"platform": platform, "role": "worker-and-controller", "version": NEXT,
                         "python_minimum": minimum, "requires_pidfd": True}
        payload = {
            "neb_release.py": f'VERSION = "{internal}"\n'.encode("ascii"),
            "neb.py": b"# Fixture source module\n",
            "compatibility.json": json.dumps(compatibility).encode("ascii"),
            "install.sh": b"#!/bin/sh\nexit 99\n",
            "apollo-neb": self.launcher.read_bytes(),
            UNIT: self.unit.read_bytes(),
        }
        buffer = BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            for filename, contents in payload.items():
                member = tarfile.TarInfo(f"Apollo.NEB-{NEXT}-linux/{filename}")
                member.size = len(contents)
                tar.addfile(member, BytesIO(contents))
        self.archive = buffer.getvalue()
        origin = "https://github.com/ALTIS13/Apollo.NEB/releases"
        digest = hashlib.sha256(self.archive).hexdigest()
        self.record = {
            "tag_name": tag, "draft": False, "prerelease": False, "published_at": "2026-11-22T19:20:00Z",
            "html_url": origin + "/tag/" + tag,
            "assets": [{"name": name, "size": len(self.archive), "state": "uploaded", "digest": "sha256:" + digest,
                        "browser_download_url": f"{origin}/download/{tag}/{name}"}],
        }
        self.job = {"id": "f" * 32, "current": PREVIOUS, "tag": tag, "sha256": digest, "channel": "stable",
                    "expires": int(time.time()) + 60}

    def download(self, asset, path):
        self.events.append("download")
        self.assertEqual(asset.digest, self.record["assets"][0]["digest"][7:])
        path.write_bytes(b"0" + self.archive[1:] if self.tamper else self.archive)

    def subprocess_run(self, args, **kwargs):
        if args[0] == "/bin/sh":
            self.events.append("stage")
            self.assertIn("--password-stdin", args)
            self.assertNotIn("--service", args)
            self.assertNotIn("--join", args)
            staged_prefix = Path(args[args.index("--prefix") + 1])
            for option in ("--config-dir", "--state-dir"):
                catalog = Path(args[args.index(option) + 1])
                self.assertNotIn(catalog, (self.config, self.state))
                catalog.mkdir(parents=True)
                (catalog / "must-stay-in-stage").write_text("fixture", encoding="ascii")
            base = staged_prefix / "lib" / "apollo-neb"
            release = base / NEW_FOLDER
            release.mkdir(parents=True, mode=0o700)
            self._write_release(release, NEXT)
            (base / "current").symlink_to(NEW_FOLDER, target_is_directory=True)
            if self.fail_ready_idle:
                self.idle = False
            return subprocess.CompletedProcess(args, 0)
        self.assertEqual(args[:3], ["systemctl", "--user", "--no-pager"])
        self.assertEqual(args[-1], UNIT)
        action = args[3]
        if action == "show":
            stdout = (f"FragmentPath={self.loaded_unit}\nLoadState=loaded\n"
                      f"ActiveState={'active' if self.running else 'inactive'}\nDropInPaths={self.drop_ins}\n")
            return subprocess.CompletedProcess(args, 0, stdout)
        if action == "is-active":
            if not self.running:
                raise subprocess.CalledProcessError(3, args)
            return subprocess.CompletedProcess(args, 0)
        self.events.append(action)
        if action == "stop":
            if self.fail_first_stop:
                self.fail_first_stop = False
                raise subprocess.TimeoutExpired(args, kwargs["timeout"])
            self.running = None
        elif action == "start":
            version = NEXT if os.readlink(self.current) == NEW_FOLDER else PREVIOUS
            if version == PREVIOUS and self.fail_old_start:
                raise subprocess.CalledProcessError(1, args)
            self.running = "9.9.9" if self.bad_candidate and version == NEXT else version
        else:
            self.fail("Unexpected systemctl action")
        return subprocess.CompletedProcess(args, 0)

    def connection(self, host, port, *, timeout):
        self.assertEqual((host, port), ("127.0.0.1", PORT))
        test = self

        class Connection:
            def request(self, method, path, **_kwargs):
                test.assertEqual((method, path), ("GET", "/api/runtime"))

            def getresponse(self):
                if test.running is None:
                    raise ConnectionRefusedError
                return Response(test.status_after if test.status_after is not None else {
                    "version": test.running, "idle": test.idle,
                })

            def close(self):
                pass

        return Connection()

    def progress(self, value):
        self.events.append(value)

    def apply(self, progress=None):
        return updater.apply(self.job, config=self.config, state=self.state, prefix=self.prefix,
                             port=PORT, progress=self.progress if progress is None else progress)

    def unchanged_settings(self):
        self.assertEqual({path: path.read_bytes() for path in self.saved}, self.saved)
        self.assertFalse((self.config / "must-stay-in-stage").exists())
        self.assertFalse((self.state / "must-stay-in-stage").exists())

    def assert_no_effect(self):
        self.assertEqual(os.readlink(self.current), OLD_FOLDER)
        self.assertFalse(any(word in self.events for word in ("stop", "start", "applying")))
        self.assertFalse((self.base / NEW_FOLDER).exists())
        self.unchanged_settings()

    @unittest.skipUnless(os.name == "posix", "POSIX atomic directory symlink replacement")
    def test_prepares_before_stop_and_confirms_running_new_version(self):
        self.assertEqual(self.apply(), "installed")
        self.assertEqual(self.events, ["download", "stage", "applying", "stop", "start"])
        self.assertEqual(os.readlink(self.current), NEW_FOLDER)
        self.assertTrue((self.base / OLD_FOLDER / "neb.py").is_file())
        self.assertEqual(self.running, NEXT)
        self.unchanged_settings()

    def test_declined_applying_callback_has_no_effect(self):
        def refuse(value):
            self.assertEqual(value, "applying")
            raise PermissionError("fixture approval not recorded")

        with self.assertRaises(PermissionError):
            self.apply(refuse)
        self.assertEqual(self.events, ["download", "stage"])
        self.assert_no_effect()

    def test_refuses_inexact_metadata_and_archive_integrity_failures(self):
        self.job["sha256"] = "3" * 64
        with self.assertRaises(ValueError):
            self.apply()
        self.assertEqual(self.events, [])
        self.assert_no_effect()
        self.configure_archive()
        self.tamper = True
        with self.assertRaises(ValueError):
            self.apply()
        self.assertEqual(self.events, ["download"])
        self.assert_no_effect()

    def test_rejects_archive_version_or_incompatible_manifest_before_installer(self):
        for fields in ({"internal": "1.1.1"}, {"platform": "windows"}, {"minimum": "3.99"}):
            with self.subTest(fields=fields):
                self.events.clear()
                self.configure_archive(**fields)
                with self.assertRaises(ValueError):
                    self.apply()
                self.assertEqual(self.events, ["download"])
                self.assert_no_effect()

    def test_rechecks_idle_and_refuses_an_unknown_loaded_unit(self):
        self.drop_ins = str(self.home / "unknown-drop-in")
        with self.assertRaises(ValueError):
            self.apply()
        self.assert_no_effect()
        self.drop_ins = ""
        self.fail_ready_idle = True
        with self.assertRaises(ValueError):
            self.apply()
        self.assert_no_effect()

    def test_rejects_malformed_runtime_response(self):
        self.status_after = {"version": PREVIOUS, "idle": 1}
        with self.assertRaises(ValueError):
            self.apply()
        self.assert_no_effect()

    @unittest.skipUnless(os.name == "posix", "POSIX atomic directory symlink replacement")
    def test_unhealthy_new_version_restores_old_link_and_live_service(self):
        self.bad_candidate = True
        self.assertEqual(self.apply(), "rolled_back")
        self.assertEqual(self.events, ["download", "stage", "applying", "stop", "start", "stop", "start"])
        self.assertEqual(os.readlink(self.current), OLD_FOLDER)
        self.assertEqual(self.running, PREVIOUS)
        self.unchanged_settings()

    @unittest.skipUnless(os.name == "posix", "POSIX atomic directory symlink replacement")
    def test_uncertain_rollback_never_returns_a_terminal_success_status(self):
        self.bad_candidate = self.fail_old_start = True
        with self.assertRaises(RuntimeError):
            self.apply()
        self.assertEqual(os.readlink(self.current), OLD_FOLDER)
        self.assertIsNone(self.running)
        self.assertEqual(self.events, ["download", "stage", "applying", "stop", "start", "stop", "start"])
        self.unchanged_settings()

    def test_failed_stop_with_old_service_confirmed_is_failed(self):
        self.fail_first_stop = True
        self.assertEqual(self.apply(), "failed")
        self.assertEqual(os.readlink(self.current), OLD_FOLDER)
        self.assertEqual(self.running, PREVIOUS)
        self.unchanged_settings()
