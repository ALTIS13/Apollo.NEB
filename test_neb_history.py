import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from neb_history import Archive


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "history.sqlite"

    def test_legacy_database_has_no_thresholds_before_first_observation(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE trend (slot INTEGER PRIMARY KEY, t INTEGER,"
                       "rate REAL, watts REAL, edge REAL, hot REAL, hbm REAL, active INTEGER, efficiency REAL)")
            db.execute("CREATE TABLE event (id INTEGER PRIMARY KEY, t INTEGER,"
                       "kind TEXT, worker TEXT, detail TEXT, status TEXT)")
            db.execute("INSERT INTO trend VALUES (100,1500,12.5,90,52,64,NULL,1,NULL)")
            db.execute("INSERT INTO event VALUES (1,1500,'thermal','gpu','sensor unavailable','stopped')")
        archive = Archive(self.path)
        archive.tick(1530, [], [], limits={"edge_limit": 80, "hot_limit": 100, "hbm_limit": None})

        old = archive.history("30m", now=1520)
        self.assertEqual(old["series"][0]["rate"], 12.5)
        self.assertEqual(old["incidents"][0]["id"], 1)
        self.assertEqual(old["thresholds"], [])
        self.assertEqual(archive.history("30m", now=1530)["thresholds"],
                         [{"t": 1530, "edge_limit": 80, "hot_limit": 100, "hbm_limit": None}])

    def test_31_day_window_includes_boundary_beyond_legacy_30_day_window(self):
        archive = Archive(self.path)
        first = 500000
        now = first + 31 * 86400
        archive.tick(first, [], [])
        archive.tick(now, [], [])

        self.assertEqual(archive.history("30d", now=now)["count"], 1)
        history = archive.history("31d", now=now)
        self.assertEqual(history["from"], first)
        self.assertEqual(history["count"], 2)
        self.assertEqual(history["series"][0]["t"], first)

    def test_threshold_changes_survive_aggregation_and_clear_hbm_limit(self):
        archive = Archive(self.path)
        initial = {"edge_limit": 80, "hot_limit": 100, "hbm_limit": None}
        with_hbm = {"edge_limit": 75, "hot_limit": 95, "hbm_limit": 85}
        archive.tick(1800, [], [], limits=initial)
        archive.tick(1801, [], [], limits=initial)
        archive.tick(1802, [], [], limits=with_hbm)
        archive.tick(1803, [], [], limits={**with_hbm, "hbm_limit": None})

        history = archive.history("30m", now=1803)
        self.assertEqual(history["count"], 1)
        self.assertEqual(history["thresholds"], [
            {"t": 1800, "edge_limit": 80, "hot_limit": 100, "hbm_limit": None},
            {"t": 1802, "edge_limit": 75, "hot_limit": 95, "hbm_limit": 85},
            {"t": 1803, "edge_limit": 75, "hot_limit": 95, "hbm_limit": None},
        ])

    def test_threshold_retention_keeps_last_change_before_window(self):
        archive = Archive(self.path)
        start = 500000
        initial = {"edge_limit": 80, "hot_limit": 100, "hbm_limit": None}
        archive.tick(start - 120, [], [], limits=initial)
        archive.tick(start - 60, [], [], limits={**initial, "edge_limit": 76})
        archive.tick(start + 60, [], [], limits={**initial, "edge_limit": 75})
        now = start + 31 * 86400
        archive.tick(now, [], [])

        self.assertEqual(archive.history("31d", now=now)["thresholds"], [
            {"t": start - 60, "edge_limit": 76, "hot_limit": 100, "hbm_limit": None},
            {"t": start + 60, "edge_limit": 75, "hot_limit": 100, "hbm_limit": None},
        ])
        self.assertEqual(archive.history("30m", now=start - 120)["thresholds"], [])
        self.assertEqual(archive.history("30m", now=now)["thresholds"],
                         [{"t": start + 60, "edge_limit": 75, "hot_limit": 100, "hbm_limit": None}])

    def test_filter_is_applied_before_latest_200_event_limit(self):
        archive = Archive(self.path)
        thermal = {"time": 1700, "type": "thermal", "worker": "gpu",
                   "detail": "sensor unavailable", "status": "stopped"}
        later = {"time": 1800, "type": "limits", "worker": "", "detail": "updated", "status": "saved"}
        archive.tick(1700, [], [thermal])
        archive.tick(1800, [], [later] * 200)

        self.assertEqual({event["type"] for event in archive.events()}, {"limits"})
        self.assertEqual(archive.events(kind="thermal"), [{"id": 1, **thermal}])

    def test_expired_events_are_pruned_on_quiet_tick(self):
        archive = Archive(self.path)
        thermal = {"time": 1700, "type": "thermal", "worker": "gpu",
                   "detail": "sensor unavailable", "status": "stopped"}
        later = {"time": 1710, "type": "limits", "worker": "", "detail": "updated", "status": "saved"}
        archive.tick(1710, [], [thermal, later])
        archive.tick(1710 + 31 * 86400, [], [])

        self.assertEqual(archive.events(), [{"id": 2, **later}])

    def test_failed_tick_does_not_publish_partial_history(self):
        archive = Archive(self.path)
        thermal = {"time": 1800, "type": "thermal", "worker": "gpu",
                   "detail": "sensor unavailable", "status": "stopped"}
        broken_event = {"time": 1800, "type": "thermal", "worker": "gpu", "detail": "incomplete"}
        with self.assertRaises(KeyError):
            archive.tick(1800, [], [thermal, broken_event],
                         limits={"edge_limit": 80, "hot_limit": 100, "hbm_limit": None})

        history = archive.history("30m", now=1800)
        self.assertEqual(history["count"], 0)
        self.assertEqual(history["thresholds"], [])
        self.assertEqual(archive.events(), [])


    def test_reboot_requires_a_changed_known_identity_across_service_starts(self):
        archive = Archive(self.path)
        archive.note_start(1200)
        archive.note_start(1300, boot_id="fixture-first-boot")
        archive.note_start(1400)
        archive = Archive(self.path)
        archive.note_start(1500, boot_id="fixture-first-boot")
        archive.note_start(1800, boot_id="fixture-next-boot", reboot_at=1700)
        archive.note_start(1900, boot_id="fixture-next-boot", reboot_at=1700)

        self.assertEqual([event["time"] for event in archive.events(kind="service-start")],
                         [1900, 1800, 1500, 1400, 1300, 1200])
        reboots = archive.events(kind="host-reboot")
        self.assertEqual([event["time"] for event in reboots], [1700])
        history = archive.history("30m", now=1900)
        self.assertEqual([(marker["type"], marker["time"]) for marker in history["markers"]],
                         [("service-start", 1900), ("service-start", 1800), ("host-reboot", 1700),
                          ("service-start", 1500), ("service-start", 1400),
                          ("service-start", 1300), ("service-start", 1200)])
        self.assertNotIn("fixture-first-boot", str(history))
        self.assertNotIn("fixture-next-boot", str(history))
        self.assertNotIn("fixture-next-boot", str(archive.events()))

    def test_implausible_reboot_time_falls_back_to_service_start(self):
        archive = Archive(self.path)
        archive.note_start(1500, boot_id="first")
        archive.note_start(1600, boot_id="second", reboot_at=1450)
        archive.note_start(1700, boot_id="third", reboot_at=1800)
        archive.note_start(1750, boot_id="   ")
        archive.note_start(1800, boot_id="third")

        self.assertEqual([event["time"] for event in archive.events(kind="host-reboot")], [1700, 1600])

    def test_chart_keeps_thermal_and_failed_exit_markers_amid_routine_events(self):
        archive = Archive(self.path)
        thermal = {"time": 3900, "type": "thermal", "worker": "gpu", "detail": "overheat", "status": "stopped"}
        failed = {"time": 3901, "type": "exit", "worker": "gpu", "detail": "unexpected exit", "status": "failed"}
        routine = {"time": 3904, "type": "exit", "worker": "gpu", "detail": "normal exit", "status": "stopped"}
        archive.tick(3900, [], [{**thermal, "time": 100}, thermal, failed, routine])
        archive.tick(4000, [], [{**routine, "type": "profile", "time": 4000, "status": "failed"}] * 200)

        history = archive.history("30m", now=4000)
        self.assertEqual(history["incidents"], [{"id": 2, **thermal}])
        self.assertEqual(history["markers"], [{"id": 3, **failed}, {"id": 2, **thermal}])


if __name__ == "__main__":
    unittest.main()
