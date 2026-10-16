import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import kanban as K  # noqa: E402
import store as S  # noqa: E402

YOU = S.Actor.person()


class FakeBoard:
    """Stands in for Hermes's kanban board: cards by board, with what `HermesBoard` returns for each."""

    def __init__(self):
        self.cards: dict = {}
        self.comments: list = []
        self.reads = 0

    def add(self, task, assignee="concierge", status="running", board="default", **fields):
        card = {"board": board, "id": task, "title": f"Task {task}", "assignee": assignee, "status": status, "reason": None, "latest": None, "updated": 1_791_900_000, **fields}
        self.cards[(board, task)] = card

        return card

    def boards(self):
        return sorted({board for board, _task in self.cards} | {"default"})

    def open_cards(self, board):
        self.reads += 1

        return [dict(card) for (slug, _task), card in self.cards.items() if slug == board and card["status"] in K.OPEN]

    def card(self, board, task):
        card = self.cards.get((board, task))

        return dict(card) if card else None

    def answer(self, board, task, text):
        self.comments.append((board, task, text))
        card = self.cards[(board, task)]

        if card["status"] == "blocked":
            card.update(status="ready", reason=None)

        return card["status"]


class Mirror(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.now = [1_791_900_000.0]
        self.store = S.Store(Path(self.dir.name), clock=lambda: self.now[0])
        self.board = FakeBoard()
        self.served = {"concierge", "assistant", "default"}

    def mirror(self):
        return K.mirror(self.store, self.board, lambda profile: profile in self.served)

    def records(self, profile="concierge"):
        """The open records of the bot's Background work."""
        return [record for record in self.store.query(S.Actor.bot(profile), K.COLLECTION, {"limit": 200})["records"] if not record["closed_at"]]

    def has(self, profile):
        try:
            self.store.describe(S.Actor.bot(profile), K.COLLECTION)

            return True
        except S.NotFound:
            return False

    def only(self, profile="concierge"):
        (record,) = self.store.query(S.Actor.bot(profile), K.COLLECTION, {"limit": 200})["records"]

        return record

    def test_a_blocked_card_needs_you_with_why_as_its_body(self):
        self.board.add("t_1", status="blocked", reason="Which week should the report cover?")
        self.assertEqual(self.mirror(), 1)
        record = self.only()
        self.assertEqual(record["values"]["status"], "Blocked")
        self.assertEqual(record["values"]["why"], "Which week should the report cover?")
        self.assertEqual(record["values"]["card"], "t_1")
        (flagged,) = self.store.needs_you_records("concierge")
        self.assertEqual(flagged["view_title"], K.TITLE)
        self.assertEqual(self.store.summary()["concierge"]["needs_you"], 1)
        shown = self.store.get_record(YOU, record["id"])["show"]
        self.assertTrue(shown["needs_you"])
        self.assertEqual(shown["subtitle"], "why")

    def test_the_panel_says_how_it_is_kept(self):
        self.board.add("t_1")
        self.mirror()
        (view,) = self.store.dashboard("concierge")["views"]
        self.assertEqual(view["kept"], {"how": "computed", "feed": K.DEFINITION["feed"]})

    def test_running_and_queued_cards_do_not_need_you(self):
        self.board.add("t_1", status="running")
        self.board.add("t_2", status="ready")
        self.mirror()
        self.assertEqual(sorted(record["values"]["status"] for record in self.records()), ["Queued", "Running"])
        self.assertEqual(self.store.needs_you_records("concierge"), [])

    def test_a_card_that_did_not_change_is_not_written_again(self):
        self.board.add("t_1", status="blocked", reason="Why?")
        self.mirror()
        version = self.store.version("concierge")
        self.assertEqual(self.mirror(), 0)
        self.assertEqual(self.store.version("concierge"), version)

    def test_a_finished_card_closes_its_record_with_its_summary(self):
        self.board.add("t_1", status="running")
        self.mirror()
        self.board.cards[("default", "t_1")].update(status="done", latest="Built the report.")
        self.mirror()
        record = self.only()
        self.assertEqual(record["values"]["status"], "Done")
        self.assertIsNotNone(record["closed_at"])
        self.assertEqual(record["values"]["latest"], "Built the report.")
        self.assertEqual(self.records(), [])

    def test_a_card_gone_from_the_board_closes_its_record_and_keeps_its_text(self):
        self.board.add("t_1", status="running", latest="user: halfway")
        self.mirror()
        del self.board.cards[("default", "t_1")]
        self.mirror()
        record = self.only()
        self.assertEqual(record["values"]["status"], "Archived")
        self.assertEqual(record["values"]["latest"], "user: halfway")
        self.assertIsNotNone(record["closed_at"])

    def test_a_record_the_person_closed_stays_closed_until_the_card_moves(self):
        self.board.add("t_1", status="blocked", reason="Why?")
        self.mirror()
        record = self.only()
        self.store.set_closed(YOU, record["id"], True)
        self.board.cards[("default", "t_1")]["reason"] = "Why, really?"
        self.mirror()
        self.assertIsNotNone(self.only()["closed_at"])
        self.assertEqual(self.only()["values"]["why"], "Why, really?")
        self.assertEqual(self.store.needs_you_records("concierge"), [])
        self.board.cards[("default", "t_1")]["status"] = "running"
        self.mirror()
        self.assertEqual(self.only()["values"]["status"], "Running")
        self.assertIsNone(self.only()["closed_at"])

    def test_a_note_on_a_blocked_card_is_the_answer(self):
        self.board.add("t_1", status="blocked", reason="Which date?")
        self.mirror()
        record = self.only()
        self.store.add_note(YOU, record["id"], "Last week.")
        K._last[0] = 0.0
        result = K.answer(self.store, record["id"], "Last week.", board=self.board, served=lambda profile: profile in self.served)
        self.assertEqual(result, {"card": "t_1", "posted": True, "status": "ready"})
        self.assertEqual(self.board.comments, [("default", "t_1", "Last week.")])
        self.assertEqual(self.only()["values"]["status"], "Queued")
        self.assertIsNone(self.only()["values"]["why"])
        self.assertEqual(self.store.needs_you_records("concierge"), [])

    def test_a_note_on_a_finished_card_stays_a_note(self):
        self.board.add("t_1", status="running")
        self.mirror()
        record = self.only()
        self.board.cards[("default", "t_1")]["status"] = "done"
        self.mirror()
        self.assertEqual(K.answer(self.store, record["id"], "thanks", board=self.board), {"card": "t_1", "posted": False})
        self.assertEqual(self.board.comments, [])

    def test_a_note_on_another_record_is_left_alone(self):
        bot = S.Actor.bot("concierge")
        self.store.create_collection(bot, {"id": "notes", "title": "Notes", "fields": [{"key": "name", "type": "text", "required": True}]})
        record = self.store.upsert(bot, "notes", [{"values": {"name": "x"}}])[0]["record"]
        self.assertIsNone(K.answer(self.store, record["id"], "hi", board=self.board))

    def test_an_answer_the_board_refuses_keeps_the_note(self):
        self.board.add("t_1", status="blocked", reason="?")
        self.mirror()
        self.board.answer = lambda *args: (_ for _ in ()).throw(RuntimeError("locked"))

        with self.assertLogs("hermes-dashboard", "ERROR"):
            result = K.answer(self.store, self.only()["id"], "x", board=self.board)

        self.assertEqual(result, {"card": "t_1", "posted": False, "error": "locked"})

    def test_bots_without_the_plugin_get_nothing(self):
        self.board.add("t_1", assignee="someone-else", status="blocked", reason="?")
        self.mirror()
        self.assertFalse(self.has("someone-else"))
        self.assertEqual(self.store.mirrored_cards(), {})

    def test_a_retired_collection_is_not_made_again(self):
        self.board.add("t_1")
        self.mirror()
        self.store.retire_collection(S.Actor.bot("concierge"), K.COLLECTION)
        self.board.add("t_2", status="blocked", reason="?")
        self.mirror()
        self.assertEqual(self.store.needs_you_records("concierge"), [])

    def test_a_card_given_to_another_bot_moves_with_it(self):
        self.board.add("t_1", status="blocked", reason="?")
        self.mirror()
        self.board.cards[("default", "t_1")]["assignee"] = "assistant"
        self.mirror()
        self.assertEqual(self.only("concierge")["values"]["status"], "Archived")
        self.assertEqual(self.only("assistant")["values"]["status"], "Blocked")
        self.assertEqual(self.store.mirrored_cards()[("default", "t_1")]["profile"], "assistant")

    def test_other_boards_name_their_card(self):
        self.board.add("t_9", board="ops", status="blocked", reason="?")
        self.mirror()
        self.assertEqual(self.only()["values"]["card"], "ops/t_9")

    def test_the_default_bot_is_served_too(self):
        self.board.add("t_1", assignee="default", status="blocked", reason="?")
        self.mirror()
        self.assertEqual(len(self.store.needs_you_records("default")), 1)


class Sync(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = S.Store(Path(self.dir.name))
        self.board = FakeBoard()
        self.board.add("t_1", status="blocked", reason="?")
        K._last[0] = 0.0

    def test_reads_the_board_at_most_every_few_seconds(self):
        served = lambda profile: True  # noqa: E731
        self.assertEqual(K.sync(self.store, self.board, served), 1)
        self.assertEqual(K.sync(self.store, self.board, served), 0)
        self.assertEqual(self.board.reads, 1)
        self.assertEqual(K.sync(self.store, self.board, served, force=True), 0)
        self.assertEqual(self.board.reads, 2)

    def test_never_raises(self):
        self.board.boards = lambda: (_ for _ in ()).throw(RuntimeError("database is locked"))

        with self.assertLogs("hermes-dashboard", "ERROR"):
            self.assertEqual(K.sync(self.store, self.board, lambda profile: True), 0)


if __name__ == "__main__":
    unittest.main()
