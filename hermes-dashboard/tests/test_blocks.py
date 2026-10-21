"""0.6: structured fields and formats, record layouts, approvals bound to a digest, pages behind a count, and the fuller
kanban mirror."""

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import approvals as A  # noqa: E402
import fields as F  # noqa: E402
import kanban as K  # noqa: E402
import layout as Y  # noqa: E402
import query as Q  # noqa: E402
import store as S  # noqa: E402

CONCIERGE = S.Actor.bot("concierge")
YOU = S.Actor.person()
NOW = 1_791_900_000.0  # a Saturday

EMAILS = {
    "id": "guest-emails",
    "title": "Guest emails",
    "fields": [
        {"key": "guest", "label": "Guest", "type": "text", "required": True},
        {"key": "email", "label": "Email", "type": "email"},
        {"key": "subject", "label": "Subject", "type": "text"},
        {"key": "status", "type": "status", "stages": [{"name": "New"}, {"name": "Sent", "done": True}]},
        {"key": "files", "label": "Attachments", "type": "files"},
        {"key": "reply", "label": "Reply", "type": "draft", "format": "html", "to": "email", "approve": True, "attachments": "files", "covers": ["subject"]},
        {"key": "arrives", "label": "Arrives", "type": "date"},
        {"key": "kept", "label": "Kept by the bot", "type": "text", "readonly": True},
    ],
    "actions": [{"id": "send", "label": "Send reply", "prompt": "Send the reply with email_send.", "draft": "reply"}],
}

HTML = "<p>Hi Dana,</p>\n<p>Checkout at <b>1 PM</b> works.</p>\n"
FILE = {"name": "House rules.pdf", "path": "/home/orion/rules.pdf", "mime": "application/pdf", "size": 182_000, "sha256": "a" * 64}


