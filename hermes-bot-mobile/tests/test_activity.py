"""Live room activity (the `on_room_member_activity` hook and its route) and carrying hermes-push's data over.

    python -m unittest discover -s hermes-bot-mobile/tests
"""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

import helpers  # noqa: F401  (puts the plugin folder on sys.path)
import activity
import store

ROOM = "r-example0001"


class Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


class Activity(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.clock = Clock()
        self.activity = activity.RoomActivity(Path(self.folder.name), clock=self.clock)

    def record(self, kind, payload=None, member="m-concierge", turn="t-1"):
        return self.activity.record(room_id=ROOM, thread_id="t-main", member_id=member, turn_id=turn, task_id="k-1", execution_generation=1, kind=kind, seq=1, payload=payload or {})

    def test_phases_follow_the_events_and_only_changes_are_written(self):
        self.assertTrue(self.record("tool.started", {"tool_id": "c1", "name": "terminal", "context": "rm -rf secret", "args": {"command": "secret"}}))
        self.assertEqual(self.activity.read(ROOM), {"m-concierge": {"phase": "tool", "tool": "terminal", "since": 1000.0}})
        self.clock.now = 1300.0
        self.assertTrue(self.record("tool.completed", {"name": "terminal", "result": "lots of output"}))
        self.assertEqual(self.activity.read(ROOM)["m-concierge"]["phase"], "thinking")
        self.clock.now = 1310.0
        self.assertTrue(self.record("message.delta", {"text": "Here is"}))
        self.assertFalse(self.record("message.delta", {"text": " the plan"}))  # the same phase: nothing written
        self.assertEqual(self.activity.read(ROOM)["m-concierge"], {"phase": "writing", "tool": None, "since": 1310.0})
        self.assertTrue(self.record("request.opened", {"command": "sudo reboot"}))
        self.assertEqual(self.activity.read(ROOM)["m-concierge"]["phase"], "approval")

    def test_it_keeps_no_text_arguments_or_output(self):
        self.record("tool.started", {"name": "terminal", "context": "cat ~/.ssh/id_rsa", "args": {"command": "cat ~/.ssh/id_rsa"}})
        self.record("message.delta", {"text": "the secret plan"}, member="m-orion")
        raw = (Path(self.folder.name) / "rooms" / f"{ROOM}.json").read_text()
        for text in ("id_rsa", "secret", "context", "args", "the secret plan"):
            self.assertNotIn(text, raw)

    def test_members_are_separate_an_error_clears_one_and_stale_ones_go(self):
        self.record("tool.started", {"name": "web_search"})
        self.record("reasoning.delta", member="m-orion")
        self.assertEqual(set(self.activity.read(ROOM)), {"m-concierge", "m-orion"})
        self.assertTrue(self.record("turn.error", {"message": "boom"}))
        self.assertEqual(set(self.activity.read(ROOM)), {"m-orion"})
        self.clock.now += activity.STALE_AFTER + 1
        self.assertEqual(self.activity.read(ROOM), {})

    def test_a_new_turn_restarts_the_phase_and_odd_input_is_ignored(self):
        self.record("message.delta", turn="t-1")
        self.clock.now = 2000.0
        self.assertTrue(self.record("message.delta", turn="t-2"))
        self.assertEqual(self.activity.read(ROOM)["m-concierge"]["since"], 2000.0)
        self.assertFalse(self.record("status.update"))
        self.assertFalse(self.activity.record(room_id="../etc", member_id="m-x", kind="message.delta"))
        self.assertFalse(self.activity.record(room_id=ROOM, member_id="", kind="message.delta"))
        self.assertTrue(self.record("tool.started", {"name": "evil/../name; rm"}))
        self.assertEqual(self.activity.read(ROOM)["m-concierge"]["tool"], "evil..namerm")
        self.assertEqual(self.activity.read("../etc"), {})

    def test_two_processes_share_the_file(self):
        gateway = activity.RoomActivity(Path(self.folder.name), clock=self.clock)
        dashboard = activity.RoomActivity(Path(self.folder.name), clock=self.clock)
        gateway.record(room_id=ROOM, member_id="m-orion", kind="tool.started", payload={"name": "read_file"})
        self.assertEqual(dashboard.read(ROOM)["m-orion"]["tool"], "read_file")


class Route(unittest.TestCase):
    def test_the_route_reads_a_room_and_refuses_odd_ids(self):
        root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_bot_mobile_activity_test", root / "dashboard" / "plugin_api.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        with tempfile.TemporaryDirectory() as folder:
            module._activity()  # loads the package
            package_store = sys.modules["_hermes_bot_mobile_package.store"]
            original = package_store.default_dir
            package_store.default_dir = lambda: Path(folder)

            try:
                sys.modules["_hermes_bot_mobile_package.activity"].RoomActivity().record(room_id=ROOM, member_id="m-orion", kind="message.delta", payload={"text": "hi"})
                app = FastAPI()
                app.include_router(module.router, prefix="/api/plugins/hermes-bot-mobile")
                client = TestClient(app)
                body = client.get(f"/api/plugins/hermes-bot-mobile/rooms/{ROOM}/activity").json()
                self.assertEqual(body["members"]["m-orion"]["phase"], "writing")
                self.assertIn("now", body)
                self.assertEqual(client.get("/api/plugins/hermes-bot-mobile/rooms/.hidden/activity").status_code, 400)
            finally:
                package_store.default_dir = original


class CarryOver(unittest.TestCase):
    def test_hermes_push_data_is_copied_once_and_the_old_folder_stays(self):
        with tempfile.TemporaryDirectory() as folder:
            old, new = Path(folder) / "hermes-push", Path(folder) / "hermes-bot-mobile"
            old.mkdir()
            (old / "devices.json").write_text(json.dumps([{"id": "abc", "endpoint": "https://ntfy.example/up1"}]))
            (old / "vapid.json").write_text(json.dumps({"private": "pem"}))
            (old / "settings.json").write_text(json.dumps({"default": "needs_you"}))

            self.assertEqual(sorted(store.carry_over(old, new)), ["devices.json", "settings.json", "vapid.json"])
            self.assertEqual(json.loads((new / "vapid.json").read_text()), {"private": "pem"})
            self.assertEqual(oct((new / "devices.json").stat().st_mode & 0o777), "0o600")
            self.assertEqual(oct(new.stat().st_mode & 0o777), "0o700")
            self.assertTrue((old / "devices.json").exists())

            (old / "devices.json").write_text("[]")
            self.assertEqual(store.carry_over(old, new), [])  # the new folder has its own files now
            self.assertNotEqual((new / "devices.json").read_text(), "[]")

    def test_nothing_to_carry(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(store.carry_over(Path(folder) / "missing", Path(folder) / "new"), [])
            self.assertFalse((Path(folder) / "new").exists())


if __name__ == "__main__":
    unittest.main()
