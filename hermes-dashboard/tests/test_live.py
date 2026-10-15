import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import sources as L  # noqa: E402
import store as S  # noqa: E402
import tools  # noqa: E402

FIELDS = [
    {"key": "customer", "label": "Customer", "type": "text", "required": True},
    {"key": "status", "type": "status", "stages": [{"name": "New"}, {"name": "Replied"}, {"name": "Done", "done": True}]},
    {"key": "draft", "type": "longtext"},
]
CONCIERGE = S.Actor.bot("concierge")

ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
SUMMARY:Movie night
DTSTART:{soon}
LOCATION:Main St Cinema
URL:https://tickets.example.com/order/42
END:VEVENT
BEGIN:VEVENT
SUMMARY:Long ago
DTSTART:20200101T190000
END:VEVENT
END:VCALENDAR
"""


class Root(unittest.TestCase):
    """A Hermes root with a profile `concierge`, its scripts/ folder and the dashboard database."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.home = self.root / "profiles" / "concierge"
        (self.home / "scripts").mkdir(parents=True)
        self.now = [1_791_900_000.0]
        self.store = S.Store(self.root / "hermes-dashboard", clock=lambda: self.now[0])
        L._cache.clear()

    def tearDown(self):
        self.dir.cleanup()


class Installed(Root):
    """Whether a bot has the plugin in its own profile (Hermes loads plugins per profile)."""

    def enable(self, home, names):
        (home / "config.yaml").write_text("plugins:\n  enabled:\n" + "".join(f"    - {name}\n" for name in names))

    def test_a_bot_with_the_plugin_folder_and_it_enabled(self):
        (self.home / "plugins" / "hermes-dashboard").mkdir(parents=True)
        self.enable(self.home, ["hermes-bot-mobile", "hermes-dashboard"])
        self.assertIs(L.plugin_installed(self.root, "concierge"), True)

    def test_a_folder_that_is_not_enabled_or_no_folder_is_not_installed(self):
        self.enable(self.home, ["hermes-dashboard"])
        self.assertIs(L.plugin_installed(self.root, "concierge"), False)
        (self.home / "plugins" / "hermes-dashboard").mkdir(parents=True)
        self.enable(self.home, ["hermes-bot-mobile"])
        self.assertIs(L.plugin_installed(self.root, "concierge"), False)

    def test_the_default_bot_lives_at_the_root(self):
        (self.root / "plugins" / "hermes-dashboard").mkdir(parents=True)
        self.enable(self.root, ["hermes-dashboard"])
        self.assertIs(L.plugin_installed(self.root, "default"), True)

    def test_a_bot_that_does_not_exist_cannot_be_told(self):
        self.assertIsNone(L.plugin_installed(self.root, "nobody"))


