import json
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import desk as D  # noqa: E402
import supervisor as S  # noqa: E402


class Gateway(unittest.TestCase):
    def test_reply_of_a_receipt(self):
        self.assertEqual(D.reply_of({"result": {"final_response": " Answered and unblocked. "}}), "Answered and unblocked.")
        self.assertEqual(D.reply_of({"result": None}), "(The bot finished without a reply.)")
        self.assertEqual(D.reply_of(None), "(The bot finished without a reply.)")

    def test_off_switch(self):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"HERMES_KANBAN_DESK": "off"}):
            self.assertFalse(D.enabled())

        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(D.enabled())


class Handle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = 5000.0
        self.card = {"id": "t_1", "status": "blocked", "block_kind": "needs_input"}
        self.comments, self.unblocked, self.turns, self.events = [], [], [], []
        self.reply = "I answered on the card and unblocked it."

        @contextmanager
        def open_board(name):
            yield "conn"

        api = SimpleNamespace(
            card=lambda conn, task_id: self.card if task_id == "t_1" else None,
            comment=lambda conn, task_id, body: self.comments.append(body),
            unblock=lambda conn, task_id: self.unblocked.append(task_id) or True,
        )

        def turn(root, target, message, input_id):
            self.turns.append((target, message, input_id))

            if isinstance(self.reply, Exception):
                raise self.reply

            return self.reply

        self.desk = D.Desk(root=self.root, clock=lambda: self.now, emit=lambda name, **kw: self.events.append((name, kw)),
                           open_board=open_board, board_api=api, turn=turn)
        self.watch = S.Supervisor(root=self.root, clock=lambda: self.now)

    def tearDown(self):
        self.tmp.cleanup()

    def log(self):
        path = self.root / "hermes-kanban" / "desk.log"

        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def wake(self, **fields):
        return self.watch.request({"action": "wake", "target": "orion", "task_id": "t_1", "board": None, "key": "t_1:k",
                                   "message": "[Kanban desk] ...", "not_before": self.now, **fields})

    def test_a_wake_runs_one_desk_turn_and_is_logged(self):
        path = self.wake()
        [(due, request)] = self.desk.due()

        self.assertEqual(self.desk.handle(due, request), "woke")
        [(target, message, input_id)] = self.turns
        self.assertEqual((target, message), ("orion", "[Kanban desk] ..."))
        self.assertRegex(input_id, r"^[0-9a-f]{32}$")
        self.assertFalse(path.exists())
        self.assertEqual(self.events, [("kanban_card_woke", {"task_id": "t_1", "board": None, "target": "orion", "ok": True, "reply": self.reply})])
        self.assertEqual(self.log()[0]["outcome"], "woke")

    def test_a_card_that_moved_on_is_not_woken(self):
        self.card = {**self.card, "status": "ready"}
        path = self.wake()

        self.assertEqual(self.desk.handle(path, json.loads(path.read_text())), "moved on (ready)")
        self.assertEqual(self.turns, [])

    def test_requests_wait_for_their_time(self):
        self.wake(not_before=self.now + 10)
        self.assertEqual(self.desk.due(), [])
        self.now += 10
        self.assertEqual(len(self.desk.due()), 1)

    def test_a_busy_bot_is_tried_again_later_without_using_a_try(self):
        self.reply = D.Busy("target_busy")
        path = self.wake()

        self.assertEqual(self.desk.handle(path, json.loads(path.read_text())), "busy")
        request = json.loads(path.read_text())
        self.assertEqual((request["tries"], request["not_before"]), (0, self.now + 60))

    def test_a_failed_turn_is_tried_once_more_with_the_same_input(self):
        self.reply = RuntimeError("gateway_unavailable")
        path = self.wake()

        self.assertEqual(self.desk.handle(path, json.loads(path.read_text())), "failed, will retry")
        first = json.loads(path.read_text())
        self.assertEqual(first["tries"], 1)

        self.now = first["not_before"]
        self.assertEqual(self.desk.handle(path, first), "failed")
        self.assertEqual(self.turns[0][2], self.turns[1][2])
        self.assertFalse(path.exists())
        self.assertFalse(self.events[0][1]["ok"])

    def test_an_old_request_expires(self):
        path = self.wake()
        self.now += D.EXPIRE_S + 1

        self.assertEqual(self.desk.handle(path, json.loads(path.read_text())), "expired")
        self.assertEqual(self.turns, [])

    def test_a_transient_block_is_unblocked_once_with_a_comment(self):
        self.card = {**self.card, "block_kind": "transient"}
        path = self.watch.request({"action": "retry", "task_id": "t_1", "board": None, "key": "retry:t_1", "not_before": self.now})

        self.assertEqual(self.desk.handle(path, json.loads(path.read_text())), "retried")
        self.assertEqual(self.unblocked, ["t_1"])
        self.assertIn("Retrying once", self.comments[0])
        self.assertEqual(self.events[0][0], "kanban_card_retried")

    def test_a_retry_for_a_card_blocked_another_way_now_is_dropped(self):
        path = self.watch.request({"action": "retry", "task_id": "t_1", "board": None, "key": "retry:t_1", "not_before": self.now})

        self.assertEqual(self.desk.handle(path, json.loads(path.read_text())), "blocked for another reason now")
        self.assertEqual(self.unblocked, [])

    def test_junk_requests_are_dropped(self):
        folder = self.root / "hermes-kanban" / "requests"
        folder.mkdir(parents=True)
        (folder / "1.json").write_text("{nope")
        (folder / "2.json").write_text(json.dumps({"action": "explode", "task_id": "t_1"}))

        self.assertEqual(self.desk.due(), [])
        self.assertEqual(list(folder.iterdir()), [])

    def test_tick_runs_each_request_once(self):
        self.wake()
        gate = threading.Event()
        original = self.desk._turn

        def slow(*args):
            gate.wait(5)

            return original(*args)

        self.desk._turn = slow
        started = self.desk.tick()
        self.assertEqual(self.desk.tick(), [])  # still running: not started twice
        gate.set()

        for thread in started:
            thread.join(5)

        self.assertEqual(len(self.turns), 1)

    def test_one_runner_per_server(self):
        other = D.Desk(root=self.root)

        self.assertTrue(self.desk.take_lock())
        self.assertFalse(other.take_lock())


if __name__ == "__main__":
    unittest.main()