class Setup(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.now = [NOW]
        self.store = S.Store(Path(self.dir.name), clock=lambda: self.now[0])
        self.store.create_collection(CONCIERGE, EMAILS)

    def add(self, source="m1", **values):
        base = {"guest": "Dana Ruiz", "email": "dana@example.com", "subject": "Re: Checkout", "reply": HTML, "files": [FILE]}

        return self.store.upsert(CONCIERGE, "guest-emails", [{"source_key": source, "values": {**base, **values}}])[0]["record"]


class Fields(Setup):
    def test_structured_values_are_checked(self):
        self.assertEqual(F.clean_value({"key": "c", "type": "list"}, "Airbnb only | Both"), ["Airbnb only", "Both"])
        entries = F.clean_value({"key": "a", "type": "entries"}, [{"label": "Run 1 · finished", "detail": "4 min", "tone": "good", "at": NOW}])
        self.assertEqual(entries, [{"label": "Run 1 · finished", "detail": "4 min", "tone": "good", "at": int(NOW)}])

        for bad in ([{"label": "x", "tone": "pink"}], [{"detail": "no label"}]):
            with self.assertRaises(F.FieldError):
                F.clean_value({"key": "a", "type": "entries"}, bad)

        for bad in ([{"name": "x"}], [{"name": "x", "path": "relative.txt"}], [{"name": "x", "url": "http://x.example"}], [{"name": "x", "path": "/a", "sha256": "nothex"}]):
            with self.assertRaises(F.FieldError):
                F.clean_value({"key": "f", "type": "files"}, bad)

    def test_html_is_stored_exactly_and_never_clipped(self):
        record = self.add(reply="  " + HTML + "  ")
        self.assertEqual(record["values"]["reply"], "  " + HTML + "  ")

        with self.assertRaises(S.DashboardError):
            self.add("m2", reply="x" * (F.LIMITS["html"] + 1))

    def test_a_readonly_field_is_the_bots(self):
        record = self.add(kept="from the source")

        with self.assertRaises(S.DashboardError):
            self.store.update(YOU, record["id"], {"kept": "mine"})

        self.assertEqual(self.store.update(CONCIERGE, record["id"], {"kept": "newer"})["changed"], ["kept"])

    def test_a_drafts_attachments_and_covers_must_be_real_fields(self):
        for bad in ({"attachments": "subject"}, {"covers": ["nope"]}, {"covers": ["reply"]}):
            draft = {**EMAILS["fields"][5], **bad}

            with self.assertRaises(F.FieldError):
                F.clean_fields([*EMAILS["fields"][:5], draft])

    def test_lists_are_not_filtered_sorted_or_shown_in_a_row(self):
        fields = self.store.describe(CONCIERGE, "guest-emails")["fields"]
        self.assertEqual(Q.clean_filter({"field": "files", "op": "not_empty"}, fields), {"field": "files", "op": "not_empty"})

        with self.assertRaises(Q.QueryError):
            Q.clean_filter({"field": "files", "op": "eq", "value": "x"}, fields)

        with self.assertRaises(Q.QueryError):
            Q.clean_sort([{"field": "files"}], fields)

        with self.assertRaises(Q.QueryError):
            Q.clean_view_config("list", {"show": {"title": "files"}}, fields)


TASK_LAYOUT = {
    "blocks": [
        {"block": "header", "meta": "status"},
        {"block": "draft", "field": "reply"},
        {"block": "files", "field": "files", "label": "Attachments"},
        {"block": "fields", "fields": ["guest", "arrives"]},
        {"block": "notes"},
    ],
    "tray": {"primary": "send", "secondary": ["ask"]},
}


class Layouts(Setup):
    def test_a_layout_is_kept_and_served_with_the_record(self):
        self.store.update_collection(CONCIERGE, "guest-emails", {"layout": TASK_LAYOUT})
        record = self.add()
        served = self.store.get_record(YOU, record["id"])["collection"]["layout"]
        self.assertEqual([block["block"] for block in served["blocks"]], ["header", "draft", "files", "fields", "notes"])
        self.assertEqual(served["blocks"][-1], {"block": "notes", "history": "notes"})
        self.assertEqual(served["tray"], {"primary": "send", "secondary": ["ask"]})

    def test_mistakes_come_back_saying_what_to_change(self):
        fields = self.store.describe(CONCIERGE, "guest-emails")["fields"]
        actions = EMAILS["actions"]

        for bad, said in (
            ({"blocks": [{"block": "carousel"}]}, "block must be one of"),
            ({"blocks": [{"block": "notes"}, {"block": "header"}]}, "header block comes first"),
            ({"blocks": [{"block": "notes"}, {"block": "notes"}]}, "at most one notes"),
            ({"blocks": [{"block": "text", "field": "status"}]}, "needs a text or longtext"),
            ({"blocks": [{"block": "notes"}], "tray": {"composer": True, "primary": "send"}}, "not both"),
            ({"blocks": [{"block": "notes"}], "tray": {"primary": "launch"}}, "button id"),
        ):
            with self.assertRaises(Y.LayoutError) as caught:
                Y.clean_layout(bad, fields, actions)

            self.assertIn(said, str(caught.exception))

    def test_retiring_a_field_drops_only_the_blocks_that_name_it(self):
        self.store.update_collection(CONCIERGE, "guest-emails", {"layout": TASK_LAYOUT})
        updated = self.store.update_collection(CONCIERGE, "guest-emails", {"retire_fields": ["arrives", "files"], "actions": []})
        kinds = [block["block"] for block in updated["layout"]["blocks"]]
        self.assertEqual(kinds, ["header", "draft", "notes"])
        self.assertEqual(updated["layout"].get("tray"), {"secondary": ["ask"]})


class Approvals(Setup):
    def test_the_digest_covers_everything_that_goes_out(self):
        record = self.add()
        collection = self.store.describe(CONCIERGE, "guest-emails")
        digest = A.record_digest(collection, record, "reply")
        doc = {
            "v": 1, "collection": "concierge/guest-emails", "record": record["id"], "draft": "reply", "format": "html", "text": HTML,
            "covers": {"email": "dana@example.com", "subject": "Re: Checkout"},
            "attachments": [{"name": "House rules.pdf", "ref": "/home/orion/rules.pdf", "mime": "application/pdf", "size": 182000, "sha256": "a" * 64}],
        }
        self.assertEqual(digest, hashlib.sha256(json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
        self.assertEqual(digest, A.digest_of("concierge/guest-emails", record["id"], "reply", "html", HTML, {"email": "dana@example.com", "subject": "Re: Checkout"}, [FILE]))

        for change in ({"subject": "Re: Other"}, {"reply": HTML + " "}, {"files": [{**FILE, "sha256": "b" * 64}]}, {"email": "sam@example.com"}):
            changed = self.store.update(CONCIERGE, record["id"], change)["record"]
            self.assertNotEqual(A.record_digest(collection, changed, "reply"), digest, change)
            self.store.update(CONCIERGE, record["id"], {key: record["values"][key] for key in change})

    def test_pressing_send_approves_exactly_what_was_seen(self):
        record = self.add()
        seen = self.store.get_record(YOU, record["id"])["approvals"]["reply"]["digest"]

        with self.assertRaises(S.Changed):
            self.store.start_action(YOU, record["id"], "send", digest=None)

        self.store.update(CONCIERGE, record["id"], {"reply": HTML.replace("1 PM", "2 PM")})

        with self.assertRaises(S.Changed) as caught:
            self.store.start_action(YOU, record["id"], "send", digest=seen)

        current = caught.exception.current
        self.assertNotEqual(current, seen)
        self.assertIsNone(self.store.get_record(YOU, record["id"])["record"]["running"], "nothing started")

        pressed = self.store.start_action(YOU, record["id"], "send", digest=current)
        self.assertEqual(pressed["approved"], current)
        self.assertIn(f"approval digest {current}", pressed["message"])
        self.assertIn("Reply in html", pressed["message"])
        approval = self.store.get_record(YOU, record["id"])["approvals"]["reply"]["approved"]
        self.assertEqual((approval["digest"], approval["action"]), (current, "send"))

    def test_the_sender_checks_and_reports_from_its_own_process(self):
        record = self.add()
        current = self.store.get_record(YOU, record["id"])["approvals"]["reply"]["digest"]
        self.store.start_action(YOU, record["id"], "send", digest=current)
        db = self.store.path

        approval = A.latest(record["id"], "reply", db_path=db)
        ours = A.digest_of("concierge/guest-emails", record["id"], "reply", "html", HTML, {"email": "dana@example.com", "subject": "Re: Checkout"}, [FILE])
        self.assertEqual(approval["digest"], ours, "what it is about to send is what was approved")
        self.assertTrue(A.report(record["id"], "reply", ours, "sent", "Sent through Proton", db_path=db, clock=lambda: NOW + 60))

        served = self.store.get_record(YOU, record["id"])
        self.assertEqual(served["approvals"]["reply"]["approved"]["outcome"], {"outcome": "sent", "digest": ours, "at": int(NOW + 60), "detail": "Sent through Proton"})
        self.assertIsNone(served["record"]["running"], "the send ends the button's job")
        self.assertEqual(served["history"][0]["field"], "_sent")
        self.assertFalse(A.report("nope", "reply", ours, "sent", db_path=db))

    def test_a_bot_reports_a_refusal(self):
        record = self.add()
        current = self.store.get_record(YOU, record["id"])["approvals"]["reply"]["digest"]
        self.store.start_action(YOU, record["id"], "send", digest=current)
        result = self.store.report(CONCIERGE, record["id"], "reply", "f" * 64, "refused", "The subject changed after approval")
        self.assertEqual((result["outcome"], result["matched"]), ("refused", False))

        with self.assertRaises(S.DashboardError):
            self.store.report(YOU, record["id"], "reply", current, "sent")


ARRIVALS = {
    "id": "arrivals",
    "title": "Arrivals",
    "fields": [
        {"key": "guest", "label": "Guest", "type": "text", "required": True},
        {"key": "cabin", "label": "Cabin", "type": "choice", "options": ["Cabin 2", "Cabin 4"]},
        {"key": "arrives", "label": "Arrives", "type": "date"},
        {"key": "status", "type": "status", "stages": [{"name": "Booked"}, {"name": "Checked in"}, {"name": "Gone", "done": True}]},
    ],
}


class Pages(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = S.Store(Path(self.dir.name), clock=lambda: NOW)
        self.store.create_collection(CONCIERGE, ARRIVALS)
        rows = [("Dana", "2026-10-19", "Cabin 4"), ("Sam", "2026-10-19", "Cabin 2"), ("Maya", "2026-10-20", "Cabin 4"), ("Alex", "2026-10-27", "Cabin 2")]
        self.store.upsert(CONCIERGE, "arrivals", [{"source_key": name, "values": {"guest": name, "arrives": day, "cabin": cabin}} for name, day, cabin in rows])

    def set(self, view):
        return self.store.set_view(CONCIERGE, {"collection": "arrivals", **view})

    def test_a_count_opens_the_records_it_counts_grouped_by_day(self):
        self.set({"id": "week", "title": "Arrivals this week", "kind": "number", "config": {"filter": {"field": "arrives", "op": "within_days", "value": 7}, "opens": {"group_by": "arrives", "sort": [{"field": "guest"}]}}})
        board = self.store.dashboard("concierge")
        self.assertTrue(board["views"][0]["opens"])
        page = self.store.page("concierge", "week")
        self.assertEqual((page["kind"], page["count"], page["group_by"], page["title"]), ("list", 3, "arrives", "Arrivals this week"))
        self.assertEqual([(row["title"]["value"], row["group"]) for row in page["rows"]], [("Dana", "2026-10-19"), ("Sam", "2026-10-19"), ("Maya", "2026-10-20")])
        self.assertIsNone(page["next"])
        self.assertEqual(page["fields"][0]["key"], "guest")

    def test_a_period_number_opens_its_current_period(self):
        self.set({"id": "this-week", "title": "This week", "kind": "number", "config": {"period": {"field": "arrives", "unit": "week"}, "opens": True}})
        page = self.store.page("concierge", "this-week")
        # Saturday 2026-10-17's week runs Monday 12th to Sunday 18th: none of the arrivals.
        self.assertEqual((page["count"], page["period"], page["start"], page["end"]), (0, "week", "2026-10-12", "2026-10-18"))

    def test_a_chart_bar_opens_its_bucket(self):
        self.set({"id": "per-week", "title": "Per week", "kind": "chart", "config": {"field": "arrives", "bucket": "week", "count": 4, "opens": {"kind": "table", "columns": ["guest", "cabin"]}}})
        page = self.store.page("concierge", "per-week", bucket="2026-10-21")
        self.assertEqual((page["kind"], page["count"], page["start"]), ("table", 3, "2026-10-19"))
        self.assertEqual([column["key"] for column in page["columns"]], ["guest", "cabin"])

        with self.assertRaises(S.DashboardError):
            self.store.page("concierge", "per-week")

    def test_a_stat_metric_opens_its_filter(self):
        self.store.set_view(CONCIERGE, {"id": "cabins", "title": "Cabins", "kind": "stat", "collection": "arrivals", "config": {"metrics": [{"label": "Cabin 4", "value": "2", "opens": {"filter": {"field": "cabin", "op": "eq", "value": "Cabin 4"}}}, {"label": "Occupancy", "value": "92%"}]}})
        board = self.store.dashboard("concierge")
        self.assertEqual([metric.get("opens") for metric in board["views"][0]["metrics"]], [True, None])
        page = self.store.page("concierge", "cabins", metric=0)
        self.assertEqual((page["count"], page["metric"]), (2, "Cabin 4"))

        with self.assertRaises(S.DashboardError):
            self.store.page("concierge", "cabins", metric=1)

    def test_a_page_never_opens_another_page(self):
        with self.assertRaises(S.DashboardError):
            self.set({"id": "deep", "title": "Deep", "kind": "number", "config": {"opens": {"opens": True}}})

    def test_a_board_page_groups_by_stage(self):
        self.set({"id": "all", "title": "All", "kind": "number", "config": {"opens": {"kind": "board", "group_by": "status"}}})
        page = self.store.page("concierge", "all")
        self.assertEqual([(column["name"], column["count"]) for column in page["columns"]], [("Booked", 4), ("Checked in", 0), ("Gone", 0)])

    def test_rows_page_fifty_at_a_time(self):
        self.store.upsert(CONCIERGE, "arrivals", [{"source_key": f"g{index}", "values": {"guest": f"Guest {index:03d}"}} for index in range(60)])
        self.set({"id": "everyone", "title": "Everyone", "kind": "number", "config": {"opens": {"sort": [{"field": "guest"}]}}})
        first = self.store.page("concierge", "everyone")
        second = self.store.page("concierge", "everyone", offset=first["next"])
        self.assertEqual((first["count"], len(first["rows"]), first["next"], len(second["rows"]), second["next"]), (64, 50, 50, 14, None))


class Kanban(unittest.TestCase):
    def test_choices_come_from_an_options_line_or_a_list_under_it(self):
        self.assertEqual(K.split_choices("Which reviews count?\nOptions: Airbnb only | Both, mark VRBO"), ("Which reviews count?", ["Airbnb only", "Both, mark VRBO"]))
        self.assertEqual(K.split_choices("Which one?\n\nOptions:\n- Keep the old one\n- Use the new one\n\nEither is safe."), ("Which one?\n\nEither is safe.", ["Keep the old one", "Use the new one"]))
        self.assertEqual(K.split_choices("No options here."), ("No options here.", []))
        self.assertEqual(K.split_choices("Options: only one"), ("Options: only one", []))

    def test_attempts_read_in_plain_words_newest_first(self):
        runs = [
            {"outcome": "timed_out", "status": "timed_out", "started_at": NOW - 1200, "ended_at": NOW - 480, "error": "Iteration budget exhausted (50/50)"},
            {"outcome": "blocked", "status": "blocked", "started_at": NOW - 300, "ended_at": NOW - 60},
            {"outcome": None, "status": "running", "started_at": NOW - 30, "ended_at": None},
        ]
        self.assertEqual(K.attempts_of(runs, NOW), [
            {"label": "Run 3 · running", "detail": "30 s", "at": int(NOW - 30)},
            {"label": "Run 2 · stopped to ask you", "detail": "4 min", "at": int(NOW - 300), "tone": "warn"},
            {"label": "Run 1 · ran out of steps", "detail": "12 min", "at": int(NOW - 1200), "tone": "bad"},
        ])

    def test_a_card_names_who_asked_only_when_another_bot_did(self):
        card = {"board": "default", "id": "t_1", "title": "Collect reviews", "assignee": "juniper", "status": "blocked", "reason": "Which?\nOptions: A | B", "body": "Collect June's reviews.", "created_by": "orion", "runs": []}
        values = K.values_of(card, NOW)
        self.assertEqual((values["asked_by"], values["why"], values["choices"], values["task"]), ("orion", "Which?", ["A", "B"], "Collect June's reviews."))
        self.assertIsNone(K.values_of({**card, "created_by": "juniper"}, NOW)["asked_by"])
        self.assertIsNone(K.values_of({**card, "created_by": "dashboard user"}, NOW)["asked_by"])


class KanbanMirror(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_kanban import FakeBoard

        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = S.Store(Path(self.dir.name), clock=lambda: NOW)
        self.board = FakeBoard()

    def mirror(self):
        return K.mirror(self.store, self.board, lambda profile: True)

    def notes(self, record_id):
        return [(entry["by"], entry["new"]) for entry in self.store.get_record(YOU, record_id)["history"] if entry["field"] == "_note"]

    def test_the_question_rises_only_while_the_card_is_blocked(self):
        self.board.add("t_9", assignee="juniper", status="blocked", reason="Which?\nOptions: A | B")
        self.mirror()
        (record,) = self.store.query(S.Actor.bot("juniper"), K.COLLECTION, {})["records"]
        served = self.store.get_record(YOU, record["id"])
        self.assertTrue(served["show"]["ask"])
        self.assertEqual(served["record"]["values"]["choices"], ["A", "B"])
        self.board.cards[("default", "t_9")].update(status="running", reason=None)
        self.mirror()
        self.assertFalse(self.store.get_record(YOU, record["id"])["show"]["ask"])

    def test_comments_become_notes_once_and_the_persons_own_are_not_doubled(self):
        card = self.board.add("t_1", assignee="juniper", status="blocked", reason="Which?", comments=[{"id": 1, "author": "juniper", "body": "Found 214 reviews.", "at": NOW - 60}])
        self.mirror()
        (record,) = self.store.query(S.Actor.bot("juniper"), K.COLLECTION, {})["records"]
        self.assertEqual(self.notes(record["id"]), [("juniper", "Found 214 reviews.")])

        card["comments"] = card["comments"] + [{"id": 2, "author": K.AUTHOR, "body": "Airbnb only", "at": NOW}, {"id": 3, "author": "orion", "body": "Thanks", "at": NOW}]
        self.mirror()
        self.mirror()
        self.assertEqual(self.notes(record["id"]), [("orion", "Thanks"), ("juniper", "Found 214 reviews.")])

    def test_an_older_background_work_collection_is_brought_up_to_date(self):
        old = {**K.DEFINITION, "fields": K.DEFINITION["fields"][:2] + [{"key": "why", "label": "Why it's blocked", "type": "longtext"}], "layout": None}
        juniper = S.Actor.script("juniper")
        self.store.create_collection(juniper, old)
        self.assertTrue(K.ensure(self.store, "juniper"))
        collection = self.store.describe(juniper, K.COLLECTION)
        by_key = {field["key"]: field for field in collection["fields"]}
        self.assertEqual((by_key["why"]["label"], by_key["why"]["format"], by_key["attempts"]["type"]), ("What it asks", "markdown", "entries"))
        self.assertEqual(collection["layout"]["tray"], {"composer": True, "secondary": ["ask"]})

        # A bot's own layout is left alone.
        self.store.update_collection(juniper, K.COLLECTION, {"layout": {"blocks": [{"block": "notes"}]}})
        K.ensure(self.store, "juniper")
        self.assertEqual(self.store.describe(juniper, K.COLLECTION)["layout"], {"blocks": [{"block": "notes", "history": "notes"}]})


if __name__ == "__main__":
    unittest.main()
