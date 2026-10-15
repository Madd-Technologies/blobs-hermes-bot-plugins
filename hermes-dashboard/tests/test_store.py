import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import migrate  # noqa: E402
import store as S  # noqa: E402

SUPPORT_EMAILS = {
    "id": "support-emails",
    "title": "Support emails",
    "fields": [
        {"key": "customer", "label": "Customer", "type": "text", "required": True},
        {"key": "status", "type": "status", "stages": [{"name": "New"}, {"name": "Replied"}, {"name": "Done", "done": True}]},
        {"key": "channel", "type": "choice", "options": ["Walk-in", "Web", "Phone"]},
        {"key": "reply_by", "type": "date"},
    ],
}

CONCIERGE = S.Actor.bot("concierge")
ASSISTANT = S.Actor.bot("assistant")
YOU = S.Actor.person()


class Setup(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.now = [1_791_900_000.0]
        self.store = S.Store(Path(self.dir.name), clock=lambda: self.now[0])
        self.store.create_collection(CONCIERGE, SUPPORT_EMAILS)

    def tearDown(self):
        self.dir.cleanup()

    def add(self, source, actor=CONCIERGE, **values):
        return self.store.upsert(actor, "support-emails", [{"source_key": source, "values": {"customer": "Sam", **values}}])[0]


class Collections(Setup):
    def test_a_bot_designs_a_collection_and_changes_it(self):
        listed = self.store.list_collections(CONCIERGE)
        self.assertEqual([entry["key"] for entry in listed], ["concierge/support-emails"])
        changed = self.store.update_collection(CONCIERGE, "support-emails", {"add_fields": [{"key": "units", "type": "number"}], "change_fields": [{"key": "customer", "label": "Customer name"}], "retire_fields": ["channel"]})
        by_key = {field["key"]: field for field in changed["fields"]}
        self.assertEqual(by_key["customer"]["label"], "Customer name")
        self.assertTrue(by_key["channel"]["retired"])
        self.assertIn("units", by_key)

    def test_a_field_type_never_changes(self):
        with self.assertRaises(S.DashboardError):
            self.store.update_collection(CONCIERGE, "support-emails", {"change_fields": [{"key": "customer", "type": "number"}]})

    def test_sharing_with_another_bot(self):
        with self.assertRaises(S.NotFound):
            self.store.describe(ASSISTANT, "concierge/support-emails")
        self.store.share(CONCIERGE, "support-emails", "assistant", can_write=False)
        self.assertEqual(self.store.describe(ASSISTANT, "support-emails")["key"], "concierge/support-emails")
        with self.assertRaises(S.DashboardError):
            self.add("m1", actor=ASSISTANT)
        self.store.share(CONCIERGE, "support-emails", "assistant", can_write=True)
        self.assertTrue(self.add("m1", actor=ASSISTANT)["created"])
        with self.assertRaises(S.DashboardError):
            self.store.update_collection(ASSISTANT, "concierge/support-emails", {"title": "Mine now"})
        self.assertTrue(self.store.unshare(CONCIERGE, "support-emails", "assistant"))
        with self.assertRaises(S.NotFound):
            self.store.describe(ASSISTANT, "concierge/support-emails")

    def test_only_bots_create_collections_and_records(self):
        with self.assertRaises(S.DashboardError):
            self.store.create_collection(YOU, SUPPORT_EMAILS)
        with self.assertRaises(S.DashboardError):
            self.store.upsert(YOU, "support-emails", [{"values": {"customer": "X"}}])

    def test_the_database_is_private(self):
        self.assertEqual(os.stat(self.store.path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.store.folder).st_mode & 0o777, 0o700)


class Records(Setup):
    def test_a_source_key_means_the_same_email_is_never_added_twice(self):
        first = self.add("msg-1", channel="Walk-in")
        again = self.add("msg-1", channel="Web")
        self.assertTrue(first["created"])
        self.assertFalse(again["created"])
        self.assertEqual(again["record"]["id"], first["record"]["id"])
        self.assertEqual(again["record"]["values"]["channel"], "Web")
        self.assertEqual(again["changed"], ["channel"])

    def test_a_new_record_starts_at_the_first_open_stage(self):
        self.assertEqual(self.add("msg-1")["record"]["values"]["status"], "New")

    def test_the_status_opens_and_closes_the_record(self):
        record = self.add("msg-1")["record"]
        closed = self.store.update(CONCIERGE, record["id"], {"status": "Done"})["record"]
        self.assertEqual(closed["closed_at"], 1_791_900_000)
        reopened = self.store.set_closed(CONCIERGE, record["id"], False)["record"]
        self.assertIsNone(reopened["closed_at"])
        self.assertEqual(reopened["values"]["status"], "New")
        self.assertEqual(self.store.set_closed(YOU, record["id"], True)["record"]["values"]["status"], "Done")

    def test_the_persons_edits_win(self):
        record = self.add("msg-1", channel="Walk-in")["record"]
        self.store.update(YOU, record["id"], {"channel": "Phone"})
        again = self.add("msg-1", channel="Walk-in")
        self.assertEqual(again["record"]["values"]["channel"], "Phone")
        self.assertEqual(again["kept"], ["channel"])
        forced = self.store.upsert(CONCIERGE, "support-emails", [{"source_key": "msg-1", "values": {"channel": "Web"}}], override_person=True)[0]
        self.assertEqual(forced["record"]["values"]["channel"], "Web")

    def test_history_says_who_changed_what(self):
        record = self.add("msg-1")["record"]
        self.now[0] += 60
        self.store.update(YOU, record["id"], {"status": "Replied"})
        self.store.add_note(YOU, record["id"], "Offer 1 PM if the meeting runs late")
        history = self.store.get_record(YOU, record["id"])["history"]
        self.assertEqual([(entry["by"], entry["field"]) for entry in history], [("you", "_note"), ("you", "status"), ("concierge", "_created")])
        self.assertEqual(history[1]["old"], "New")
        self.assertEqual(history[0]["new"], "Offer 1 PM if the meeting runs late")

    def test_a_record_is_headed_the_way_its_view_shows_it(self):
        record = self.add("msg-1", reply_by="2026-10-14")["record"]
        bare = self.store.get_record(YOU, record["id"])["show"]
        self.assertEqual((bare["title"], bare["needs_you"], bare["view"]), ("customer", False, None))
        self.store.set_view(CONCIERGE, {"id": "emails", "title": "Support emails", "kind": "list", "collection": "support-emails", "config": {"show": {"title": "customer", "meta": "reply_by"}, "needs_you": {"field": "status", "op": "eq", "value": "New"}}})
        show = self.store.get_record(YOU, record["id"])["show"]
        self.assertEqual((show["title"], show["meta"], show["needs_you"], show["view"]), ("customer", "reply_by", True, "emails"))
        self.store.update(YOU, record["id"], {"status": "Replied"})
        self.assertFalse(self.store.get_record(YOU, record["id"])["show"]["needs_you"])
        # A number placed first does not take the heading: it has no rows to show.
        self.store.set_view(CONCIERGE, {"id": "count", "title": "Count", "kind": "number", "collection": "support-emails", "config": {}})
        self.store.reorder_views(CONCIERGE, ["count", "emails"])
        self.assertEqual(self.store.get_record(YOU, record["id"])["show"]["meta"], "reply_by")

    def test_bad_values_are_refused_whole(self):
        with self.assertRaises(S.DashboardError):
            self.store.upsert(CONCIERGE, "support-emails", [{"values": {"customer": "Sam"}}, {"values": {"customer": "Lee", "status": "Archived"}}])
        self.assertEqual(self.store.query(CONCIERGE, "support-emails", {})["total"], 0)

    def test_a_query_for_a_bot(self):
        self.add("m1", channel="Walk-in", reply_by="2026-10-14")
        self.add("m2", channel="Web")
        result = self.store.query(CONCIERGE, "support-emails", {"filter": {"field": "channel", "op": "eq", "value": "Walk-in"}, "group_by": "status"})
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["groups"], {"New": 1})


