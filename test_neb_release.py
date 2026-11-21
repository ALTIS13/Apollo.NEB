"""Portable's public release catalog and its authenticated, local stream preference."""
import hashlib
import http.client
import json
import tarfile
import tempfile
import threading
import unittest
from contextlib import closing, nullcontext, redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError

import neb_app
import neb_core
import neb_release as ota


def release(tag, suffix="linux.tar.gz"):
    base = tag[1:].split("-")[0]
    asset = f"apollo-neb-{base}-{suffix}"
    return {
        "tag_name": tag, "draft": False, "prerelease": "-rc." in tag,
        "html_url": ota.RELEASES + "/tag/" + tag,
        "published_at": "2027-02-21T12:00:00Z",
        "body": "Notes are rendered as text.",
        "assets": [{"name": asset, "size": 100, "digest": "sha256:" + "a" * 64, "state": "uploaded",
                    "browser_download_url": f"{ota.RELEASES}/download/{tag}/{asset}"}],
    }


class MetadataFallbackTests(unittest.TestCase):
    raw = "https://raw.githubusercontent.com/ALTIS13/Apollo.NEB/main/docs/ota-catalog.json"

    def setUp(self):
        self.calls = []
        self.opener = SimpleNamespace(open=self.reply)

    def reply(self, request, timeout):
        address = request.full_url
        self.calls.append((address, timeout))
        if address == ota.REMOTE:
            if getattr(self, "rest_body", None) is not None:
                return self.response(address, self.rest_body)
            raise HTTPError(address, 403, "rate limit", {"X-RateLimit-Remaining": "0"}, None)
        self.assertEqual(address, self.raw)
        return self.response(getattr(self, "raw_response_url", address), self.raw_body)

    @staticmethod
    def response(url, body):
        data = BytesIO(body)
        data.geturl = lambda: url
        return data

    def test_rate_limit_falls_back_to_the_official_raw_catalog_and_caches_it(self):
        self.raw_body = json.dumps([release("v1.1.0"), release("v1.2.0-rc.2")]).encode()
        with tempfile.TemporaryDirectory() as directory, patch.object(ota, "build_opener", return_value=self.opener):
            config = Path(directory)
            lookup = ota.Releases(platform="linux")
            self.assertEqual(lookup.status(config)["latest"], "v1.1.0")
            lookup.set_channel(config, "testing")
            self.assertEqual(lookup.status(config)["latest"], "v1.2.0-rc.2")
        self.assertEqual(self.calls, [(ota.REMOTE, 3), (self.raw, 3)])

    def test_slow_primary_is_bounded_and_falls_back(self):
        waiting = threading.Event()
        unblocked = threading.Event()
        result = [release("v1.1.0")]

        def delayed(request, timeout):
            if request.full_url == ota.REMOTE:
                waiting.set()
                unblocked.wait(1)
                return self.response(ota.REMOTE, b"[]")
            return self.response(self.raw, json.dumps(result).encode())

        opener = SimpleNamespace(open=delayed)
        slots = {url: threading.BoundedSemaphore() for url in (ota.REMOTE, self.raw)}
        try:
            with (patch.object(ota, "build_opener", return_value=opener),
                  patch.object(ota, "_metadata_slots", slots), patch.object(ota, "METADATA_TIMEOUT", .10)):
                self.assertEqual(ota.records(), result)
                self.assertTrue(waiting.is_set())
        finally:
            unblocked.set()

    def test_primary_empty_result_is_authoritative_and_never_reads_the_fallback(self):
        self.rest_body = b"[]"
        with patch.object(ota, "build_opener", return_value=self.opener):
            self.assertEqual(ota.records(), [])
        self.assertEqual(self.calls, [(ota.REMOTE, 3)])

    def test_rejects_malformed_or_redirected_raw_catalog_and_forged_asset(self):
        with patch.object(ota, "build_opener", return_value=self.opener):
            for body in (b'{"tag_name":"v2.0.0"}', b"\xff", b"[" * 5000):
                with self.subTest(body_size=len(body)), self.assertRaises(ValueError):
                    self.raw_body = body
                    ota.records()
            self.raw_body = json.dumps([release("v2.0.0")]).encode()
            self.raw_response_url = "https://foreign.fixture.invalid/catalog.json"
            with self.assertRaises(ValueError):
                ota.records()
            self.raw_response_url = self.raw
            bad = release("v2.0.0")
            bad["assets"][0]["browser_download_url"] = "https://foreign.fixture.invalid/archive"
            self.raw_body = json.dumps([bad]).encode()
            result = ota.Releases(platform="linux").status()
            self.assertEqual(result["state"], "unavailable")
            self.assertNotIn("asset", result)


