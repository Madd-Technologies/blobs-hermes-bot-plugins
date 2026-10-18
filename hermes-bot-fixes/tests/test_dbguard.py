import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import dbguard  # noqa: E402

TLS = b"\x17\x03\x03\x00\x13" + bytes(range(100, 119))  # a 24-byte TLS record: header plus 19 payload bytes


def make_db(path: Path, page: int = 4096, rows: int = 200) -> None:
    con = sqlite3.connect(path)
    con.execute(f"PRAGMA page_size={page}")
    con.execute("CREATE TABLE rooms (id TEXT PRIMARY KEY, name TEXT, body TEXT)")
    con.executemany("INSERT INTO rooms VALUES (?, ?, ?)", [(f"r-{i}", f"Room {i}", "x" * 300) for i in range(rows)])
    con.commit()
    con.close()


def damage(path: Path) -> None:
    with open(path, "r+b") as f:
        f.seek(5)
        f.write(TLS)


def rows(path: Path) -> int:
    con = sqlite3.connect(path)

    try:
        return con.execute("SELECT count(*) FROM rooms").fetchone()[0]
    finally:
        con.close()


class Guard(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.state = self.root / "hermes-bot-fixes"
        self.now = [1_792_000_000]

    def tearDown(self):
        self.dir.cleanup()

    def scan(self, **kw):
        return dbguard.scan(self.root, self.state, clock=lambda: self.now[0], **kw)

    def test_the_damage_is_repaired_and_every_row_is_back(self):
        db = self.root / "shared-state.db"
        make_db(db)
        before = db.read_bytes()
        damage(db)

        with self.assertRaises(sqlite3.DatabaseError):
            rows(db)

        (event,) = self.scan()
        self.assertEqual((event["status"], event["page"], event["wal"]), ("repaired", 4096, False))
        self.assertEqual(rows(db), 200)
        # Only the 29 header bytes were written; everything after them is exactly as before the damage.
        after = db.read_bytes()
        self.assertEqual(after[29:], before[29:])
        self.assertEqual(after[:16], b"SQLite format 3\x00")

    def test_the_damaged_original_is_kept(self):
        db = self.root / "kanban.db"
        make_db(db)
        damage(db)
        damaged_bytes = db.read_bytes()
        (event,) = self.scan()
        self.assertEqual(Path(event["backup"]).read_bytes(), damaged_bytes)
        self.assertNotIn(str(self.state), str(dbguard.candidates(self.root)))

    def test_another_page_size_is_found(self):
        db = self.root / "metrics.sqlite3"
        make_db(db, page=1024)
        damage(db)
        (event,) = self.scan()
        self.assertEqual((event["status"], event["page"]), ("repaired", 1024))
        self.assertEqual(rows(db), 200)

    def test_a_wal_database_gets_its_wal_format_back(self):
        db = self.root / "state-small.db"
        make_db(db)
        damage(db)
        Path(f"{db}-wal").write_bytes(b"")
        (event,) = self.scan()
        self.assertEqual((event["status"], event["wal"]), ("repaired", True))
        self.assertEqual(db.read_bytes()[18:20], b"\x02\x02")

    def test_databases_in_profiles_and_their_folders_are_checked(self):
        db = self.root / "profiles" / "juniper" / "cron" / "executions.db"
        db.parent.mkdir(parents=True)
        make_db(db)
        damage(db)
        (event,) = self.scan()
        self.assertEqual((event["path"], event["status"]), (str(db), "repaired"))

    def test_healthy_and_unrelated_files_are_never_touched(self):
        healthy = self.root / "state.db"
        make_db(healthy)
        other = self.root / "notes.db"
        other.write_bytes(b"SQLit" + b"e not really" * 10)
        stamp = (healthy.stat().st_mtime_ns, other.stat().st_mtime_ns)
        self.assertEqual(self.scan(), [])
        self.assertEqual((healthy.stat().st_mtime_ns, other.stat().st_mtime_ns), stamp)
        self.assertFalse((self.state / "backups").exists())

    def test_a_file_too_big_to_copy_is_reported_not_repaired(self):
        db = self.root / "state.db"
        make_db(db)
        damage(db)
        damaged_bytes = db.read_bytes()
        (event,) = self.scan(max_bytes=1024)
        self.assertEqual(event["status"], "too_large")
        self.assertEqual(db.read_bytes(), damaged_bytes)

    def test_deeper_damage_fails_the_check_and_leaves_the_file_alone(self):
        db = self.root / "projects.db"
        make_db(db)
        damage(db)

        with open(db, "r+b") as f:  # also wreck page 2: no header can make this file whole
            f.seek(4096)
            f.write(os.urandom(4096))

        damaged_bytes = db.read_bytes()
        (event,) = self.scan()
        self.assertEqual(event["status"], "failed")
        self.assertEqual(db.read_bytes(), damaged_bytes)

    def test_status_records_what_happened(self):
        db = self.root / "shared-state.db"
        make_db(db)
        damage(db)
        self.scan()
        status = json.loads((self.state / "status.json").read_text())
        self.assertEqual(status["last_scan"], self.now[0])
        self.assertEqual(status["events"][-1]["status"], "repaired")
        self.assertGreaterEqual(status["files_checked"], 1)

    def test_backups_are_capped(self):
        db = self.root / "shared-state.db"
        make_db(db)

        for i in range(5):
            damage(db)
            self.now[0] += 60
            self.scan()

        self.assertEqual(len(list((self.state / "backups").glob("shared-state.db.*.corrupt"))), dbguard.KEEP_BACKUPS)


class StartTest(unittest.TestCase):
    def test_one_guard_per_process_across_copies(self):
        """A process that loads a copy of the plugin for each bot imports this module once per copy; one guard runs."""
        import importlib.util
        import threading

        spec = importlib.util.spec_from_file_location("dbguard_second_copy", ROOT / "dbguard.py")
        second = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(second)
        root = Path(tempfile.mkdtemp())

        try:
            self.assertTrue(dbguard.start(root, interval=3600, delay=3600))
            self.assertFalse(dbguard.start(root, interval=3600, delay=3600))
            self.assertFalse(second.start(root, interval=3600, delay=3600))
        finally:
            dbguard._started = False

        self.assertEqual(sum(thread.name == dbguard.THREAD for thread in threading.enumerate()), 1)


if __name__ == "__main__":
    unittest.main()