class LiveSources(Root):
    def test_a_sqlite_file_the_bot_keeps_is_a_live_table(self):
        db = sqlite3.connect(self.home / "ledger.db")
        db.executescript("CREATE TABLE mail (subject TEXT, received TEXT, size INTEGER); INSERT INTO mail VALUES ('Invoice', '2026-10-13', 12), ('Hello', '2026-10-12', 3);")
        db.commit()
        db.close()
        view = self.store.set_view(CONCIERGE, {"id": "mail", "title": "Mail", "kind": "table", "source": {"kind": "sqlite", "path": "ledger.db", "query": "SELECT subject, received, size FROM mail ORDER BY received DESC"}, "config": {"columns": ["subject", "received", "size"]}})
        self.assertEqual([field["type"] for field in view["config"]["source_fields"]], ["text", "date", "number"])
        self.assertEqual(view["preview"][0]["subject"], "Invoice")
        panel = self.store.dashboard("concierge")["views"][0]
        self.assertEqual([row["cells"][0]["value"] for row in panel["rows"]], ["Invoice", "Hello"])
        self.assertEqual(panel["kept"], {"how": "live", "kind": "sqlite", "ttl": 60})
        self.assertTrue(panel["rows"][0]["id"].startswith("live:"))

    def test_a_sqlite_source_only_reads(self):
        sqlite3.connect(self.home / "ledger.db").execute("CREATE TABLE t (a)").connection.close()
        with self.assertRaises(S.DashboardError):
            self.store.set_view(CONCIERGE, {"title": "Bad", "kind": "list", "source": {"kind": "sqlite", "path": "ledger.db", "query": "DELETE FROM t"}, "config": {"show": {"title": "a"}}})
        with self.assertRaises(S.DashboardError):
            self.store.set_view(CONCIERGE, {"title": "Escape", "kind": "list", "source": {"kind": "sqlite", "path": "../../other.db", "query": "SELECT 1 AS a"}, "config": {"show": {"title": "a"}}})

    def test_a_script_source_and_its_cache(self):
        script = self.home / "scripts" / "disk.py"
        script.write_text("import json\nprint(json.dumps([{'mount': '/', 'used_pct': 71}, {'mount': '/data', 'used_pct': 93}]))\n")
        self.store.set_view(CONCIERGE, {"id": "disk", "title": "Disk", "kind": "number", "source": {"kind": "script", "script": "disk.py", "ttl": 30}, "config": {"aggregate": {"op": "max", "field": "used_pct"}}})
        self.assertEqual(self.store.dashboard("concierge")["views"][0]["value"], 93)
        script.write_text("import json\nprint(json.dumps([{'mount': '/', 'used_pct': 50}]))\n")
        self.assertEqual(self.store.dashboard("concierge")["views"][0]["value"], 93, "read from the cache within the ttl")
        self.assertEqual(self.store.dashboard("concierge", fresh=True)["views"][0]["value"], 50, "pull to refresh reads it now")
        script.write_text("raise SystemExit('broken')\n")
        self.now[0] += 31
        panel = self.store.dashboard("concierge")["views"][0]
        self.assertEqual(panel["value"], 50, "a failing source keeps its last good rows")
        self.assertIn("broken", panel["live"]["error"])

    def test_a_calendar_lists_whats_coming(self):
        soon = __import__("datetime").datetime.fromtimestamp(self.now[0] + 2 * 86_400).strftime("%Y%m%dT%H%M%S")
        (self.home / "events.ics").write_text(ICS.format(soon=soon))
        self.store.set_view(CONCIERGE, {"id": "week", "title": "This week", "kind": "list", "source": {"kind": "ics", "path": "events.ics", "days": 7}, "config": {"show": {"title": "title", "subtitle": "location", "meta": "start", "link": "url"}}})
        rows = self.store.dashboard("concierge")["views"][0]["rows"]
        self.assertEqual([row["title"]["value"] for row in rows], ["Movie night"])
        self.assertEqual(rows[0]["link"]["value"], "https://tickets.example.com/order/42")
        self.assertEqual(rows[0]["meta"]["type"], "datetime")

    def test_live_panels_cant_need_you_and_skip_home_reads(self):
        (self.home / "scripts" / "n.py").write_text("print('[{\"a\": 1}]')\n")
        with self.assertRaises(S.DashboardError):
            self.store.set_view(CONCIERGE, {"title": "N", "kind": "list", "source": {"kind": "script", "script": "n.py"}, "config": {"show": {"title": "a"}, "needs_you": {"field": "a", "op": "eq", "value": 1}}})
        self.store.set_view(CONCIERGE, {"title": "N", "kind": "list", "source": {"kind": "script", "script": "n.py"}, "config": {"show": {"title": "a"}}})
        L._cache.clear()
        with mock.patch.object(L, "read", side_effect=AssertionError("the summary must not read live sources")):
            self.assertEqual(self.store.summary()["concierge"]["needs_you"], 0)