class ReleaseCatalogTests(unittest.TestCase):
    def test_channel_orders_rc_numerically_and_selects_only_matching_platform(self):
        sources = [release("v1.0.0"), release("v1.1.0-rc.2"), release("v1.1.0-rc.10"),
                   release("v2.0.0-rc.1", "windows-x64.zip")]
        stable = ota.select(sources, channel="stable", platform="linux")
        testing = ota.select(sources, channel="testing", platform="linux")
        windows = ota.select(sources, channel="testing", platform="windows")
        self.assertEqual((stable.tag, testing.tag, windows.tag), ("v1.0.0", "v1.1.0-rc.10", "v2.0.0-rc.1"))
        self.assertEqual(windows.name, "apollo-neb-2.0.0-windows-x64.zip")
        self.assertIsNone(ota.select(sources, channel="stable", platform="windows"))

    def test_matching_verified_kernel_variant_wins_only_within_the_selected_tag(self):
        latest = release("v1.2.0-rc.2")
        exact = release("v1.2.0-rc.2", "linux-x64-kernel-6.6.tar.gz")["assets"][0]
        other = release("v1.2.0-rc.2", "linux-x64-kernel-6.12.tar.gz")["assets"][0]
        latest["assets"] += [exact, other]
        older = release("v1.1.0-rc.10", "linux-x64-kernel-6.6.tar.gz")
        def choice(kernel):
            return ota.select([latest, older], channel="testing", platform="linux", kernel=kernel).name

        self.assertEqual(choice("6.6"), "apollo-neb-1.2.0-linux-x64-kernel-6.6.tar.gz")
        self.assertEqual(choice("6.12"), "apollo-neb-1.2.0-linux-x64-kernel-6.12.tar.gz")
        self.assertEqual(choice("6.8"), "apollo-neb-1.2.0-linux.tar.gz")
        exact["digest"] = None
        self.assertEqual(choice("6.6"), "apollo-neb-1.2.0-linux.tar.gz")

    def test_rejects_drafts_wrong_channel_metadata_and_unverifiable_asset(self):
        invalid = [
            {**release("v1.0.0"), "draft": True},
            {**release("v1.0.0"), "prerelease": True},
            {**release("v1.0.0"), "published_at": "yesterday"},
            {**release("v1.0.0"), "html_url": "https://foreign.fixture.invalid/"},
            release("v01.0.0"),
            release("v1.0.0-preview"),
        ]
        for field, value in (("digest", None), ("size", True), ("state", "open"),
                             ("browser_download_url", "https://foreign.fixture.invalid/archive")):
            row = release("v1.0.0")
            row["assets"][0][field] = value
            invalid.append(row)
        duplicate = release("v1.0.0")
        duplicate["assets"].append(duplicate["assets"][0].copy())
        invalid.append(duplicate)
        self.assertFalse(any(ota.select([row], channel="stable", platform="linux") for row in invalid))

    def test_preference_does_not_fetch_or_make_an_older_release_installable(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(ota, "VERSION", "1.1.0-rc.10"), \
                patch.object(ota, "records", return_value=[release("v1.0.0"), release("v1.2.0-rc.2")]) as fetch:
            config = Path(tmp)
            releases = ota.Releases(platform="linux")
            initial = releases.status(config, check=False)
            self.assertEqual((initial["channel"], initial["state"]), ("stable", "unchecked"))
            fetch.assert_not_called()
            self.assertEqual(releases.status(config)["state"], "current")
            self.assertNotIn("asset", releases.status(config))
            self.assertEqual(releases.set_channel(config, "testing"), {"channel": "testing"})
            self.assertEqual(ota.Releases(platform="linux").status(config, check=False)["channel"], "testing")
            candidate = releases.status(config)
            self.assertEqual((candidate["state"], candidate["latest"]), ("available", "v1.2.0-rc.2"))
            self.assertEqual(candidate["asset"]["sha256"], "a" * 64)
            self.assertEqual(candidate["notes"], "Notes are rendered as text.")
            self.assertEqual(releases.set_channel(config, "stable"), {"channel": "stable"})
            self.assertEqual(releases.status(config)["state"], "current")

    def test_apply_requires_a_pinned_newer_release_in_the_selected_channel(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(ota, "VERSION", "1.1.0-rc.10"), \
                patch.object(ota, "host_platform", return_value="linux"), \
                patch.object(ota, "records", return_value=[release("v1.0.0"), release("v1.2.0-rc.2")]) as fetch, \
                patch.object(ota, "download") as download:
            path = Path(tmp)
            with self.assertRaisesRegex(ValueError, "--version"):
                ota.upgrade(path/"config", path/"state", prefix=path, apply=True)
            fetch.assert_not_called()
            with self.assertRaises(ValueError):
                ota.upgrade(path/"config", path/"state", prefix=path, apply=True, tag="v1.2.0-rc.2")
            with self.assertRaises(ValueError):
                ota.upgrade(path/"config", path/"state", prefix=path, apply=True, tag="v1.0.0")
            with redirect_stdout(StringIO()):
                ota.upgrade(path/"config", path/"state", prefix=path)
            download.assert_not_called()

    def test_windows_never_enters_apply_path(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(ota, "host_platform", return_value="windows"), \
                patch.object(ota, "records") as fetch:
            path = Path(tmp)
            with self.assertRaises(ValueError):
                ota.upgrade(path/"config", path/"state", prefix=path, apply=True, tag="v1.0.0")
            fetch.assert_not_called()


class ReleaseInstallTests(unittest.TestCase):
    def test_only_a_verified_archive_with_matching_version_reaches_the_existing_installer(self):
        tag = "v1.2.0-rc.2"
        fixtures = (("1.2.0-rc.2", True, True), ("1.2.0-rc.10", True, False), ("1.2.0-rc.2", False, False))
        for content_version, valid_digest, matches in fixtures:
            with self.subTest(content_version=content_version, valid_digest=valid_digest), tempfile.TemporaryDirectory() as tmp:
                output = BytesIO()
                with tarfile.open(fileobj=output, mode="w:gz") as tar:
                    sources = {"install.sh": b"#!/bin/sh\n", "neb_release.py": f'VERSION = "{content_version}"\n'.encode()}
                    for name, body in sources.items():
                        member = tarfile.TarInfo(f"Apollo.NEB-1.2.0-linux/{name}")
                        member.size = len(body)
                        tar.addfile(member, BytesIO(body))
                payload = output.getvalue()
                row = release(tag)
                row["assets"][0]["size"] = len(payload)
                if valid_digest:
                    row["assets"][0]["digest"] = "sha256:" + hashlib.sha256(payload).hexdigest()
                root = Path(tmp)
                config, state, prefix = (root / name for name in ("config", "state", "prefix"))
                ota.Releases(platform="linux").set_channel(config, "testing")
                with (patch.object(ota, "host_platform", return_value="linux"),
                        patch.object(ota, "records", return_value=[row]),
                        patch.object(ota, "download", side_effect=lambda asset, path: path.write_bytes(payload)),
                        patch.object(ota, "lock_upgrade", return_value=nullcontext()),
                        patch.object(ota.subprocess, "run") as install, redirect_stdout(StringIO())):
                    if matches:
                        ota.upgrade(config, state, prefix=prefix, apply=True, tag=tag)
                        args = install.call_args.args[0]
                        self.assertEqual(args[0], "/bin/sh")
                        self.assertEqual(Path(args[1]).name, "install.sh")
                        self.assertEqual(args[2:], ["--prefix", str(prefix), "--config-dir", str(config),
                                                   "--state-dir", str(state)])
                    else:
                        with self.assertRaises(ValueError):
                            ota.upgrade(config, state, prefix=prefix, apply=True, tag=tag)
                        install.assert_not_called()


    def test_stale_launcher_cannot_downgrade_a_newer_installation(self):
        with (tempfile.TemporaryDirectory() as tmp, patch.object(ota, "host_platform", return_value="linux"),
              patch.object(ota, "records", return_value=[release("v1.1.0")]),
              patch.object(ota, "installed_order", return_value=ota.version("1.2.0")),
              patch.object(ota, "lock_upgrade", return_value=nullcontext()),
              patch.object(ota, "download") as download, redirect_stdout(StringIO())):
            root = Path(tmp)
            with self.assertRaises(ValueError):
                ota.upgrade(root/"config", root/"state", prefix=root, apply=True, tag="v1.1.0")
            download.assert_not_called()


class ReleaseRoutesTests(unittest.TestCase):
    def test_check_and_stream_switch_require_owner_session_and_same_origin_post(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(ota, "records", return_value=[]) as fetch:
            root = Path(tmp)
            service = SimpleNamespace(config=root / "config", releases=ota.Releases(platform="linux"), fleet=None)
            password = "portable release test fixture"
            auth = neb_core.Auth(neb_core.new_login(password), root / "auth.sqlite")
            with neb_app.Server(("127.0.0.1", 0), service, auth, neb_app.HERE / "web", None) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                host = f"127.0.0.1:{server.server_port}"
                origin = f"http://{host}"
                try:
                    with closing(http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)) as conn:
                        def request(method, route, payload=None, cookie=None, ref=origin):
                            headers = {"Host": host, "Origin": ref}
                            if payload is not None:
                                headers["Content-Type"] = "application/json"
                            if cookie is not None:
                                headers["Cookie"] = cookie
                            conn.request(method, route, None if payload is None else json.dumps(payload), headers)
                            response = conn.getresponse()
                            return response.status, json.loads(response.read()), response.getheader("Set-Cookie")

                        route = "/api/releases/channel"
                        self.assertEqual(request("GET", "/api/releases?check=0")[0], 401)
                        self.assertEqual(request("POST", route, {"channel": "testing"})[0], 401)
                        _, _, cookie = request("POST", "/api/login", {"password": password})
                        cookie = cookie.split(";", 1)[0]
                        self.assertEqual(request("GET", "/api/releases?check=0", cookie=cookie)[1]["channel"], "stable")
                        self.assertEqual(request("POST", route, {"channel": "testing"}, cookie,
                                                 ref="https://foreign.fixture.invalid")[0], 403)
                        self.assertEqual(request("POST", route, {"channel": "testing", "apply": True}, cookie)[0], 400)
                        self.assertEqual(request("POST", route, {"channel": "private"}, cookie)[0], 409)
                        self.assertEqual(request("POST", route, {"channel": "testing"}, cookie)[:2],
                                         (200, {"channel": "testing"}))
                        self.assertEqual(request("GET", "/api/releases?check=0", cookie=cookie)[1]["channel"], "testing")
                        self.assertEqual(request("POST", "/api/releases/apply", {"tag": "v1.0.0"}, cookie)[0], 404)
                        fetch.assert_not_called()
                finally:
                    server.shutdown()
                    thread.join(timeout=3)
