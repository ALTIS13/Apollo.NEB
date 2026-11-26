import copy
import unittest

from neb_overview import summarize


NOW = 1700000000


def worker(*, state="running", **readings):
    return {"id": "gpu0", "state": state, **readings}


def local(workers, *, stamp=NOW - 2, controller=False):
    return {
        "time": stamp,
        "capabilities": {"local_workers": not controller},
        "profile": {"wallet": "private-fixture"},
        "workers": workers,
    }


def remote(ident, workers, *, status="online", seen=NOW - 4, **details):
    return {"id": ident * 24, "label": "private-fixture", "status": status,
            "last_seen": seen, "workers": workers, "commands": [], **details}


class OverviewTests(unittest.TestCase):
    def test_unified_overview_excludes_unregistered_and_stale_rows_without_leaking_metadata(self):
        mine = local([
            worker(device="0000:01:00.0", rate_hs=2e6, watts=84, edge=56, hot=74, hbm=80,
                   accepted=4, rejected=0),
            worker(state="blocked", device=None),
        ])
        current = remote("a", [worker(rate_hs=3.5e6, watts=145, edge=64, hot=82, hbm=85,
                                      accepted=7, rejected=1)])
        old = [worker(rate_hs=10e6, watts=270, edge=95, hot=110, hbm=101, accepted=600, rejected=40)]
        fleet = {"nodes": [current, remote("a", old, status="offline", seen=NOW - 200),
                           remote("b", old, status="offline", seen=NOW - 180),
                           remote("c", old, status="revoked"),
                           remote("d", old, status="pending"),
                           remote("e", old, status="expired")]}
        before = copy.deepcopy((mine, fleet))
        result = summarize(mine, fleet, now=NOW)
        self.assertEqual(result, {
            "time": NOW - 2,
            "nodes": {"online": 2, "total": 3},
            "workers": {"running": 2, "total": 3, "attention": 1},
            "hashrate_hs": 5.5e6,
            "watts": 229,
            "temperatures": {"edge": 64, "hot": 82, "hbm": 85},
            "shares": {"accepted": 11, "rejected": 1},
        })
        self.assertEqual((mine, fleet), before)

    def test_unknown_readings_do_not_turn_into_partial_totals(self):
        mine = local([worker(device="0000:01:00.0", rate_hs=2e6, watts=0, edge=55, hot=77, hbm=60,
                             accepted=0, rejected=0)])
        incomplete = remote("a", [worker(rate_hs=None, watts=145, edge=64, hot=None, hbm=0,
                                         accepted=1, rejected=None)])
        result = summarize(mine, {"nodes": [incomplete]}, now=NOW)
        self.assertIsNone(result["hashrate_hs"])
        self.assertEqual(result["watts"], 145)
        self.assertEqual(result["temperatures"], {"edge": 64, "hot": None, "hbm": 60})
        self.assertEqual(result["shares"], {"accepted": 1, "rejected": None})
        self.assertEqual(result["workers"], {"running": 2, "total": 2, "attention": 0})

        incomplete["workers"][0]["watts"] = None
        self.assertIsNone(summarize(mine, {"nodes": [incomplete]}, now=NOW)["watts"])

    def test_windows_gpu_sensors_add_power_and_edge_without_adding_workers(self):
        mine = local([worker(device="0000:01:00.0", rate_hs=2e6, watts=88, edge=47, hot=70, hbm=75,
                             accepted=4, rejected=0)])
        windows = remote("b", [], hardware={"devices": [{"id": "00000000:02:00.0", "label": "RTX 5070",
                                                       "edge": 53, "watts": 18}]})
        result = summarize(mine, {"nodes": [windows]}, now=NOW)
        self.assertEqual(result["nodes"], {"online": 2, "total": 2})
        self.assertEqual(result["workers"], {"running": 1, "total": 1, "attention": 0})
        self.assertEqual(result["hashrate_hs"], 2e6)
        self.assertEqual(result["watts"], 106)
        self.assertEqual(result["temperatures"], {"edge": 53, "hot": 70, "hbm": 75})
        self.assertNotIn("RTX 5070", repr(result))

    def test_atlas_combines_common_metrics_without_counting_gpus_as_workers(self):
        mine = local([worker(device="0000:01:00.0", rate_hs=2e6, watts=88, edge=47, hot=70, hbm=75,
                             accepted=4, rejected=0)])
        atlas = {"status": "online", "time": NOW - 1, "rate_hs": 3e6, "watts": 100,
                 "gpu_counts": {"active": 2, "total": 6}, "temperatures": {"edge": 53, "hot": 79, "hbm": None},
                 "shares": {"accepted": 7, "rejected": 1}}
        result = summarize(mine, {}, now=NOW, atlas=atlas)
        self.assertEqual(result["nodes"], {"online": 2, "total": 2})
        self.assertEqual(result["atlas_gpus"], {"active": 2, "total": 6})
        self.assertEqual(result["workers"], {"running": 1, "total": 1, "attention": 0})
        self.assertEqual(result["hashrate_hs"], 5e6)
        self.assertEqual(result["watts"], 188)
        self.assertEqual(result["temperatures"], {"edge": 53, "hot": 79, "hbm": 75})
        self.assertEqual(result["shares"], {"accepted": 11, "rejected": 1})
        stale = summarize(mine, {}, now=NOW, atlas={**atlas, "time": NOW - 30})
        self.assertEqual(stale["nodes"], {"online": 1, "total": 2})
        self.assertEqual(stale["hashrate_hs"], 2e6)
        self.assertNotIn("atlas_gpus", stale)

    def test_controller_is_not_a_local_mining_node(self):
        controller = local([worker(rate_hs=10e6, watts=100)], controller=True)
        node = remote("a", [worker(rate_hs=0, watts=0, edge=0, hot=0, hbm=0, accepted=0, rejected=0)])
        result = summarize(controller, {"nodes": [node]}, now=NOW)
        self.assertEqual(result, {
            "time": NOW - 4, "nodes": {"online": 1, "total": 1},
            "workers": {"running": 1, "total": 1, "attention": 0},
            "hashrate_hs": 0, "watts": 0, "temperatures": {"edge": 0, "hot": 0, "hbm": 0},
            "shares": {"accepted": 0, "rejected": 0},
        })

    def test_stale_and_future_snapshots_count_as_registered_but_do_not_supply_readings(self):
        old = [worker(rate_hs=2e6, watts=99, accepted=1)]
        mine = local(old, stamp=NOW - 30)
        fleet = {"nodes": [remote("a", old, seen=NOW - 30), remote("b", old, seen=NOW + 2),
                           remote("c", old, seen=None)]}
        result = summarize(mine, fleet, now=NOW)
        self.assertEqual(result, {
            "time": None, "nodes": {"online": 0, "total": 4},
            "workers": {"running": None, "total": None, "attention": None},
            "hashrate_hs": None, "watts": None, "temperatures": {"edge": None, "hot": None, "hbm": None},
            "shares": {"accepted": None, "rejected": None},
        })

    def test_unconfigured_fresh_source_has_known_zero_worker_counts(self):
        result = summarize(local([]), {"nodes": []}, now=NOW)
        self.assertEqual(result["nodes"], {"online": 1, "total": 1})
        self.assertEqual(result["workers"], {"running": 0, "total": 0, "attention": 0})
        self.assertIsNone(result["hashrate_hs"])
        self.assertIsNone(result["watts"])

    def test_missing_worker_snapshot_keeps_connection_coverage_but_prevents_partial_measurements(self):
        mine = local([worker(device="gpu", rate_hs=22, watts=15)])
        result = summarize(mine, {"nodes": [remote("a", None)]}, now=NOW)
        self.assertEqual(result["nodes"], {"online": 2, "total": 2})
        self.assertEqual(result["workers"], {"running": None, "total": None, "attention": None})
        self.assertIsNone(result["hashrate_hs"])
        self.assertIsNone(result["watts"])

    def test_stopped_worker_preserves_measured_zero_and_absent_share_counters(self):
        mine = local([worker(state="stopped", device="gpu", watts=0, edge=0, hot=float("nan"),
                             hbm=True, accepted=100, rejected=50)])
        result = summarize(mine, {"nodes": []}, now=NOW)
        self.assertEqual(result["workers"], {"running": 0, "total": 1, "attention": 0})
        self.assertEqual(result["hashrate_hs"], 0)
        self.assertEqual(result["watts"], 0)
        self.assertEqual(result["temperatures"], {"edge": 0, "hot": None, "hbm": None})
        self.assertEqual(result["shares"], {"accepted": None, "rejected": None})

    def test_two_arguments_use_registered_status_without_waiting_on_a_clock(self):
        fleet = {"nodes": [remote("a", [worker(rate_hs=1)], seen=NOW - 60),
                           remote("b", [worker(rate_hs=100)], status="offline", seen=NOW - 2)]}
        result = summarize(None, fleet)
        self.assertEqual(result["time"], NOW - 60)
        self.assertEqual(result["nodes"], {"online": 1, "total": 2})
        self.assertEqual(result["hashrate_hs"], 1)


if __name__ == "__main__":
    unittest.main()
