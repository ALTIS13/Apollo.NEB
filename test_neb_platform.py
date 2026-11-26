import base64
from contextlib import ExitStack, nullcontext, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, call, patch

import neb
import neb_platform as publisher


ORIGIN = "https://api.apollot.ru"
NOW = 1712345678
TOKEN = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode("ascii")


def status():
    return {
        "time": NOW - 2,
        "workers": {"total": 4, "running": 2, "attention": 1, "ids": ["private"]},
        "hashrate_hs": 123456789.25,
        "watts": 58,
        "temperatures": {"edge": 41, "hot": 63.5, "hbm": None, "device": "private"},
        "shares": {"accepted": 7, "rejected": 0, "wallet": "private"},
        "profile": {"wallet": "private"},
        "devfee": {"enabled": True, "state": "counting"},
    }


class PublisherIntegrationTests(unittest.TestCase):
    def test_portable_serve_runs_publisher_only_with_valid_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config"
            config.mkdir()
            state = Path(directory) / "state"
            for settings, controller, expected in (
                (None, False, False),
                ({"enabled": False}, False, False),
                ({"enabled": True, "origin": ORIGIN}, True, False),
                ({"enabled": True, "origin": ORIGIN + "/other"}, False, False),
                ({"enabled": True, "origin": ORIGIN}, False, True),
            ):
                with self.subTest(settings=settings, controller=controller), ExitStack() as stack:
                    if settings is not None:
                        (config / "platform-export.json").write_text(json.dumps(settings), encoding="utf-8")
                    stack.enter_context(redirect_stdout(io.StringIO()))
                    stack.enter_context(patch.object(neb, "paths", return_value=(config, state)))
                    stack.enter_context(patch.object(neb, "private"))
                    stack.enter_context(patch.object(neb, "lock_state", return_value=nullcontext()))
                    stack.enter_context(patch.object(neb, "linux_boot", return_value=(None, None)))
                    stack.enter_context(patch.object(neb, "release_support"))
                    stack.enter_context(patch.object(neb, "require_controller_settings"))
                    stack.enter_context(patch.object(neb, "signal"))
                    stack.enter_context(patch.object(neb.core, "read", side_effect=lambda path, default=None: {
                        "login.json": {}, "settings.json": {"port": 18783},
                    }.get(path.name, default)))
                    stack.enter_context(patch.object(neb.core, "Auth"))
                    service = stack.enter_context(patch.object(neb.neb_app, "Service")).return_value
                    stack.enter_context(patch.object(neb.neb_app, "Server"))
                    stack.enter_context(patch("neb_fleet.Fleet"))
                    stack.enter_context(patch.object(neb.neb_setup, "Setup"))
                    threads = stack.enter_context(patch.object(neb.threading, "Thread"))
                    log = stack.enter_context(patch.object(neb.logging, "warning"))
                    neb.serve(None, controller=controller)
                    exporters = [call.kwargs for call in threads.call_args_list
                                 if call.kwargs["name"] == "neb-platform-publisher"]
                    self.assertEqual(len(exporters), int(expected))
                    if expected:
                        self.assertEqual(exporters[0]["args"],
                                         (service.public_status, config / "platform-export.token", service.stopping))
                    if settings and settings.get("origin") == ORIGIN + "/other":
                        log.assert_called_once_with("Apollo Platform publisher: invalid configuration")


