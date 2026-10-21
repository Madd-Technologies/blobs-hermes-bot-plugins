import json
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import supervisor as S  # noqa: E402


def card(**fields):
    base = {"id": "t_1", "title": "Draft the welcome email", "assignee": "juniper", "status": "blocked", "created_by": "orion",
            "session_id": "", "workspace_path": "", "block_kind": "needs_input", "block_recurrences": 1}

    return {**base, **fields}


BOTS = {"default", "orion", "juniper"}.__contains__


class Decide(unittest.TestCase):
    def test_wakes_the_creator_bot_first(self):
        decision = S.decide(card(), "Which tone?", is_bot=BOTS, woken={})

        self.assertEqual((decision["route"], decision["target"]), ("wake", "orion"))
        self.assertEqual(decision["key"], S.wake_key("t_1", "needs_input", "Which tone?"))

    def test_wakes_the_cards_own_bot_when_a_person_made_it(self):
        decision = S.decide(card(created_by="alex"), "Which tone?", is_bot=BOTS, woken={})

        self.assertEqual((decision["route"], decision["target"]), ("wake", "juniper"))

    def test_skips_when_no_bot_made_or_works_it(self):
        decision = S.decide(card(created_by="alex", assignee="dana"), "Which tone?", is_bot=BOTS, woken={})

        self.assertEqual(decision["route"], "skip")

    def test_untyped_and_capability_blocks_wake(self):
        for kind in (None, "capability"):
            self.assertEqual(S.decide(card(block_kind=kind), "No key", is_bot=BOTS, woken={})["route"], "wake")

    def test_a_card_hermes_moved_to_triage_asks_the_person(self):
        self.assertEqual(S.decide(card(status="triage", block_recurrences=2), "Again", is_bot=BOTS, woken={})["route"], "loop")

    def test_a_card_that_is_not_blocked_any_more_is_skipped(self):
        # A dependency wait fires the hook before its write is saved, so the card still reads as running.
        for status in ("running", "ready", "todo", "done"):
            self.assertEqual(S.decide(card(status=status), "Waiting", is_bot=BOTS, woken={})["route"], "skip")

    def test_the_same_block_is_handed_over_once(self):
        woken = {S.wake_key("t_1", "needs_input", "Which   tone?"): 1.0}

        self.assertEqual(S.decide(card(), "Which tone?", is_bot=BOTS, woken=woken)["route"], "skip")

    def test_after_two_tries_the_person_is_asked(self):
        woken = {"t_1:aaaa": 1.0, "t_1:bbbb": 2.0, "t_2:cccc": 3.0}

        self.assertEqual(S.decide(card(), "Something new", is_bot=BOTS, woken=woken)["route"], "loop")
        self.assertEqual(S.decide(card(), "Something new", is_bot=BOTS, woken={"t_1:aaaa": 1.0})["route"], "wake")

    def test_a_transient_block_is_retried_once(self):
        self.assertEqual(S.decide(card(block_kind="transient"), "Timeout", is_bot=BOTS, woken={})["route"], "retry")
        self.assertEqual(S.decide(card(block_kind="transient"), "Timeout", is_bot=BOTS, woken={"retry:t_1": 1.0})["route"], "loop")


class DeskMessage(unittest.TestCase):
    def test_names_the_card_the_reason_and_how_to_act(self):
        text = S.desk_message(card(), "Which tone should\nthe email use?", [{"author": "juniper", "body": "Draft one is ready."}], "", "orion")

        self.assertIn('Card t_1 "Draft the welcome email" is blocked (needs_input). It is worked by juniper.', text)
        self.assertIn("Why it blocked: Which tone should the email use?", text)
        self.assertIn("- juniper: Draft one is ready.", text)
        self.assertIn("You created this card", text)
        self.assertIn("kanban_unblock", text)
        self.assertIn("`hermes kanban unblock t_1`", text)
        self.assertIn('"Question for you:"', text)
        self.assertNotIn("--board", text)

    def test_names_a_board_other_than_the_default(self):
        text = S.desk_message(card(), "Why", [], "launch", "juniper")

        self.assertIn("on board launch", text)
        self.assertIn('board "launch"', text)
        self.assertIn("hermes kanban --board launch show t_1", text)
        self.assertIn("You work this card", text)


class OnBlocked(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "profiles" / "orion").mkdir(parents=True)
        (self.root / "profiles" / "juniper").mkdir(parents=True)
        self.card = card()
        self.events = []
        self.now = 1000.0

        @contextmanager
        def open_board(name):
            self.opened = name
            yield "conn"

        self.watch = S.Supervisor(
            root=self.root, clock=lambda: self.now, emit=lambda name, **kw: self.events.append((name, kw)),
            open_board=open_board, read_card=lambda conn, task_id: self.card if task_id == "t_1" else None,
            read_comments=lambda conn, task_id: [],
        )

    def tearDown(self):
        self.tmp.cleanup()

    def requests(self):
        folder = self.root / "hermes-kanban" / "requests"

        return [json.loads(p.read_text()) for p in sorted(folder.glob("*.json"))] if folder.is_dir() else []

    def test_a_first_block_writes_one_wake_for_the_creator(self):
        decision = self.watch.on_blocked(task_id="t_1", board="launch", reason="Which tone?")

        self.assertEqual(decision["route"], "wake")
        self.assertEqual(self.opened, "launch")
        [request] = self.requests()
        self.assertEqual((request["action"], request["target"], request["board"], request["not_before"]), ("wake", "orion", "launch", 1000.0))
        self.assertIn("Which tone?", request["message"])
        self.assertEqual([name for name, _ in self.events], ["kanban_card_blocked"])
        self.assertEqual(self.events[0][1]["target"], "orion")

        # The same block again (the hook can fire twice) is not handed over twice.
        self.assertEqual(self.watch.on_blocked(task_id="t_1", board="launch", reason="Which tone?")["route"], "skip")
        self.assertEqual(len(self.requests()), 1)

    def test_a_loop_asks_once(self):
        self.card = card(status="triage", block_recurrences=2)
        self.watch.on_blocked(task_id="t_1", reason="Again")
        self.watch.on_blocked(task_id="t_1", reason="Again")

        self.assertEqual([name for name, _ in self.events], ["kanban_card_blocked", "kanban_card_looping", "kanban_card_blocked"])
        self.assertEqual(self.requests(), [])

    def test_a_transient_block_asks_for_a_retry_later(self):
        self.card = card(block_kind="transient")
        self.watch.on_blocked(task_id="t_1", reason="Timeout")

        [request] = self.requests()
        self.assertEqual((request["action"], request["not_before"]), ("retry", 1000.0 + S.RETRY_AFTER_S))

    def test_an_unknown_card_does_nothing(self):
        self.assertIsNone(self.watch.on_blocked(task_id="t_9", reason="x"))
        self.assertIsNone(self.watch.on_blocked(reason="x"))
        self.assertEqual(self.events, [])

    def test_old_memories_are_forgotten(self):
        self.watch.on_blocked(task_id="t_1", reason="Which tone?")
        self.now += S.REMEMBER_S + 1
        self.card = card(id="t_1", block_kind="capability")
        self.watch.on_blocked(task_id="t_1", reason="No key")

        woken = json.loads((self.root / "hermes-kanban" / "woken.json").read_text())
        self.assertEqual(list(woken), [S.wake_key("t_1", "capability", "No key")])


if __name__ == "__main__":
    unittest.main()
