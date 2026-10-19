import threading
import time
import types
import unittest

import helpers  # noqa: F401  (puts the plugin folder on the path)

import needs_you


class Request:
    """The parts of Hermes's ServerRequest the watch reads."""

    count = 0

    def __init__(self, sid, method, params=None):
        Request.count += 1
        self.id = f"srq-{Request.count}"
        self.sid = sid
        self.method = method
        self.params = dict(params or {})
        self.created_at = time.time() + Request.count / 1000
        self.locked = {}

    def snapshot(self):
        params = {"session_id": self.sid, **self.params}

        if self.locked:
            params["answers"] = dict(self.locked)

        return {"id": self.id, "method": self.method, "params": params}


def fake_hermes():
    """A stand-in for `tui_gateway.server_requests`: its lock and its table, written the way Hermes writes them."""
    module = types.SimpleNamespace(_lock=threading.Lock(), _open={})

    def open_(request):
        with module._lock:
            module._open[request.id] = request

        return request

    def close(request):
        with module._lock:
            module._open.pop(request.id, None)

    module.open = open_
    module.close = close

    return module


class Watch(unittest.TestCase):
    def setUp(self):
        self.hermes = fake_hermes()
        self.sent = []
        self.arrived = threading.Condition()

        def broadcast(payload):
            with self.arrived:
                self.sent.append(payload)
                self.arrived.notify_all()

        self.watch = needs_you.NeedsYou(
            requests=lambda: self.hermes,
            broadcast=broadcast,
            profile_of=lambda sid: ({"s-leto": "default", "s-hydra": "hydra"}.get(sid, ""), f"stored-{sid}"),
            coalesce_s=0.01,
        )

    def wait_for(self, count):
        with self.arrived:
            self.assertTrue(self.arrived.wait_for(lambda: len(self.sent) >= count, timeout=2), f"expected {count} pushes, got {self.sent}")

        return self.sent[count - 1]

    def test_pushes_a_prompt_the_moment_it_opens_and_again_when_it_closes(self):
        self.assertTrue(self.watch.install())
        asked = self.hermes.open(Request("s-hydra", "approval", {"command": "rm -r <redacted>", "description": "delete files", "choices": ["once", "deny"]}))
        opened = self.wait_for(1)
        self.assertEqual(
            opened["requests"],
            [
                {
                    "id": asked.id,
                    "method": "approval",
                    "session_id": "s-hydra",
                    "session_key": "stored-s-hydra",
                    "profile": "hydra",
                    "created_at": asked.created_at,
                    "params": {"command": "rm -r <redacted>", "description": "delete files", "choices": ["once", "deny"]},
                }
            ],
        )
        self.assertTrue(opened["live"])

        self.hermes.close(asked)
        closed = self.wait_for(2)
        self.assertEqual(closed["requests"], [])
        self.assertGreater(closed["version"], opened["version"])

    def test_leaves_out_what_the_agent_asks_the_client_rather_than_you(self):
        self.watch.install()
        self.hermes.open(Request("s-leto", "terminal.read"))
        self.hermes.open(Request("s-leto", "clarify", {"questions": [{"id": "q1", "question": "Which one?"}]}))
        pushed = self.wait_for(1)
        self.assertEqual([entry["method"] for entry in pushed["requests"]], ["clarify"])

    def test_sends_one_push_for_changes_that_land_together(self):
        self.watch = needs_you.NeedsYou(requests=lambda: self.hermes, broadcast=self.sent.append, profile_of=lambda sid: ("", ""), coalesce_s=0.2)
        self.watch.install()
        for n in range(3):
            self.hermes.open(Request("s-leto", "clarify", {"questions": [{"id": f"q{n}"}]}))
        time.sleep(0.6)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.sent[0]["requests"]), 3)

    def test_a_question_in_a_batch_answered_is_a_change(self):
        self.watch.install()
        batch = self.hermes.open(Request("s-leto", "clarify", {"questions": [{"id": "q1"}, {"id": "q2"}]}))
        self.wait_for(1)
        with self.hermes._lock:
            batch.locked["q1"] = "Yes"
            self.hermes._open[batch.id] = batch
        answered = self.wait_for(2)
        self.assertEqual(answered["requests"][0]["params"]["answers"], {"q1": "Yes"})

    def test_nothing_is_pushed_when_nothing_changed(self):
        self.watch.install()
        time.sleep(0.1)
        self.assertEqual(self.sent, [])
        self.assertFalse(self.watch.publish())

    def test_prompts_already_open_are_kept_and_pushed_at_install(self):
        early = self.hermes.open(Request("s-leto", "sudo"))
        self.watch.install()
        self.assertEqual(self.wait_for(1)["requests"][0]["id"], early.id)
        self.assertIn(early.id, self.hermes._open)

    def test_a_read_for_the_route_never_hides_a_change_from_the_push(self):
        self.watch.install()
        asked = self.hermes.open(Request("s-leto", "secret", {"prompt": "API key", "env_var": "X_KEY"}))
        self.assertEqual(self.watch.snapshot()["requests"][0]["id"], asked.id)
        self.assertEqual(self.wait_for(1)["requests"][0]["id"], asked.id)

    def test_installing_twice_swaps_the_table_once(self):
        self.watch.install()
        table = self.hermes._open
        self.assertIsInstance(table, needs_you.WatchedTable)
        self.assertTrue(self.watch.install())
        self.assertIs(self.hermes._open, table)

        other = needs_you.NeedsYou(requests=lambda: self.hermes, broadcast=self.sent.append, profile_of=lambda sid: ("", ""), coalesce_s=0.01)
        other.install()
        self.assertIs(self.hermes._open, table, "a second copy of the module shares the swapped table")

    def test_a_hermes_without_the_table_leaves_the_app_on_its_own_check(self):
        def missing():
            raise ImportError("No module named 'tui_gateway.server_requests'")

        watch = needs_you.NeedsYou(requests=missing, broadcast=self.sent.append)
        self.assertFalse(watch.install())
        self.assertEqual(watch.snapshot(), {"live": False, "version": 0, "requests": []})

    def test_a_broadcast_that_fails_does_not_end_the_watch(self):
        calls = []

        def flaky(payload):
            calls.append(payload)

            if len(calls) == 1:
                raise RuntimeError("peer gone")

        watch = needs_you.NeedsYou(requests=lambda: self.hermes, broadcast=flaky, profile_of=lambda sid: ("", ""), coalesce_s=0.01)
        watch.install()
        first = self.hermes.open(Request("s-leto", "approval"))
        time.sleep(0.1)
        self.hermes.close(first)
        time.sleep(0.1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["requests"], [])


class Table(unittest.TestCase):
    def test_flags_every_kind_of_write_and_only_real_ones(self):
        flags = []
        table = needs_you.WatchedTable({"a": 1}, changed=lambda: flags.append(1))
        table["b"] = 2
        table.pop("b")
        table.pop("missing", None)
        del table["a"]
        table.setdefault("c", 3)
        table.setdefault("c", 4)
        table.update(d=5)
        table.popitem()
        table.clear()
        table.clear()
        self.assertEqual(len(flags), 7)


if __name__ == "__main__":
    unittest.main()
