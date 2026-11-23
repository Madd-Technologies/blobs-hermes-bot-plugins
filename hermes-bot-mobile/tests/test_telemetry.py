import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import telemetry  # noqa: E402


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.now = 1_790_000_000

    def tearDown(self):
        self.dir.cleanup()

    def test_aggregates_sessions_messages_tokens_cost_and_cutoff(self):
        db1 = self.root / "state.db"
        conn = sqlite3.connect(db1)
        conn.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, model TEXT, input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL, updated_at INTEGER)"
        )
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, created_at INTEGER)")
        # Inside 7-day window (1 in ms, 1 in s)
        conn.execute("INSERT INTO sessions VALUES ('s1', 'claude-sonnet', 1000, 500, 0.025, ?)", (self.now - 3600,))
        conn.execute("INSERT INTO sessions VALUES ('s2', 'gpt-4o', 200, 100, 0.01, ?)", ((self.now - 86400) * 1000,))
        # Outside window -> ignored
        conn.execute("INSERT INTO sessions VALUES ('s_old', 'claude-sonnet', 9999, 9999, 9.9, ?)", (self.now - 10 * 86_400,))
        conn.execute("INSERT INTO messages VALUES (1, ?)", (self.now - 3600,))
        conn.execute("INSERT INTO messages VALUES (2, ?)", (self.now - 86400,))
        conn.execute("INSERT INTO messages VALUES (3, ?)", (self.now - 10 * 86_400,))
        conn.commit()
        conn.close()

        # Second bot without optional cost/model columns -> degrades gracefully
        prof = self.root / "profiles" / "scout"
        prof.mkdir(parents=True)
        db2 = prof / "state.db"
        c2 = sqlite3.connect(db2)
        c2.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, total_tokens INTEGER, started_at INTEGER)")
        c2.execute("INSERT INTO sessions VALUES ('s3', 400, ?)", (self.now - 120,))
        c2.commit()
        c2.close()

        res = telemetry.summary(days=7, now=self.now, root=self.root)
        self.assertEqual(res["days"], 7)
        self.assertEqual(res["totals"]["sessions"], 3)
        self.assertEqual(res["totals"]["messages"], 2)
        self.assertEqual(res["totals"]["total_tokens"], 2200)
        self.assertAlmostEqual(res["totals"]["cost_usd"], 0.035)
        self.assertEqual(res["by_bot"]["default"]["sessions"], 2)
        self.assertEqual(res["by_bot"]["scout"]["total_tokens"], 400)
        self.assertEqual(res["by_model"]["claude-sonnet"]["tokens"], 1500)
        self.assertEqual(res["by_model"]["unknown"]["tokens"], 400)
        self.assertEqual(len(res["daily"]), 2)


if __name__ == "__main__":
    unittest.main()
