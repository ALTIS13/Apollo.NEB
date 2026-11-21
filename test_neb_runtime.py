from itertools import count
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import neb_runtime as runtime


PROFILE = {"wallet": "fixture-address", "worker": "fake_rig", "pool": "stratum+tcp://example.invalid:3412"}
GPU = "0000:04:00.0"


class WorkerConfigTests(unittest.TestCase):
    def test_rejects_untrusted_fields_and_bad_templates(self):
        base = {"id": "a", "adapter": "custom", "binary": "/bin/false", "args": ["{wallet}", "{pool}"]}
        for mutation in ({"binary": "miner"}, {"id": "../a"}, {"password": "no"},
                         {"args": ["{wallet.__class__}"]}, {"args": ["{wallet!r}"]},
                         {"args": ["{wallet}"]}, {"args": ["{pool}"]},
                         {"device": GPU, "args": ["{wallet}", "{pool}", "--device=0"]}, {"device_index": 0}):
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                runtime.validate_workers([{**base, **mutation}])
        self.assertEqual(runtime.validate_workers([{**base, "args": ["{wallet}", "{pool}", "--id={worker}"]}])[0]["label"], "a")

    def test_rejects_duplicate_worker_ids_or_gpu_bindings(self):
        base = {"id": "a", "adapter": "custom", "binary": "/bin/false", "args": ["{wallet}", "{pool}", "{device}"]}
        for workers in ([base, base], [{**base, "device": GPU}, {**base, "id": "b", "device": GPU}]):
            with self.assertRaises(ValueError):
                runtime.validate_workers(workers)

    def test_cuda_uses_full_uuid_to_mask_the_requested_card_and_local_ordinal_zero(self):
        worker = runtime.validate_workers([dict(id="cuda", adapter="suprminer-cuda", binary="/bin/true",
                                                device=GPU, device_index=7)])[0]
        uuid = "GPU-12345678-9abc-0000-0000-000000000001"
        card, env = {"vendor": "nvidia", "index": 7, "_uuid": uuid}, {}
        argv = runtime._argv(worker, PROFILE, Path("/bin/true"), card, env)
        self.assertEqual(env["CUDA_VISIBLE_DEVICES"], uuid)
        self.assertEqual(argv[1:], ["-a", "neb", "-o", PROFILE["pool"], "-u", "fixture-address.fake_rig",
                                    "-p", "x", "-d", "0"])
        card["index"] = 6
        with self.assertRaises(ValueError):
            runtime._argv(worker, PROFILE, Path("/bin/true"), card, {})

    def test_opencl_refuses_driver_with_ambiguous_or_unfiltered_device_binding(self):
        worker = runtime.validate_workers([dict(id="opencl", adapter="suprminer-opencl", binary="/bin/true",
                                                device=GPU, device_index=1)])[0]
        foreign = "0000:03:00.0"
        for mask_result in ([foreign, GPU], [foreign], [GPU, None]):
            with patch.object(runtime, "_opencl_devices", side_effect=[[foreign, GPU], mask_result]):
                with self.assertRaises(ValueError):
                    runtime._argv(worker, PROFILE, Path("/bin/true"), {"vendor": "amd"}, {})
        with patch.object(runtime, "_opencl_devices", side_effect=[[foreign, GPU], [GPU]]):
            env = {}
            argv = runtime._argv(worker, PROFILE, Path("/bin/true"), {"vendor": "amd"}, env)
        self.assertEqual(env["GPU_DEVICE_ORDINAL"], "1")
        self.assertEqual(argv[-2:], ["-d", "0"])


class WorkerJournalTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        workers = [dict(id=ident, adapter="custom", binary="/offline-miner",
                        args=["{wallet}", "{pool}"], log_format="json-lines")
                   for ident in ("one", "two")]
        self.manager = runtime.Manager(workers, Path(scratch.name))

    def record(self, output, clock):
        with tempfile.TemporaryFile() as stream:
            stream.write(output)
            stream.seek(0)
            process = SimpleNamespace(stdout=stream)
            # Substitute only Linux launch and poll boundaries. The reader still consumes real bytes.
            with (patch.object(runtime.sys, "platform", "linux"),
                  patch.object(runtime, "_trusted_file", return_value=Path("/offline-miner")),
                  patch.object(runtime, "_spawn_guard", return_value=(process, -1)),
                  patch.object(runtime.selectors, "DefaultSelector") as selector,
                  patch.object(runtime, "time", SimpleNamespace(monotonic=clock, time=time.time))):
                selector.return_value.__enter__.return_value.select.return_value = [True]
                self.manager.start("one", PROFILE)
                child = self.manager._live["one"]
                child.thread.join(timeout=2)
                self.assertFalse(child.thread.is_alive())
                with (patch.object(child, "finished", return_value=False),
                      patch.object(child, "stop", return_value=b"")):
                    self.manager.stop("one")

    def test_journal_keeps_first_and_latest_observation_from_noisy_miner(self):
        output = (b"fixture-secret\n" + b"x" * 10000 + b"\n"
                  b'{"rate_hs":125,"accepted":1,"detail":"fixture-secret"}\n'
                  + b'{"rate_hs":200}\n' * 180
                  + b'{"rejected":3}\n{"accepted":42,"wallet":"fixture-address"}\n')
        self.record(output, lambda: 100)
        self.assertTrue(callable(getattr(self.manager, "journal", None)), "Configured workers need a journal")
        rows = self.manager.journal("one")
        self.assertEqual([row["type"] for row in rows], ["stop", "telemetry", "telemetry", "start"])
        self.assertEqual({k: v for k, v in rows[1].items() if k != "time"},
                         {"type": "telemetry", "rate_hs": 200, "accepted": 42, "rejected": 3})
        self.assertEqual({k: v for k, v in rows[2].items() if k != "time"},
                         {"type": "telemetry", "rate_hs": 125, "accepted": 1})
        self.assertTrue(all(isinstance(row["time"], int) for row in rows))
        self.assertEqual(self.manager.journal("two"), [])
        with self.assertRaisesRegex(runtime.Refusal, "Unknown worker"):
            self.manager.journal("missing")

    def test_journal_bounds_sustained_telemetry_to_the_latest_rows(self):
        output = b"".join(json.dumps({"accepted": i}).encode() + b"\n" for i in range(130))
        self.record(output, count(100, 10).__next__)
        self.assertTrue(callable(getattr(self.manager, "journal", None)), "Configured workers need a journal")
        rows = self.manager.journal("one")
        self.assertGreater(len(rows), 1)
        self.assertLessEqual(len(rows), 80)
        self.assertEqual(rows[0]["type"], "stop")
        self.assertEqual(rows[1]["accepted"], 129)
        self.assertGreater(rows[-1]["accepted"], 0)


