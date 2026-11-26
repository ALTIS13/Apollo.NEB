"""Exercise the boundary between an authorized release, SSH and the first report."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from neb_core import read, store
from neb_fleet import Fleet
from neb_remote_install import RemoteInstaller
from neb_release import Releases
from test_neb_release import release


TARGET = "neb-rig"
ORIGIN = "https://panel.example.invalid"
ARCHIVE = b"portable archive fixture"
SHA256 = hashlib.sha256(ARCHIVE).hexdigest()
VERSION = "v0.9.2"


class RemoteInstallTests(unittest.TestCase):
    def setUp(self):
        sandbox = tempfile.TemporaryDirectory()
        self.addCleanup(sandbox.cleanup)
        self.directory = Path(sandbox.name)
        store(self.directory / "targets.json", [{"id": TARGET, "label": "Test rig", "kernel": "6.6"}])
        for text_file in ("ssh_config", "known_hosts"):
            (self.directory / text_file).write_text("# test fixture\n")
        self.journal = self.directory / "requests.json"
        self.fleet = Fleet(self.directory / "fleet.sqlite")
        metadata = release(VERSION, "linux-x64-kernel-6.6.tar.gz")
        metadata["assets"][0].update(digest="sha256:" + SHA256, size=len(ARCHIVE))
        catalog = patch("neb_release.records", return_value=[metadata, release("v0.9.0")])
        catalog.start()
        self.addCleanup(catalog.stop)
        self.install = RemoteInstaller(self.directory, self.journal, ORIGIN, Releases(platform="linux"), self.fleet)

    def report(self, header):
        ident, code = header["key"].split(".")
        key = "b" * 43
        self.fleet.pair(ident, code, key)
        self.fleet.poll(ident, key, [], [], release={"version": VERSION[1:], "platform": "linux", "arch": "x64",
                                                    "kernel": "6.6", "upgrade": True})

    def request(self):
        with patch("neb_remote_install.threading.Thread") as thread:
            row = self.install.start(None, TARGET, "stable", VERSION, SHA256)
        self.assertEqual(row["stage"], "downloading")
        worker = thread.call_args.kwargs
        return worker["target"], worker["args"]

    def test_reject_changed_hash_old_release_and_unapproved_target(self):
        for target, version, digest in (("neb-absent", VERSION, SHA256), (TARGET, VERSION, "a" * 64),
                                        (TARGET, "v0.9.0", "a" * 64)):
            with self.subTest(target=target, version=version), self.assertRaises(ValueError):
                self.install.start(None, target, "stable", version, digest)
        self.assertFalse(self.journal.exists())

    def test_report_not_ssh_exit_is_the_installation_confirmation(self):
        run, args = self.request()
        header = {}

        def ship(ident, payload):
            line, document = payload.split(b"\n", 1)
            header.update(json.loads(line))
            self.assertEqual(ident, TARGET)
            self.assertEqual(document, ARCHIVE)
            self.assertEqual(header["origin"], ORIGIN)
            self.assertEqual(header["kernel"], "6.6")
            self.assertNotIn(header["key"], self.journal.read_text(encoding="utf-8"))
            return SimpleNamespace(returncode=0)

        with patch("neb_remote_install.download", side_effect=lambda asset, path: path.write_bytes(ARCHIVE)), \
             patch.object(self.install, "_ssh", side_effect=ship):
            run(*args)
        self.assertEqual(self.install.list()[0]["install"]["stage"], "waiting")
        with self.assertRaises(ValueError):
            self.install.start(None, TARGET, "stable", VERSION, SHA256)
        self.report(header)
        self.assertEqual(self.install.list()[0]["install"]["stage"], "ready")
        self.assertEqual(read(self.journal)[TARGET]["stage"], "ready")

    def test_corrupt_download_forbids_first_contact(self):
        run, args = self.request()
        with patch("neb_remote_install.download", side_effect=lambda asset, path: path.write_bytes(b"changed")), \
             patch.object(self.install, "_ssh") as ssh:
            run(*args)
        ssh.assert_not_called()
        self.assertEqual(self.install.list()[0]["install"]["stage"], "failed")
        self.assertEqual(self.fleet.list()["nodes"], [])

    def test_interrupted_ssh_retains_existing_enrollment_and_cannot_retry_blindly(self):
        run, args = self.request()

        def incomplete(ident, payload):
            line, _ = payload.split(b"\n", 1)
            self.report(json.loads(line))
            raise subprocess.TimeoutExpired("ssh", 1)

        with patch("neb_remote_install.download", side_effect=lambda asset, path: path.write_bytes(ARCHIVE)), \
             patch.object(self.install, "_ssh", side_effect=incomplete):
            run(*args)
        self.assertEqual(self.install.list()[0]["install"]["stage"], "ready")
        self.assertEqual(len(self.fleet.list()["nodes"]), 1)

    def test_openssh_uses_only_saved_alias_with_strict_pinning_and_private_input(self):
        with patch("neb_remote_install.subprocess.run") as process, \
             patch("neb_remote_install.Path.read_text", return_value="print('bootstrap fixture')"):
            self.install._ssh(TARGET, b"private fixture")
        args = process.call_args.args[0]
        self.assertEqual(args[:3], ["ssh", "-F", str(self.directory / "ssh_config")])
        self.assertIn("-oStrictHostKeyChecking=yes", args)
        self.assertIn("-oForwardAgent=no", args)
        self.assertTrue(any(word.startswith("-oUserKnownHostsFile=") for word in args))
        self.assertEqual(args[-3:-1], ["--", TARGET])
        self.assertTrue(args[-1].startswith("/usr/bin/python3 -I -c "))
        self.assertNotIn("private fixture", " ".join(args))
        self.assertEqual(process.call_args.kwargs["input"], b"private fixture")