class Dashboards(Setup):
    def setUp(self):
        super().setUp()
        self.store.set_view(CONCIERGE, {"id": "inbox", "title": "Support emails", "kind": "list", "collection": "support-emails", "config": {"show": {"title": "customer", "subtitle": "channel"}, "needs_you": {"field": "status", "op": "eq", "value": "New"}}})
        self.store.set_view(CONCIERGE, {"id": "by-stage", "title": "By stage", "kind": "board", "collection": "support-emails", "config": {"group_by": "status", "show": {"title": "customer"}, "needs_you": {"field": "status", "op": "eq", "value": "New"}}})
        self.store.set_view(CONCIERGE, {"id": "note", "title": "Note", "kind": "summary", "config": {"text": "All quiet."}})

    def test_a_dashboard_is_computed_for_the_app(self):
        self.add("m1")
        board = self.store.dashboard("concierge")
        self.assertEqual([view["kind"] for view in board["views"]], ["list", "board", "summary"])
        self.assertEqual(board["views"][0]["rows"][0]["title"]["value"], "Sam")
        self.assertIn("concierge/support-emails", board["collections"])

    def test_needs_you_counts_each_record_once(self):
        self.add("m1")
        self.add("m2")
        record = self.add("m3")["record"]
        self.store.update(YOU, record["id"], {"status": "Replied"})
        self.assertEqual(self.store.summary()["concierge"]["needs_you"], 2)
        self.assertEqual(len(self.store.needs_you_records("concierge")), 2)

    def test_a_record_a_number_flags_is_named_as_its_list_shows_it(self):
        self.store.set_view(CONCIERGE, {"id": "waiting", "title": "Waiting", "kind": "number", "collection": "support-emails", "config": {"needs_you": {"field": "channel", "op": "eq", "value": "Web"}}})
        self.store.reorder_views(CONCIERGE, ["waiting", "inbox", "by-stage", "today"])
        self.add("m1", channel="Web")
        [entry] = [entry for entry in self.store.needs_you_records("concierge") if entry["row"]["title"]["value"] == "Sam"]
        self.assertEqual(entry["view"], "inbox")

    def test_views_belong_to_the_bots_dashboard(self):
        self.store.share(CONCIERGE, "support-emails", "assistant", can_write=False)
        self.store.set_view(ASSISTANT, {"id": "theirs", "title": "Their emails", "kind": "list", "collection": "concierge/support-emails", "config": {"show": {"title": "customer"}}})
        self.assertEqual([view["id"] for view in self.store.dashboard("assistant")["views"]], ["theirs"])
        self.assertTrue(self.store.remove_view(CONCIERGE, "note"))
        self.assertEqual([view["id"] for view in self.store.reorder_views(CONCIERGE, ["by-stage", "inbox"])], ["by-stage", "inbox"])

    def test_retiring_a_collection_takes_its_views_with_it(self):
        self.store.retire_collection(CONCIERGE, "support-emails")
        self.assertEqual([view["id"] for view in self.store.dashboard("concierge")["views"]], ["note"])