@unittest.skipUnless(sys.platform == "linux", "Linux process and sysfs contracts")
class RuntimeTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)

    def manager(self, body=None, **options):
        if body is None:
            body = "import time\nwhile True: time.sleep(0.02)"
        exe = self.root / "fake miner"
        exe.write_text(f"#!{Path(sys.executable).resolve()}\n{body}\n", encoding="utf-8")
        exe.chmod(0o700)
        opts = {"args": ["{wallet}", "{pool}"], **options}
        if opts["args"] == ["{device}"]:
            opts["args"] += ["{wallet}", "{pool}"]
        manager = runtime.Manager(
            [{"id": "test", "adapter": "custom", "binary": str(exe), **opts}],
            self.root / "state",
        )
        self.addCleanup(manager.close)
        return manager

    def wait_row(self, manager, predicate):
        for _ in range(100):
            row = manager.snapshot({})[0]
            if predicate(row):
                return row
            time.sleep(0.02)
        self.fail(f"worker did not reach condition: {row}")

    def test_argv_is_literal_and_only_numeric_metrics_escape(self):
        observed = self.root / "argv.json"
        manager = self.manager(
            f"import json, sys, time\n"
            f"from pathlib import Path\n"
            f"Path({str(observed)!r}).write_text(json.dumps(sys.argv[1:]))\n"
            "print('X' * 10000, flush=True)\n"
            "print(json.dumps(dict(rate_hs=1234.5, accepted=7, rejected=2, detail=sys.argv[1])), flush=True)\n"
            "while True: time.sleep(0.02)",
            args=["{wallet}.{worker}", "{pool}", "$(not-a-shell)"], log_format="json-lines",
        )
        manager.start("test", PROFILE)
        row = self.wait_row(manager, lambda row: row["accepted"] == 7)
        self.assertEqual(json.loads(observed.read_text()),
                         ["fixture-address.fake_rig", "stratum+tcp://example.invalid:3412", "$(not-a-shell)"])
        self.assertEqual((row["state"], row["rate_hs"], row["rejected"]), ("running", 1234.5, 2))
        self.assertNotIn("fixture-address", json.dumps([row, manager.drain_events()]))
        manager.stop("test")
        self.assertEqual(manager.snapshot({})[0]["state"], "stopped")
        self.assertEqual([row["type"] for row in manager.journal("test")], ["stop", "telemetry", "start"])
        self.assertNotIn("fixture-address", json.dumps(manager.journal("test")))

    def test_refuses_credentials_without_spawning(self):
        manager = self.manager()
        with self.assertRaises(ValueError) as caught:
            manager.start("test", {**PROFILE, "pool": "stratum+tcp://user:not-allowed@example.invalid:3412"})
        self.assertNotIn("not-allowed", str(caught.exception))
        self.assertNotEqual(manager.snapshot({})[0]["state"], "running")

    def test_pidfd_unavailable_prevents_launch(self):
        manager = self.manager()
        with patch.object(runtime.os, "pidfd_open", side_effect=OSError("unavailable")):
            with self.assertRaises(ValueError):
                manager.start("test", PROFILE)
        self.assertEqual(manager.snapshot({})[0]["state"], "blocked")

    def test_max_seconds_stops_miner_without_panel_polling(self):
        started, lingered = self.root / "leased", self.root / "late"
        lease, late_marker_delay = 3.0, 4.0
        manager = self.manager(
            "import time\nfrom pathlib import Path\n"
            f"Path({str(started)!r}).touch()\n"
            f"time.sleep({late_marker_delay})\nPath({str(lingered)!r}).touch()\n"
            "while True: time.sleep(0.02)"
        )
        manager.start("test", PROFILE, max_seconds=lease)
        # Observe the fixture's marker without polling the manager.
        deadline = time.monotonic() + lease
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(started.exists())
        time.sleep(late_marker_delay + 0.5)
        self.assertFalse(lingered.exists())
        expired = manager.snapshot({})[0]
        self.assertEqual(expired["state"], "stopped")
        self.assertEqual(expired["exit_reason"], "time-limit")
        self.assertTrue(any(e["type"] == "exit" and e["status"] == "expired" for e in manager.drain_events()))
        manager.start("test", PROFILE)
        self.assertIsNone(manager.snapshot({})[0].get("exit_reason"))
        manager.stop("test")
        self.assertIsNone(manager.snapshot({})[0].get("exit_reason"))

    def test_sigkill_of_panel_kills_its_owned_miner_without_shutdown(self):
        started, lingered = self.root / "launched", self.root / "orphan"
        exe = self.root / "owned-miner"
        exe.write_text(f"#!{Path(sys.executable).resolve()}\n"
                       "import time\nfrom pathlib import Path\n"
                       f"Path({str(started)!r}).touch()\ntime.sleep(0.9)\nPath({str(lingered)!r}).touch()\n")
        exe.chmod(0o700)
        command = ("import time; from pathlib import Path; from neb_runtime import Manager; "
                   f"m=Manager([dict(id='test',adapter='custom',binary={str(exe)!r},args=['{{wallet}}','{{pool}}'])],"
                   f"Path({str(self.root / 'worker-state')!r})); m.start('test',{PROFILE!r}); time.sleep(10)")
        parent = subprocess.Popen([sys.executable, "-c", command], cwd=Path(runtime.__file__).parent)
        try:
            for _ in range(100):
                if started.exists():
                    break
                time.sleep(0.01)
            self.assertTrue(started.exists())
            parent.kill()
            parent.wait(timeout=3)
            time.sleep(1.0)
            self.assertFalse(lingered.exists())
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait(timeout=3)

    def test_exiting_leader_cleans_owned_group_not_unrelated_process(self):
        appeared = self.root / "child-alive"
        escaped = self.root / "child-lingered"
        manager = self.manager(
            "import os, signal, time\n"
            "from pathlib import Path\n"
            "if os.fork() == 0:\n"
            "  signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"  Path({str(appeared)!r}).touch()\n"
            "  time.sleep(0.6)\n"
            f"  Path({str(escaped)!r}).touch()\n"
            "  os._exit(0)\n"
            f"while not Path({str(appeared)!r}).exists(): time.sleep(0.01)\n"
            "os._exit(7)"
        )
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"], start_new_session=True)
        try:
            manager.start("test", PROFILE)
            row = self.wait_row(manager, lambda row: row["state"] == "failed")
            self.assertIsNone(row["rate_hs"])
            self.assertTrue(appeared.exists())
            self.assertTrue(any(event["type"] == "exit" for event in manager.drain_events()))
            self.assertEqual(manager.journal("test")[0]["type"], "exit")
            time.sleep(0.65)
            self.assertFalse(escaped.exists())
            self.assertIsNone(unrelated.poll())
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=3)

    def test_gpu_missing_temperature_blocks_then_running_gpu_loses_telemetry(self):
        card = {"device": GPU, "vendor": "amd", "edge": 60, "hot": 75, "hbm": None, "watts": None}
        manager = self.manager(device=GPU, args=["{device}"])
        with patch.object(runtime, "discover_devices", side_effect=lambda: [dict(card)]):
            card["hot"] = None
            with self.assertRaises(ValueError):
                manager.start("test", PROFILE)
            self.assertEqual(manager.snapshot({})[0]["state"], "blocked")
            card["hot"] = 75
            manager.start("test", PROFILE)
            self.assertEqual(manager.snapshot({})[0]["state"], "running")
            card["edge"] = None
            self.assertEqual(manager.snapshot({})[0]["state"], "protection")
            card["edge"] = 60
            self.assertEqual(manager.snapshot({})[0]["state"], "protection")
            self.assertTrue(any(event["type"] == "thermal" and event["status"] == "stopped"
                                for event in manager.drain_events()))
            self.assertEqual(manager.journal("test")[0]["type"], "thermal")

    def test_thermal_signals_all_cards_before_first_wait_and_exposes_unconfirmed_stop(self):
        other = "0000:05:00.0"
        cards = [dict(device=bdf, vendor="amd", edge=60, hot=75) for bdf in (GPU, other)]
        mine = self.root / "two-worker-fixture"
        mine.write_text(f"#!{Path(sys.executable).resolve()}\nimport time\nwhile True: time.sleep(0.02)\n")
        mine.chmod(0o700)
        manager = runtime.Manager([
            dict(id=label, adapter="custom", binary=str(mine), device=card["device"],
                 args=["{device}", "{wallet}", "{pool}"])
            for label, card in zip(("first", "second"), cards)], self.root / "two")
        self.addCleanup(manager.close)
        with patch.object(runtime, "discover_devices", side_effect=lambda: [dict(card) for card in cards]):
            for label in ("first", "second"):
                manager.start(label, PROFILE)
            first, second = manager._live["first"], manager._live["second"]
            order = []
            second_signal = second._signal

            def signal_second(signum):
                order.append("signal-second")
                second_signal(signum)

            def wait_first(*_args, **_kwargs):
                order.append("wait-first")
                raise RuntimeError("simulated kernel-stalled child")

            for card in cards:
                card["edge"] = 81
            with patch.object(first, "stop", side_effect=wait_first), patch.object(second, "_signal", side_effect=signal_second):
                states = [row["state"] for row in manager.snapshot({})]
            self.assertEqual(order[0], "signal-second")
            self.assertEqual(states, ["protection", "protection"])
            thermal = {e["worker"]: e["status"] for e in manager.drain_events() if e["type"] == "thermal"}
            self.assertEqual(thermal, {"first": "unconfirmed", "second": "stopped"})

    def test_replaced_gpu_at_same_pci_address_triggers_protection(self):
        card = {"device": GPU, "vendor": "amd", "_identity": (12, "amdgpu"), "edge": 50,
                "hot": 70, "hbm": None, "watts": None}
        manager = self.manager(device=GPU, args=["{device}"])
        with patch.object(runtime, "discover_devices", side_effect=lambda: [dict(card)]):
            manager.start("test", PROFILE)
            card["_identity"] = (13, "amdgpu")
            self.assertEqual(manager.snapshot({})[0]["state"], "protection")

    def test_existing_limit_protects_prelaunch_and_exact_threshold_stops(self):
        card = {"device": GPU, "vendor": "amd", "edge": 79, "hot": 99, "hbm": None, "watts": None}
        manager = self.manager(device=GPU, args=["{device}"])
        with patch.object(runtime, "discover_devices", side_effect=lambda: [dict(card)]):
            manager.snapshot({"edge_limit": 79, "hot_limit": 100})
            with self.assertRaises(ValueError):
                manager.start("test", PROFILE)
            manager.snapshot({})
            manager.start("test", PROFILE)
            card["hot"] = 100
            self.assertEqual(manager.snapshot({})[0]["state"], "protection")

    def test_enabled_hbm_limit_requires_readable_sensor(self):
        card = {"device": GPU, "vendor": "amd", "edge": 55, "hot": 60, "hbm": None, "watts": None}
        manager = self.manager(device=GPU, args=["{device}"])
        with patch.object(runtime, "discover_devices", return_value=[card]):
            manager.snapshot({"hbm_limit": 85})
            with self.assertRaisesRegex(ValueError, "Memory temperature is unavailable"):
                manager.start("test", PROFILE)


class DiscoveryTests(unittest.TestCase):
    def test_amd_hwmon_labels_preserve_sensor_meaning_when_channels_are_reordered(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        hwmon = root / "hwmon" / "hwmon4"
        hwmon.mkdir(parents=True)
        for file, content in {"name": "amdgpu", "temp1_label": "junction", "temp1_input": "91200",
                              "temp2_label": "edge", "temp2_input": "70200", "temp3_label": "mem",
                              "temp3_input": "83800", "power1_average": "125100000"}.items():
            (hwmon / file).write_text(content)
        row = runtime._hwmon(root, "amdgpu")
        self.assertEqual(row, {"edge": 70.2, "hot": 91.2, "hbm": 83.8, "watts": 125.1})


if __name__ == "__main__":
    unittest.main()
