import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import quiet  # noqa: E402


class QuietTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.clock = [1_790_000_000.0]
        self.qh = quiet.QuietHours(folder=Path(self.dir.name), clock=lambda: self.clock[0])

    def tearDown(self):
        self.dir.cleanup()

    def test_defaults_and_validation(self):
        cfg = self.qh.get()
        self.assertFalse(cfg["enabled"])
        self.assertEqual((cfg["start"], cfg["end"], cfg["timezone"]), ("22:00", "07:00", "UTC"))

        for bad in (
            {"start": "25:00"},
            {"end": "9:00"},
            {"timezone": "Mars/Olympus"},
            {"days": [7]},
            {"days": [True]},
            {"muted_until": True},
            {"enabled": "yes"},
            {"allow_urgent": 1},
        ):
            with self.assertRaises(ValueError):
                self.qh.set(bad)

    def test_overnight_window_and_weekday_boundary(self):
        # Enable quiet hours only for Monday (0) nights: 22:00 Mon -> 07:00 Tue
        self.qh.set({"enabled": True, "start": "22:00", "end": "07:00", "timezone": "UTC", "days": [0]})
        # Monday 2026-06-01 23:00 UTC (weekday=0) -> quiet
        mon_night = datetime(2026, 6, 1, 23, 0, tzinfo=timezone.utc).timestamp()
        # Tuesday 2026-06-02 06:30 UTC (weekday=1, belongs to Monday's window) -> quiet
        tue_early = datetime(2026, 6, 2, 6, 30, tzinfo=timezone.utc).timestamp()
        # Tuesday 2026-06-02 07:00 UTC -> active again
        tue_morning = datetime(2026, 6, 2, 7, 0, tzinfo=timezone.utc).timestamp()
        # Tuesday 2026-06-02 23:00 UTC -> not quiet (Tuesday not in days)
        tue_night = datetime(2026, 6, 2, 23, 0, tzinfo=timezone.utc).timestamp()

        self.assertTrue(self.qh.is_quiet(mon_night))
        self.assertTrue(self.qh.is_quiet(tue_early))
        self.assertFalse(self.qh.is_quiet(tue_morning))
        self.assertFalse(self.qh.is_quiet(tue_night))

    def test_daytime_window_muted_until_and_suppression_policy(self):
        self.qh.set({"enabled": True, "start": "12:00", "end": "14:00", "timezone": "UTC", "allow_urgent": True})
        noon = datetime(2026, 6, 1, 12, 30, tzinfo=timezone.utc).timestamp()

        self.assertFalse(self.qh.should_suppress("test", noon))
        self.assertFalse(self.qh.should_suppress("approval", noon))
        self.assertFalse(self.qh.should_suppress("call_question", noon))
        self.assertTrue(self.qh.should_suppress("reply", noon))
        self.assertTrue(self.qh.should_suppress("routine", noon))
        self.assertTrue(self.qh.should_suppress("blocked", noon))

        self.qh.set({"allow_urgent": False})
        self.assertTrue(self.qh.should_suppress("approval", noon))

        # Manual snooze overrides even outside schedule
        evening = datetime(2026, 6, 1, 18, 0, tzinfo=timezone.utc).timestamp()
        self.assertFalse(self.qh.is_quiet(evening))
        self.qh.set({"muted_until": evening + 3600})
        self.assertTrue(self.qh.is_quiet(evening + 100))
        self.assertFalse(self.qh.is_quiet(evening + 3700))


if __name__ == "__main__":
    unittest.main()
