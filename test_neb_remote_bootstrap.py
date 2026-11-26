import contextlib
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch


ORIGIN = "https://controller.example.invalid:8443"
PAIR_KEY = "a" * 24 + "." + "B" * 24
TAG = "v0.9.1-rc.1"


def bundle(*, kernel=None, embedded_version=TAG[1:], additions=(), omit=()):
    suffix = "linux" if kernel is None else "linux-x64-kernel-" + kernel
    root = "Apollo.NEB-0.9.1-" + suffix
    metadata = {"platform": "linux", "version": TAG[1:]}
    if kernel is not None:
        metadata.update(architecture="x64", kernel_family=kernel)
    files = {
        "install.sh": b"#!/bin/sh\nexit 0\n",
        "neb_release.py": f'VERSION = "{embedded_version}"\n'.encode("ascii"),
        "compatibility.json": json.dumps(metadata).encode("ascii"),
        "apollo-neb": b"#!/bin/sh\nexit 0\n",
        "apollo-neb.service": b"[Service]\n",
    }
    output = BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        directory = tarfile.TarInfo(root)
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        for name, contents in files.items():
            if name in omit:
                continue
            member = tarfile.TarInfo(root + "/" + name)
            member.size = len(contents)
            archive.addfile(member, BytesIO(contents))
        for name, kind in additions:
            member = tarfile.TarInfo(root + "/" + name)
            member.type = kind
            if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                member.linkname = "fixture"
            archive.addfile(member, BytesIO())
    return output.getvalue()