class PublisherTests(unittest.TestCase):
    def test_config_requires_one_explicit_flag_and_the_fixed_origin(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory)
            path = config / "platform-export.json"
            for invalid in (
                '{"enabled": true}',
                '{"enabled": false, "enabled": true, "origin": "https://api.apollot.ru"}',
                '{"enabled": 1, "origin": "https://api.apollot.ru"}',
                '{"enabled": true, "origin": "https://api.apollot.ru:443"}',
                '{"enabled": true, "origin": "https://api.apollot.ru", "unexpected": "private"}',
                '{"enabled": true, "origin": "https://api.apollot.ru"}' + " " * 513,
            ):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    path.write_text(invalid, encoding="utf-8")
                    publisher.configured_token(config)

    def test_retry_preserves_original_timestamp_and_projects_only_contract_fields(self):
        source = Mock(return_value=status())
        stopping = Mock()
        stopping.is_set.return_value = stopping.wait.return_value = False
        with patch.object(publisher.time, "time", return_value=NOW), \
             patch.object(publisher, "load_token", return_value=TOKEN), \
             patch.object(publisher, "post", side_effect=["unavailable", "sent"]) as post:
            self.assertEqual(publisher.publish_once(source, Path("token"), stopping), "sent")
        source.assert_called_once()
        stopping.wait.assert_called_once_with(2)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0], post.call_args_list[1])
        self.assertEqual(json.loads(post.call_args.args[-1]), {
            "schema_version": 1,
            "sample": {"time": NOW - 2, "workers": {"total": 4, "running": 2, "attention": 1},
                       "hashrate_hs": 123456789.25, "watts": 58,
                       "temperatures": {"edge": 41, "hot": 63.5, "hbm": None},
                       "shares": {"accepted": 7, "rejected": 0}},
        })

    def test_invalid_or_stale_measurements_are_never_posted(self):
        for changes in ({"time": NOW - 121}, {"time": NOW + 6}, {"watts": float("inf")}, {"hashrate_hs": False},
                        {"workers": {"total": 1, "running": 1, "attention": 1}}):
            with self.subTest(changes=changes), patch.object(publisher.time, "time", return_value=NOW), \
                 patch.object(publisher, "load_token") as token, patch.object(publisher, "post") as post:
                result = publisher.publish_once(lambda: {**status(), **changes}, Path("token"), neb.threading.Event())
                self.assertEqual(result, "sample-unavailable")
                token.assert_not_called()
                post.assert_not_called()

    def test_stop_during_retry_prevents_the_second_post(self):
        stopping = Mock()
        stopping.is_set.return_value = False
        with patch.object(publisher.time, "time", return_value=NOW), \
             patch.object(publisher, "load_token", return_value=TOKEN), \
             patch.object(publisher, "post", return_value="unavailable") as post:
            stopping.wait.return_value = True
            self.assertEqual(publisher.publish_once(status, Path("token"), stopping), "stopped")
        post.assert_called_once()

    def test_interval_and_bounded_startup_receipts(self):
        stopping = Mock()
        stopping.is_set.side_effect = [False, False, False, True]
        output = io.StringIO()
        with patch.object(publisher.time, "monotonic", side_effect=[1, 5, 33, 35, 63, 64]), \
             redirect_stdout(output), \
             patch.object(publisher, "publish_once", return_value="sent") as once:
            publisher.run(status, Path("token"), stopping)
        self.assertEqual(once.call_count, 3)
        self.assertEqual(stopping.wait.call_args_list, [call(26), call(28), call(29)])
        self.assertEqual(output.getvalue().splitlines(), ["Apollo Platform publisher received HTTP 204"] * 2)

    @unittest.skipUnless(os.name == "posix", "Linux credential file semantics")
    def test_only_the_owner_private_regular_file_can_supply_a_canonical_token(self):
        with tempfile.TemporaryDirectory() as directory:
            token = Path(directory) / "platform-export.token"
            token.write_text(TOKEN + "\n", encoding="ascii")
            token.chmod(0o600)
            self.assertEqual(publisher.load_token(token), TOKEN)

            token.chmod(0o644)
            with self.assertRaises(ValueError):
                publisher.load_token(token)
            token.chmod(0o600)
            symlink = token.with_suffix(".symlink")
            symlink.symlink_to(token)
            with self.assertRaises(ValueError):
                publisher.load_token(symlink)
            hardlink = token.with_suffix(".link")
            hardlink.hardlink_to(token)
            with self.assertRaises(ValueError):
                publisher.load_token(token)
            hardlink.unlink()
            token.write_text(TOKEN[:-1] + "B", encoding="ascii")
            with self.assertRaises(ValueError):
                publisher.load_token(token)