class SeveralWritersAtOnce(Setup):
    def test_two_processes_writing_together_lose_nothing(self):
        import threading

        other = S.Store(Path(self.dir.name), clock=lambda: self.now[0])
        errors = []

        def write(store, prefix):
            try:
                for index in range(40):
                    store.upsert(CONCIERGE, "support-emails", [{"source_key": f"{prefix}-{index}", "values": {"customer": f"Customer {index}"}}])
            except Exception as error:  # pragma: no cover
                errors.append(error)

        threads = [threading.Thread(target=write, args=(self.store, "a")), threading.Thread(target=write, args=(other, "b"))]
        [thread.start() for thread in threads]
        [thread.join() for thread in threads]
        self.assertEqual(errors, [])
        self.assertEqual(self.store.query(CONCIERGE, "support-emails", {"limit": 200})["total"], 80)


class FromVersionOne(unittest.TestCase):
    def test_snapshot_panels_move_into_collections_once(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "concierge.json").write_text(
                json.dumps(
                    {
                        "panels": {
                            "voicemails": {"id": "voicemails", "title": "Voicemails", "kind": "list", "order": 0, "updated": 1, "items": [{"id": "vm-1", "title": "Customer callback", "needs_you": True, "at": 1_791_890_000, "action": {"label": "Call back", "url": "tel:+15555550143"}}, {"id": "vm-2", "title": "Supplier"}]},
                            "week": {"id": "week", "title": "This week", "kind": "metrics", "order": 1, "updated": 1, "metrics": [{"label": "Tickets", "value": "12", "change": "+3"}]},
                        },
                        "done": {"voicemails": {"vm-2": 1_791_800_000}},
                    }
                )
            )
            store = S.Store(Path(folder), clock=lambda: 1_791_900_000)
            self.assertEqual(migrate.migrate_folder(store), ["concierge"])
            self.assertEqual(migrate.migrate_folder(store), [])
            board = store.dashboard("concierge")
            self.assertEqual([(view["id"], view["kind"]) for view in board["views"]], [("voicemails", "list"), ("week", "stat")])
            self.assertEqual(board["views"][0]["needs_you"], 1)
            records = store.query(S.Actor.bot("concierge"), "voicemails", {})["records"]
            by_source = {record["source_key"]: record for record in records}
            self.assertEqual(by_source["vm-1"]["values"]["phone"], "+15555550143")
            self.assertEqual(by_source["vm-2"]["closed_at"], 1_791_800_000)