def frame(payload, **fields):
    metadata = {"origin": ORIGIN, "key": PAIR_KEY, "tag": TAG, "kernel": None,
                "size": len(payload), "sha256": hashlib.sha256(payload).hexdigest(), **fields}
    return json.dumps(metadata, separators=(",", ":")).encode("ascii") + b"\n" + payload


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        import neb_remote_bootstrap as bootstrap

        self.bootstrap = bootstrap
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name).resolve() / "home"
        self.home.mkdir()
        self.uid = getattr(os, "geteuid", lambda: 0)()
        lock = patch.object(bootstrap, "_lock_home", side_effect=lambda _: contextlib.nullcontext())
        self.addCleanup(lock.stop)
        lock.start()
        unit = patch.object(bootstrap, "_user_unit_missing", return_value=True)
        self.addCleanup(unit.stop)
        self.unit = unit.start()
        installer = patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0))
        self.addCleanup(installer.stop)
        self.installer = installer.start()

    def execute(self, raw):
        with patch.object(self.bootstrap, "_target_home", return_value=(self.home, self.uid)):
            return self.bootstrap.bootstrap(BytesIO(raw))

    def test_clean_kernel_package_stages_installer_and_passes_keys_only_on_stdin(self):
        installed_from = []

        def install(args, *, cwd, input, stdout, stderr, timeout, check):
            installed_from.append(cwd)
            self.assertTrue((cwd / "install.sh").is_file())
            self.assertEqual((cwd / "neb_release.py").read_bytes(), b'VERSION = "0.9.1-rc.1"\n')
            self.assertEqual(args, ["sh", "./install.sh", "--service", "--join", ORIGIN,
                                    "--join-stdin", "--allow-upgrades"])
            password, key = input.decode("ascii").splitlines()
            self.assertEqual(key, PAIR_KEY)
            self.assertRegex(password, r"^[A-Za-z0-9_-]{40,}$")
            self.assertNotEqual(password, key)
            self.assertEqual((stdout, stderr), (subprocess.DEVNULL, subprocess.DEVNULL))
            return subprocess.CompletedProcess(args, 0)

        self.installer.side_effect = install
        raw = frame(bundle(kernel="6.12"), kernel="6.12")
        self.assertEqual(self.execute(raw), 0)
        self.assertEqual(len(installed_from), 1)
        self.assertFalse(installed_from[0].exists())
        self.assertEqual(list(self.home.iterdir()), [])

    def test_malformed_headers_and_trailing_wire_bytes_never_reach_installer(self):
        payload = bundle()
        valid = frame(payload)
        bad_inputs = (
            b"x" * 4096 + b"\n" + payload,
            valid.replace(b'"kernel":null', b'"kernel":null,"kernel":null', 1),
            frame(payload, destination="unused"),
            frame(payload, origin="http://controller.example.invalid"),
            frame(payload, origin="https://name@controller.example.invalid"),
            frame(payload, origin="https://controller.example.invalid/path"),
            frame(payload, origin="https://controller.example.invalid:0"),
            frame(payload, key="short"),
            frame(payload, tag="v00.9.1-rc.1"),
            frame(payload, kernel="6.60"),
            frame(payload, size=True),
            frame(payload, size=20 * 1024**2 + 1),
            valid[:-1],
            valid + b"\x00",
        )
        for number, invalid in enumerate(bad_inputs, 1):
            with self.subTest(case=number):
                self.assertEqual(self.execute(invalid), 2)
        self.installer.assert_not_called()

    def test_wrong_digest_wrong_build_and_unsafe_archive_entries_are_rejected(self):
        payload = bundle()
        bad_bundles = (
            frame(payload, sha256="f" * 64),
            frame(payload + b"tail"),
            frame(bundle(embedded_version="0.9.0")),
            frame(bundle(kernel="6.6"), kernel="6.12"),
            frame(bundle(omit=("install.sh",))),
            frame(bundle(additions=(("duplicate", tarfile.REGTYPE), ("duplicate", tarfile.REGTYPE)))),
            frame(bundle(additions=(("unsafe/../alias", tarfile.REGTYPE),))),
            frame(bundle(additions=(("a-link", tarfile.SYMTYPE),))),
            frame(bundle(additions=(("a-hardlink", tarfile.LNKTYPE),))),
        )
        for number, invalid in enumerate(bad_bundles, 1):
            with self.subTest(case=number):
                self.assertEqual(self.execute(invalid), 4)
        self.installer.assert_not_called()
        self.assertEqual(list(self.home.iterdir()), [])

    def test_preflight_refuses_any_preexisting_default_installation_path(self):
        raw = frame(bundle())
        for relative in (".config/apollo-neb", ".local/state/apollo-neb", ".local/lib/apollo-neb/current",
                         ".local/bin/apollo-neb", ".config/systemd/user/apollo-neb.service",
                         ".local/share/systemd/user/apollo-neb.service"):
            with self.subTest(target=relative):
                existing = self.home / relative
                existing.parent.mkdir(parents=True, exist_ok=True)
                existing.write_bytes(b"existing fixture")
                self.assertEqual(self.execute(raw), 3)
                self.assertEqual(existing.read_bytes(), b"existing fixture")
                existing.unlink()
                if existing.parent.name == "apollo-neb":
                    existing.parent.rmdir()
        self.installer.assert_not_called()

    def test_preflight_rejects_directory_symlinks(self):
        outside = self.home.parent / "outside"
        outside.mkdir()
        try:
            (self.home / ".config").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Directory symlinks are unavailable for this process.")
        self.assertEqual(self.execute(frame(bundle())), 3)
        self.assertEqual(list(outside.iterdir()), [])
        self.installer.assert_not_called()

    def test_non_default_unit_discovered_by_user_manager_blocks_installation(self):
        self.unit.return_value = False
        self.assertEqual(self.execute(frame(bundle())), 3)
        self.installer.assert_not_called()

    def test_real_target_platform_gate_rejects_root_wrong_architecture_and_wrong_kernel(self):
        raw = frame(bundle(kernel="6.12"), kernel="6.12")
        for host, uid, arch, release in (
            ("win32", 1000, "x86_64", "6.12.2"),
            ("linux", 0, "x86_64", "6.12.2"),
            ("linux", 1000, "aarch64", "6.12.2"),
            ("linux", 1000, "x86_64", "6.6.3"),
            ("linux", 1000, "x86_64", "6.120.0"),
        ):
            with self.subTest(host=host, uid=uid, arch=arch, release=release), contextlib.ExitStack() as stack:
                for effect in (patch.object(sys, "platform", host), patch.object(os, "geteuid", return_value=uid, create=True),
                               patch.object(os, "getuid", return_value=uid, create=True),
                               patch("platform.machine", return_value=arch), patch("platform.release", return_value=release)):
                    stack.enter_context(effect)
                self.assertEqual(self.bootstrap.bootstrap(BytesIO(raw)), 3)
        self.installer.assert_not_called()

    def test_installer_failure_exits_without_exposing_its_error(self):
        self.installer.side_effect = subprocess.TimeoutExpired("installer", 1, output=b"private fixture")
        self.assertEqual(self.execute(frame(bundle())), 5)


if __name__ == "__main__":
    unittest.main()
