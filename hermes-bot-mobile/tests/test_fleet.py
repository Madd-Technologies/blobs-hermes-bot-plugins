import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import fleet  # noqa: E402


class FleetTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        (self.root / "cron").mkdir(parents=True)
        (self.root / "cron" / "jobs.json").write_text(json.dumps([{"id": "j1"}, {"id": "j2"}]), encoding="utf-8")

        juniper = self.root / "profiles" / "juniper"
        (juniper / "cron").mkdir(parents=True)
        (juniper / "profile.yaml").write_text("ui_meta:\n  hermes-bots:\n    title: Juniper Bot\n", encoding="utf-8")
        (juniper / "cron" / "jobs.json").write_text(json.dumps({"jobs": [{"id": "a"}]}), encoding="utf-8")

        self.clock = [1_790_000_000]
        self.fleet = fleet.Fleet(folder=self.root / "hermes-bot-mobile", root=self.root, clock=lambda: self.clock[0])

    def tearDown(self):
        self.dir.cleanup()

    def test_discovers_profiles_and_pauses_resumes(self):
        initial = self.fleet.status()
        self.assertFalse(initial["paused"])
        self.assertEqual([b["profile"] for b in initial["bots"]], ["default", "juniper"])
        self.assertEqual(initial["bots"][0]["routines"], 2)
        self.assertEqual((initial["bots"][1]["name"], initial["bots"][1]["routines"]), ("Juniper Bot", 1))

        paused = self.fleet.pause("Travel mode")
        self.assertTrue(paused["paused"])
        self.assertEqual(paused["reason"], "Travel mode")
        self.assertTrue(self.fleet.is_paused())
        self.assertTrue(all(bot["paused"] for bot in paused["bots"]))

        self.clock[0] += 120
        resumed = self.fleet.resume()
        self.assertFalse(resumed["paused"])
        self.assertEqual(resumed["reason"], "")
        self.assertEqual(resumed["resumed_at"], 1_790_000_120)
        self.assertFalse(self.fleet.is_paused())


if __name__ == "__main__":
    unittest.main()