if __name__ == "__main__":
    unittest.main()


class Buttons(Setup):
    REPLY = {"key": "reply", "label": "Draft reply", "type": "draft", "to": "customer"}
    SEND = {"id": "send", "label": "Send reply", "prompt": "Send the draft reply to the customer in the same email thread.", "when": {"field": "status", "op": "eq", "value": "New"}, "draft": "reply"}

    def setUp(self):
        super().setUp()
        self.store.update_collection(CONCIERGE, "support-emails", {"add_fields": [self.REPLY], "actions": [self.SEND]})
        self.store.set_view(CONCIERGE, {"id": "inbox", "title": "Support emails", "kind": "list", "collection": "support-emails", "config": {"show": {"title": "customer"}, "needs_you": {"field": "status", "op": "eq", "value": "New"}}})
        self.record = self.add("m1", reply="Hi Sam, 11 AM works.")["record"]

    def test_a_record_offers_the_buttons_that_match(self):
        show = self.store.get_record(YOU, self.record["id"])["show"]
        self.assertEqual(show["actions"], [{"id": "send", "label": "Send reply"}])
        self.store.update(YOU, self.record["id"], {"status": "Replied"})
        self.assertEqual(self.store.get_record(YOU, self.record["id"])["show"]["actions"], [])

    def test_a_button_that_sends_a_draft_waits_for_the_draft(self):
        empty = self.add("m2")["record"]
        self.assertEqual(self.store.get_record(YOU, empty["id"])["show"]["actions"], [])
        with self.assertRaises(S.DashboardError):
            self.store.start_action(YOU, empty["id"], "send")

    def test_pressing_one_hands_the_bot_the_job(self):
        self.store.update(YOU, self.record["id"], {"reply": "Hi Sam, 11 AM works. See you Friday!"})
        pressed = self.store.start_action(YOU, self.record["id"], "send", "20250101_120000_ab12")
        message = pressed["message"]
        self.assertTrue(message.startswith('[Dashboard button "Send reply" · Sam]'))
        self.assertIn("Send the draft reply to the customer", message)
        self.assertIn('"""\nHi Sam, 11 AM works. See you Friday!\n"""', message)
        self.assertIn("your person edited it", message)
        self.assertIn(f'record_id "{self.record["id"]}"', message)
        detail = self.store.get_record(YOU, self.record["id"])
        self.assertEqual(detail["show"]["running"]["session"], "20250101_120000_ab12")
        self.assertEqual(detail["show"]["actions"], [])
        self.assertEqual(detail["history"][0]["field"], "_action")
        # While the bot works the record needs nothing from the person.
        self.assertEqual(self.store.summary()["concierge"]["needs_you"], 0)
        with self.assertRaises(S.DashboardError):
            self.store.start_action(YOU, self.record["id"], "send")

    def test_the_bot_writing_the_record_ends_the_job(self):
        self.store.start_action(YOU, self.record["id"], "send")
        self.store.add_note(CONCIERGE, self.record["id"], "Sent at 9:42.")
        self.assertIsNone(self.store.get_record(YOU, self.record["id"])["show"]["running"])
        self.store.start_action(YOU, self.record["id"], "send")
        self.store.update(CONCIERGE, self.record["id"], {"status": "Replied"})
        self.assertIsNone(self.store.get_record(YOU, self.record["id"])["record"]["running"])

    def test_a_job_that_never_comes_back_lapses(self):
        self.store.start_action(YOU, self.record["id"], "send")
        self.now[0] += 31 * 60
        self.assertIsNone(self.store.get_record(YOU, self.record["id"])["show"]["running"])
        self.assertEqual(self.store.summary()["concierge"]["needs_you"], 1)

    def test_only_offered_buttons_can_be_pressed(self):
        with self.assertRaises(S.NotFound):
            self.store.start_action(YOU, self.record["id"], "nope")
        with self.assertRaises(S.DashboardError):
            self.store.start_action(CONCIERGE, self.record["id"], "send")
        self.store.update(YOU, self.record["id"], {"status": "Replied"})
        with self.assertRaises(S.DashboardError):
            self.store.start_action(YOU, self.record["id"], "send")

    def test_what_a_bot_gets_told_about_buttons(self):
        cases = [
            ([{"label": "Go"}], "prompt"),
            ([{**self.SEND, "draft": "customer"}], "draft fields"),
            ([self.SEND, self.SEND], "share an id"),
            ([{**self.SEND, "id": f"b{index}"} for index in range(5)], "at most 4"),
            ([{**self.SEND, "when": {"field": "nope", "op": "eq", "value": 1}}], "button send"),
        ]

        for actions, words in cases:
            with self.subTest(words=words):
                with self.assertRaises(S.DashboardError) as caught:
                    self.store.update_collection(CONCIERGE, "support-emails", {"actions": actions})
                self.assertIn(words, str(caught.exception))


