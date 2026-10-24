"""Pushing dashboard changes to the app (`changes.py`), and live sources read only when their data can have changed.

    python -m unittest discover -s hermes-dashboard/tests
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import changes  # noqa: E402
import sources as L  # noqa: E402
import store as S  # noqa: E402

FIELDS = [{"key": "customer", "label": "Customer", "type": "text", "required": True}]
CONCIERGE = S.Actor.bot("concierge")
QUERY = "SELECT subject FROM mail ORDER BY subject"


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        self.home = self.root / "profiles" / "concierge"
        (self.home / "scripts").mkdir(parents=True)
        self.now = [1_791_900_000.0]
        self.store = S.Store(self.root / "hermes-dashboard", clock=lambda: self.now[0])
        L._cache.clear()

    def ledger(self, *subjects):
        db = sqlite3.connect(self.home / "ledger.db")
        db.executescript("DROP TABLE IF EXISTS mail; CREATE TABLE mail (subject TEXT);")
        db.executemany("INSERT INTO mail VALUES (?)", [(subject,) for subject in subjects])
        db.commit()
        db.close()

    def live_view(self):
        self.store.set_view(CONCIERGE, {"id": "mail", "title": "Mail", "kind": "list", "source": {"kind": "sqlite", "path": "ledger.db", "query": QUERY, "ttl": 60}, "config": {"show": {"title": "subject"}}})


class LiveReads(Base):
    def test_a_file_nobody_wrote_is_not_read_again_after_its_ttl(self):
        self.ledger("Invoice")
        self.live_view()
        self.store.dashboard("concierge")

        with mock.patch.object(L, "read", wraps=L.read) as read:
            self.now[0] += 120
            self.store.version("concierge")
            self.assertEqual(read.call_count, 0, "the ttl ran out but the file is the same")

            self.ledger("Invoice", "Hello")
            self.now[0] += 120
            self.store.version("concierge")
            self.assertEqual(read.call_count, 1)

    def test_the_version_moves_with_the_rows_not_with_each_read(self):
        self.ledger("Invoice")
        self.live_view()
        first = self.store.version("concierge")

        # Read again (the file was touched) with the same rows: same version.
        self.ledger("Invoice")
        self.now[0] += 120
        self.assertEqual(self.store.version("concierge"), first)

        self.ledger("Invoice", "Hello")
        self.now[0] += 120
        self.assertNotEqual(self.store.version("concierge"), first)
        self.assertEqual(self.store.dashboard("concierge")["version"], self.store.version("concierge"))

    def test_a_dashboard_nobody_looks_at_is_not_read(self):
        self.ledger("Invoice")
        self.live_view()
        self.store.dashboard("concierge")
        self.ledger("Invoice", "Hello")
        self.now[0] += 120

        with mock.patch.object(L, "read", wraps=L.read) as read:
            self.store.version("concierge", read_live=False)
            self.assertEqual(read.call_count, 0)


class Changes(Base):
    def setUp(self):
        super().setUp()
        self.sent = []
        self.watched = set()
        self.changes = changes.DashboardChanges(lambda: self.store, broadcast=self.sent.append, watched=lambda: set(self.watched), clock=lambda: self.now[0])
        self.addCleanup(self.changes._close)
        self.store.create_collection(CONCIERGE, {"id": "support-emails", "title": "Support emails", "fields": FIELDS})
        self.store.set_view(CONCIERGE, {"id": "inbox", "title": "Inbox", "kind": "list", "collection": "support-emails", "config": {"show": {"title": "customer"}, "needs_you": {"field": "customer", "op": "not_empty"}}})

    def test_sends_each_dashboards_version_and_the_summary_after_a_write(self):
        first = self.changes.tick()
        self.assertEqual(set(first["versions"]), {"concierge"})
        self.assertEqual(first["summary"]["concierge"]["needs_you"], 0)
        self.assertIsNone(self.changes.tick(), "nothing was written")

        self.store.upsert(CONCIERGE, "support-emails", [{"source_key": "m1", "values": {"customer": "Sam"}}])
        self.now[0] += changes.SUMMARY_EVERY
        event = self.changes.tick()
        self.assertEqual(event["versions"]["concierge"], self.store.version("concierge"))
        self.assertEqual(event["summary"]["concierge"]["needs_you"], 1)
        self.assertEqual(len(self.sent), 2)

    def test_a_watched_dashboards_live_panel_is_read_without_a_write(self):
        self.ledger("Invoice")
        self.live_view()
        self.changes.tick()
        self.ledger("Invoice", "Hello")
        self.now[0] += 120

        self.assertIsNone(self.changes.tick(), "nobody is looking: the file is not read")
        self.watched.add("concierge")
        event = self.changes.tick()
        self.assertIn("concierge", event["versions"])

    def test_the_kanban_mirror_runs_on_its_own(self):
        mirrored = []
        watcher = changes.DashboardChanges(lambda: self.store, broadcast=self.sent.append, watched=set, mirror=mirrored.append, clock=lambda: self.now[0])
        self.addCleanup(watcher._close)
        watcher.tick()
        self.assertEqual(mirrored, [self.store])


if __name__ == "__main__":
    unittest.main()
