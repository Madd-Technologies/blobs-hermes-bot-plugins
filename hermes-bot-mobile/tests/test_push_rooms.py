"""Pushing rooms to the app: what is on screen (`watch.py`), members' turns as they stream (`live.py`) and the relay that
sends both over `/api/ws` (`relay.py`).

    python -m unittest discover -s hermes-bot-mobile/tests
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401  (puts the plugin folder on sys.path)
import live
import relay
import watch

ROOM = "r-example0001"


class Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class Watch(unittest.TestCase):
    def setUp(self):
        watch.clear()
        self.addCleanup(watch.clear)

    def test_the_union_of_every_app_on_screen_until_it_lapses(self):
        watch.put("phone", {"dashboards": ["orion"], "rooms": [ROOM]}, ttl=30, now=100)
        watch.put("tablet", {"dashboards": ["juniper"]}, ttl=60, now=100)

        self.assertEqual(watch.watched("dashboards", now=110), {"orion", "juniper"})
        self.assertEqual(watch.watched("rooms", now=110), {ROOM})
        self.assertEqual(watch.watched("dashboards", now=140), {"juniper"}, "the phone stopped repeating it")
        self.assertFalse(watch.anyone(now=200))

    def test_a_new_put_replaces_and_an_empty_one_leaves(self):
        watch.put("phone", {"rooms": [ROOM]}, now=100)
        watch.put("phone", {"dashboards": ["orion"]}, now=101)

        self.assertEqual(watch.watched("rooms", now=102), set())
        watch.put("phone", {}, now=103)
        self.assertFalse(watch.anyone(now=104))

    def test_refuses_what_is_not_an_id(self):
        with self.assertRaises(ValueError):
            watch.put("phone", {"rooms": ["../etc"]})
        with self.assertRaises(ValueError):
            watch.put("bad client!", {"rooms": [ROOM]})
        with self.assertRaises(ValueError):
            watch.put("phone", {"rooms": [f"r-{index}" for index in range(watch.MAX_ITEMS + 1)]})

    def test_one_state_for_every_copy_of_the_module(self):
        # The dashboard loads the plugin twice and other plugins read it: all see the same object in sys.modules.
        self.assertIs(watch.registry(), watch.registry())
        watch.put("phone", {"dashboards": ["orion"]})
        import sys

        self.assertEqual(sys.modules[watch.REGISTRY].watched("dashboards"), {"orion"})


class Live(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.clock = Clock()
        self.live = live.RoomLive(Path(self.folder.name), clock=self.clock, start_thread=False)

    def record(self, kind, payload=None, member="m-orion", turn="t-1"):
        return self.live.record(room_id=ROOM, member_id=member, turn_id=turn, kind=kind, payload=payload or {})

    def test_keeps_the_text_and_steps_of_the_turn_as_it_streams(self):
        self.record("tool.started", {"tool_id": "c1", "name": "web_search"})
        self.record("tool.completed", {"tool_id": "c1", "name": "web_search", "duration_s": 1.234})
        self.record("message.delta", {"text": "The quote "})
        self.record("message.delta", {"text": "looks fair."})

        member = self.live.snapshot(ROOM)["m-orion"]
        self.assertEqual(member["text"], "The quote looks fair.")
        self.assertEqual(member["phase"], "writing")
        self.assertEqual(member["steps"], [{"id": "c1", "name": "web_search", "done": True, "seconds": 1.2}])

    def test_a_new_turn_starts_fresh_and_an_interim_message_is_kept(self):
        self.record("message.delta", {"text": "Old reply"})
        self.record("message.interim", {"text": "Checking two things first.", "already_streamed": False}, turn="t-2")
        self.record("message.interim", {"text": "Already streamed", "already_streamed": True}, turn="t-2")

        self.assertEqual(self.live.snapshot(ROOM)["m-orion"]["text"], "Checking two things first.")

    def test_keeps_only_the_end_of_a_long_reply(self):
        self.record("message.delta", {"text": "x" * (live.MAX_TEXT + 50)})

        member = self.live.snapshot(ROOM)["m-orion"]
        self.assertEqual(len(member["text"]), live.MAX_TEXT)
        self.assertTrue(member["cut"])

    def test_ignores_what_it_cannot_place_and_never_raises(self):
        self.assertFalse(self.live.record(room_id="../etc", member_id="m-x", kind="message.delta"))
        self.assertFalse(self.live.record(room_id=ROOM, member_id="m-x", kind="message.start"))
        self.assertFalse(self.live.record(room_id=ROOM, member_id="m-x", kind="message.delta", payload=object()) and False)

    def test_writes_at_most_every_quarter_second_and_forgets_a_quiet_member(self):
        self.record("message.delta", {"text": "Hi"})
        self.live.flush()
        path = live.live_path(Path(self.folder.name), ROOM)
        self.assertEqual(json.loads(path.read_text())["members"]["m-orion"]["text"], "Hi")

        self.record("message.delta", {"text": " there"})
        self.live.flush()
        self.assertEqual(json.loads(path.read_text())["members"]["m-orion"]["text"], "Hi", "too soon after the last write")
        self.clock.now += live.WRITE_EVERY
        self.live.flush()
        self.assertEqual(json.loads(path.read_text())["members"]["m-orion"]["text"], "Hi there")

        self.clock.now += live.FORGET_AFTER + 1
        self.assertEqual(self.live.snapshot(ROOM), {})


class Relay(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.clock = Clock()
        self.sent = []
        self.rooms_watched = {ROOM}
        self.db = Path(self.folder.name) / "shared-state.db"
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE rooms (room_id TEXT PRIMARY KEY, latest_seq INTEGER, revision INTEGER, updated_at REAL)")
        con.execute("INSERT INTO rooms VALUES (?, 3, 1, 50.0)", (ROOM,))
        con.commit()
        con.close()

        def list_rooms(path):
            con = sqlite3.connect(path)
            try:
                return [{"room_id": r, "latest_seq": s, "revision": v, "updated_at": u} for r, s, v, u in con.execute("SELECT * FROM rooms")]
            finally:
                con.close()

        self.relay = relay.RoomRelay(
            Path(self.folder.name), broadcast=lambda event, payload: self.sent.append((event, payload)),
            rooms_db=lambda: self.db, list_rooms=list_rooms, watched=lambda kind: set(self.rooms_watched) if kind == "rooms" else set(),
            clock=self.clock)
        self.addCleanup(self.relay._close)
        self.live = live.RoomLive(Path(self.folder.name), clock=self.clock, start_thread=False)

    def test_sends_the_rooms_once_and_again_only_after_a_write(self):
        self.assertTrue(self.relay.tick_rooms())
        self.assertEqual(self.sent[-1], ("rooms.changed", {"rooms": {ROOM: {"latest_seq": 3, "revision": 1, "updated_at": 50.0}}}))
        self.assertFalse(self.relay.tick_rooms(), "nothing was written")

        con = sqlite3.connect(self.db)
        con.execute("UPDATE rooms SET latest_seq = 4")
        con.commit()
        con.close()
        self.assertTrue(self.relay.tick_rooms())
        self.assertEqual(self.sent[-1][1]["rooms"][ROOM]["latest_seq"], 4)

    def test_relays_a_watched_rooms_live_turn_when_its_file_changes(self):
        self.assertEqual(self.relay.tick_live(), 0, "no file yet, nothing to say")
        self.live.record(room_id=ROOM, member_id="m-orion", turn_id="t-1", kind="message.delta", payload={"text": "Hel"})
        self.live.flush(force=True)

        self.assertEqual(self.relay.tick_live(), 1)
        event, payload = self.sent[-1]
        self.assertEqual(event, "room.live")
        self.assertEqual(payload["members"]["m-orion"]["text"], "Hel")
        self.assertEqual(self.relay.tick_live(), 0, "unchanged file")

    def test_an_unwatched_room_is_not_read(self):
        self.live.record(room_id=ROOM, member_id="m-orion", turn_id="t-1", kind="message.delta", payload={"text": "Hel"})
        self.live.flush(force=True)
        self.rooms_watched = set()

        self.assertEqual(self.relay.tick_live(), 0)
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