class SchemaOne(unittest.TestCase):
    def test_a_schema_1_database_gains_buttons_and_jobs(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "dashboard.db"
            conn = sqlite3.connect(path)
            conn.executescript(S.SCHEMA.replace(", actions TEXT NOT NULL DEFAULT '[]'", "").replace(", running TEXT,\n  muted_until INTEGER", ""))
            conn.execute("INSERT INTO meta (key, value) VALUES ('schema', '1')")
            conn.commit()
            conn.close()
            store = S.Store(Path(folder))
            store.create_collection(CONCIERGE, {**SUPPORT_EMAILS, "actions": [{"label": "Send reply", "prompt": "Send it."}]})
            self.assertEqual(store.describe(CONCIERGE, "support-emails")["actions"][0]["id"], "send-reply")
            record = store.upsert(CONCIERGE, "support-emails", [{"values": {"customer": "Sam"}}])[0]["record"]
            self.assertIsNone(record["muted_until"])


class Muting(Setup):
    def setUp(self):
        super().setUp()
        self.store.set_view(CONCIERGE, {"id": "inbox", "title": "Support emails", "kind": "list", "collection": "support-emails", "config": {"show": {"title": "customer"}, "needs_you": {"field": "status", "op": "eq", "value": "New"}}})
        self.first = self.add("m1")["record"]
        self.second = self.add("m2", customer="Lee")["record"]

    def rows(self):
        return self.store.dashboard("concierge")["views"][0]["rows"]

    def test_a_muted_record_waits_quietly_at_the_bottom(self):
        self.assertEqual(self.store.summary()["concierge"]["needs_you"], 2)
        self.store.mute(YOU, self.first["id"], self.now[0] + 3600)
        self.assertEqual(self.store.summary()["concierge"]["needs_you"], 1)
        rows = self.rows()
        self.assertEqual([row["id"] for row in rows], [self.second["id"], self.first["id"]])
        self.assertEqual((rows[1]["needs_you"], rows[1]["muted_until"]), (False, self.now[0] + 3600))
        self.assertEqual([entry["row"]["id"] for entry in self.store.needs_you_records("concierge")], [self.second["id"]])
        self.assertEqual(self.store.get_record(YOU, self.first["id"])["history"][0]["field"], "_muted")

    def test_it_speaks_up_again_when_the_time_comes_or_when_unmuted(self):
        self.store.mute(YOU, self.first["id"], self.now[0] + 3600)
        self.now[0] += 3601
        self.assertEqual(self.store.summary()["concierge"]["needs_you"], 2)
        self.assertNotIn("muted_until", self.rows()[0])
        self.store.mute(YOU, self.second["id"], S.MUTE_FOREVER + 10**9)
        self.assertEqual(self.store.get_record(YOU, self.second["id"])["record"]["muted_until"], S.MUTE_FOREVER)
        self.store.mute(YOU, self.second["id"], None)
        self.assertEqual(self.store.summary()["concierge"]["needs_you"], 2)
        self.assertEqual(self.store.get_record(YOU, self.second["id"])["history"][0]["new"], None)

    def test_only_the_person_mutes_and_only_into_the_future(self):
        with self.assertRaises(S.DashboardError):
            self.store.mute(CONCIERGE, self.first["id"], self.now[0] + 60)
        with self.assertRaises(S.DashboardError):
            self.store.mute(YOU, self.first["id"], self.now[0] - 60)
        with self.assertRaises(S.DashboardError):
            self.store.mute(YOU, self.first["id"], "soon")

