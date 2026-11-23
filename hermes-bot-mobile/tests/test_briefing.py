import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import briefing  # noqa: E402


class BriefingTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.folder = self.root / "hermes-bot-mobile"
        self.clock = [1_790_000_000.0]
        self.store = briefing.BriefingStore(folder=self.folder, clock=lambda: self.clock[0], root=self.root)

    def tearDown(self):
        self.dir.cleanup()

    def test_ack_and_default_24h_window(self):
        self.assertEqual(self.store.last_ack("iphone"), self.clock[0] - 86_400.0)
        res = self.store.ack("iphone", at=self.clock[0] - 100.0)
        self.assertEqual(res, {"ok": True, "since": self.clock[0] - 100.0})
        self.assertEqual(self.store.last_ack("iphone"), self.clock[0] - 100.0)

    def test_activity_aggregation_filtering_and_pruning(self):
        t0 = self.clock[0]
        self.store.ack("iphone", at=t0 - 500)
        # Before ack -> excluded by default since
        self.store.record_event("reply", "juniper", "Old reply", at=t0 - 1000)
        # Included events
        self.store.record_event("reply", "juniper", "Drafted invoice", session="s1", status="ok", at=t0 - 200)
        self.store.record_event("routine", "scout", "Nightly sync failed", status="failed", at=t0 - 100)
        self.store.record_event("approval", "juniper", "Needs your OK: deploy", status="ok", at=t0 - 50)

        out = self.store.activity(client="iphone")
        self.assertEqual(out["counts"], {"total": 3, "completed": 2, "failed": 1, "approvals": 1, "replies": 1, "routines": 1})
        self.assertEqual([e["profile"] for e in out["events"]], ["juniper", "scout", "juniper"])
        self.assertEqual(out["by_bot"]["juniper"]["total"], 2)
        self.assertEqual(out["by_bot"]["scout"]["failed"], 1)

        only_scout = self.store.activity(client="iphone", profiles=["scout"])
        self.assertEqual(len(only_scout["events"]), 1)

    def test_merges_readonly_dashboard_records(self):
        dash_dir = self.root / "hermes-dashboard"
        dash_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(dash_dir / "dashboard.db")
        conn.execute("CREATE TABLE collections (key TEXT PRIMARY KEY, owner TEXT)")
        conn.execute("CREATE TABLE records (id TEXT PRIMARY KEY, collection TEXT, updated_at INTEGER, closed_at INTEGER)")
        conn.execute("INSERT INTO collections VALUES ('bot:harbor/todos', 'bot:harbor')")
        conn.execute("INSERT INTO records VALUES ('rec_1', 'bot:harbor/todos', ?, NULL)", (int(self.clock[0]) - 30,))
        conn.commit()
        conn.close()

        self.store.ack("default", at=self.clock[0] - 60)
        out = self.store.activity(client="default")
        self.assertEqual(out["counts"]["total"], 1)
        self.assertEqual(out["events"][0]["profile"], "harbor")
        self.assertEqual(out["events"][0]["kind"], "dashboard")


if __name__ == "__main__":
    unittest.main()