class Writer(Root):
    def setUp(self):
        super().setUp()
        self.store.create_collection(CONCIERGE, {"id": "support-emails", "title": "Support emails", "fields": FIELDS})

    def write(self, *argv, stdin="", home=None):
        env = {**os.environ, "HERMES_HOME": str(home or self.home)}
        return subprocess.run([sys.executable, str(ROOT / "write.py"), *argv], input=stdin, capture_output=True, text=True, env=env, cwd=self.home / "scripts", check=False)

    def test_a_script_keeps_a_collection_with_no_model(self):
        done = self.write("upsert", "support-emails", stdin=json.dumps([{"source_key": "m1", "values": {"customer": "Sam"}}, {"source_key": "m2", "values": {"customer": "Lee"}}]))
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "2 added, 0 updated, 0 unchanged"), done.stderr)
        again = self.write("upsert", "support-emails", stdin=json.dumps([{"source_key": "m1", "values": {"customer": "Sam"}}]))
        self.assertEqual(again.stdout.strip(), "0 added, 0 updated, 1 unchanged")
        record = self.store.query(CONCIERGE, "support-emails", {"filter": {"field": "customer", "op": "eq", "value": "Sam"}})["records"][0]
        history = self.store.get_record(S.Actor.person(), record["id"])["history"]
        self.assertEqual((history[-1]["by_kind"], history[-1]["by"]), ("script", "concierge"))

    def test_the_bot_is_where_it_runs_not_an_argument(self):
        other = self.root / "profiles" / "assistant"
        (other / "scripts").mkdir(parents=True)
        done = self.write("upsert", "support-emails", stdin=json.dumps([{"source_key": "x", "values": {"customer": "Mallory"}}]), home=other)
        self.assertNotEqual(done.returncode, 0, "assistant can't write concierge's collection")
        self.assertIn("no collection", done.stderr)
        self.assertNotIn("profile", tools.DashboardTools.__init__.__code__.co_varnames, "the tools take no bot either")

    def test_pending_is_stable_and_retries_a_few_times_a_day(self):
        self.store.upsert(S.Actor.script("concierge"), "support-emails", [{"source_key": "m1", "values": {"customer": "Sam"}}, {"source_key": "m2", "values": {"customer": "Lee", "status": "Replied"}}])
        lines = self.store.pending(CONCIERGE, "support-emails", {"field": "status", "op": "eq", "value": "New"})
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].endswith(" Sam"))
        self.assertEqual(self.store.pending(CONCIERGE, "support-emails", {"field": "status", "op": "eq", "value": "New"}), lines, "unchanged output: the monitor skips the model")
        self.now[0] += 7 * 3600
        retried = self.store.pending(CONCIERGE, "support-emails", {"field": "status", "op": "eq", "value": "New"}, retry_after=6 * 3600)
        self.assertIn(" retry:1 ", retried[0])

    def test_a_script_write_doesnt_end_a_pressed_buttons_job(self):
        self.store.update_collection(CONCIERGE, "support-emails", {"actions": [{"id": "send", "label": "Send reply", "prompt": "Send it."}]})
        record = self.store.upsert(CONCIERGE, "support-emails", [{"source_key": "m1", "values": {"customer": "Sam"}}])[0]["record"]
        self.store.start_action(S.Actor.person(), record["id"], "send")
        self.store.update(S.Actor.script("concierge"), record["id"], {"draft": "Hi"})
        self.assertIsNotNone(self.store.get_record(S.Actor.person(), record["id"])["show"]["running"])


class Visibility(Root):
    def setUp(self):
        super().setUp()
        self.store.create_collection(CONCIERGE, {"id": "support-emails", "title": "Support emails", "fields": FIELDS, "feed": {"how": "script", "job": "abc123", "every": "5m"}})
        self.store.set_view(CONCIERGE, {"id": "inbox", "title": "Inbox", "kind": "list", "collection": "support-emails", "config": {"show": {"title": "customer"}}})

    def test_each_panel_says_how_it_is_kept(self):
        self.assertEqual(self.store.dashboard("concierge")["views"][0]["kept"], {"how": "computed", "feed": {"how": "script", "job": "abc123", "every": "5m"}})
        with self.assertRaises(S.DashboardError):
            self.store.update_collection(CONCIERGE, "support-emails", {"feed": {"how": "whenever"}})

    def test_writes_are_counted_by_how_they_were_made(self):
        self.store.upsert(CONCIERGE, "support-emails", [{"source_key": "m1", "values": {"customer": "Sam"}}])
        self.store.upsert(S.Actor.script("concierge"), "support-emails", [{"source_key": "m2", "values": {"customer": "Lee"}}])
        self.assertEqual(self.store.describe(CONCIERGE, "support-emails")["writes_7d"], {"model": 1, "script": 1, "person": 0})

    def test_the_version_changes_only_when_something_did(self):
        first = self.store.version("concierge")
        self.assertEqual(self.store.version("concierge"), first)
        self.store.upsert(CONCIERGE, "support-emails", [{"source_key": "m1", "values": {"customer": "Sam"}}])
        self.assertNotEqual(self.store.version("concierge"), first)
        self.assertEqual(self.store.dashboard("concierge")["version"], self.store.version("concierge"))

    def test_a_summary_rewritten_too_often_gets_a_hint(self):
        with mock.patch.object(tools, "current_profile", lambda: "concierge"):
            handler = tools.DashboardTools(store_factory=lambda: self.store)
            for count in range(S.REWRITES_PER_HOUR + 1):
                answer = json.loads(handler.view({"action": "set", "id": "status", "title": "Status", "kind": "summary", "config": {"text": f"All quiet {count}"}}))
            self.assertIn("rewrote this panel", answer["message"])
            nothing = json.loads(handler.record({"action": "upsert", "collection": "support-emails", "records": [{"source_key": "m9", "values": {"customer": "Z"}}]}))
            again = json.loads(handler.record({"action": "upsert", "collection": "support-emails", "records": [{"source_key": "m9", "values": {"customer": "Z"}}]}))
            self.assertNotIn("Nothing changed", nothing["message"])
            self.assertIn("Nothing changed", again["message"])


if __name__ == "__main__":
    unittest.main()
