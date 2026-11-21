import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import neb_runtime as runtime


PROFILE = {"wallet": "fixture-address", "worker": "fake_rig", "pool": "stratum+tcp://example.invalid:3412"}
GPU = "0000:04:00.0"


class WorkerConfigTests(unittest.TestCase):
    def test_rejects_untrusted_fields_and_bad_templates(self):
        base = {"id": "a", "adapter": "custom", "binary": "/bin/false", "args": []}
        for mutation in ({"binary": "miner"}, {"id": "../a"}, {"password": "no"},
                         {"args": ["{wallet.__class__}"]}, {"args": ["{wallet!r}"]},
                         {"device": GPU, "args": ["--device=0"]}, {"device_index": 0}):
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                runtime.validate_workers([{**base, **mutation}])
        self.assertEqual(runtime.validate_workers([{**base, "args": ["--id={worker}"]}])[0]["label"], "a")

    def test_rejects_duplicate_worker_ids_or_gpu_bindings(self):
        base = {"id": "a", "adapter": "custom", "binary": "/bin/false", "args": ["{device}"]}
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
        manager = runtime.Manager(
            [{"id": "test", "adapter": "custom", "binary": str(exe), "args": [], **options}],
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

    def test_refuses_credentials_without_spawning(self):
        manager = self.manager()
        with self.assertRaises(ValueError) as caught:
            manager.start("test", {**PROFILE, "pool": "stratum+tcp://user:not-allowed@example.invalid:3412"})
        self.assertNotIn("not-allowed", str(caught.exception))
        self.assertNotEqual(manager.snapshot({})[0]["state"], "running")

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
            self.assertTrue(any(event["type"] == "thermal" for event in manager.drain_events()))

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
