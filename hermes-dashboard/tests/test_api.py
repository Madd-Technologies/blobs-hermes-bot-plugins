import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent

FIELDS = [
    {"key": "customer", "label": "Customer", "type": "text", "required": True},
    {"key": "status", "type": "status", "stages": [{"name": "New"}, {"name": "Replied"}, {"name": "Done", "done": True}]},
    {"key": "channel", "type": "choice", "options": ["Walk-in", "Web"]},
]


class Api(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        name = "hermes_dashboard_plugin_hermes-dashboard-test"
        spec = importlib.util.spec_from_file_location(name, ROOT / "dashboard" / "plugin_api.py")
        self.module = importlib.util.module_from_spec(spec)
        sys.modules[name] = self.module
        spec.loader.exec_module(self.module)
        S, T = self.module._package()
        self.S = S
        T._shared.clear()
        patch = mock.patch.object(S, "default_dir", lambda: Path(self.dir.name))
        patch.start()
        self.addCleanup(patch.stop)
        store = T.open_store()
        bot = S.Actor.bot("concierge")
        store.create_collection(bot, {"id": "support-emails", "title": "Support emails", "fields": FIELDS})
        self.record = store.upsert(bot, "support-emails", [{"source_key": "m1", "values": {"customer": "Sam", "channel": "Walk-in"}}])[0]["record"]
        store.set_view(bot, {"id": "inbox", "title": "Support emails", "kind": "list", "collection": "support-emails", "config": {"show": {"title": "customer", "subtitle": "channel"}, "needs_you": {"field": "status", "op": "eq", "value": "New"}}})
        app = FastAPI()
        app.include_router(self.module.router, prefix="/api/plugins/hermes-dashboard")
        self.client = TestClient(app)
        self.base = "/api/plugins/hermes-dashboard"

    def tearDown(self):
        self.dir.cleanup()

        for key in [k for k in sys.modules if k.startswith("_hermes_dashboard_package") or k.startswith("hermes_dashboard_plugin_hermes-dashboard-test")]:
            del sys.modules[key]

    def test_status_and_summary(self):
        self.assertEqual(self.client.get(f"{self.base}/status").json()["version"], "0.3.1")
        self.assertEqual(self.client.get(f"{self.base}/bots").json()["bots"]["concierge"]["needs_you"], 1)

    def test_a_dashboard_computed(self):
        body = self.client.get(f"{self.base}/bots/concierge").json()
        self.assertEqual(body["views"][0]["rows"][0]["title"]["value"], "Sam")
        self.assertEqual(body["collections"]["concierge/support-emails"]["fields"][0]["key"], "customer")
        self.assertEqual(self.client.get(f"{self.base}/bots/assistant").json()["views"], [])
        # Whether the bot has the plugin is asked of its own profile; this test root has none, so it can't be told.
        self.assertIn("installed", body)
        self.assertIsNone(body["installed"])

    def test_the_persons_actions_on_a_record(self):
        url = f"{self.base}/records/{self.record['id']}"
        self.assertEqual(self.client.patch(url, json={"values": {"status": "Replied"}}).json()["record"]["values"]["status"], "Replied")
        self.assertEqual(self.client.post(f"{url}/notes", json={"text": "Call her after 2"}).json()["note"], "Call her after 2")
        self.assertIsNotNone(self.client.post(f"{url}/close").json()["record"]["closed_at"])
        self.assertIsNone(self.client.post(f"{url}/reopen").json()["record"]["closed_at"])
        history = self.client.get(url).json()["history"]
        self.assertEqual([entry["field"] for entry in history][:3], ["status", "status", "_note"])
        self.assertEqual(self.client.get(f"{self.base}/bots/concierge/needs-you").json()["records"][0]["row"]["title"]["value"], "Sam")

    def test_pressing_a_button(self):
        store = self.module._package()[1].open_store()
        store.update_collection(self.S.Actor.bot("concierge"), "support-emails", {"actions": [{"id": "send", "label": "Send reply", "prompt": "Send it."}]})
        url = f"{self.base}/records/{self.record['id']}/actions/send"
        body = self.client.post(url, json={"session": "20250101_120000_ab12"}).json()
        self.assertTrue(body["message"].startswith('[Dashboard button "Send reply"'))
        self.assertEqual(body["running"]["session"], "20250101_120000_ab12")
        self.assertEqual(self.client.post(url, json={}).status_code, 400)
        self.assertEqual(self.client.post(f"{self.base}/records/{self.record['id']}/actions/nope", json={}).status_code, 404)

    def test_muting_a_record(self):
        url = f"{self.base}/records/{self.record['id']}/mute"
        until = int(__import__("time").time()) + 3600
        self.assertEqual(self.client.post(url, json={"until": until}).json()["record"]["muted_until"], until)
        self.assertEqual(self.client.get(f"{self.base}/bots").json()["bots"]["concierge"]["needs_you"], 0)
        self.assertIsNone(self.client.post(url, json={"until": None}).json()["record"]["muted_until"])
        self.assertEqual(self.client.post(url, json={"until": 5}).status_code, 400)

    def test_unchanged_since_the_last_read(self):
        board = self.client.get(f"{self.base}/bots/concierge").json()
        same = self.client.get(f"{self.base}/bots/concierge", params={"since": board["version"]}).json()
        self.assertEqual((same["unchanged"], same["version"]), (True, board["version"]))
        self.client.patch(f"{self.base}/records/{self.record['id']}", json={"values": {"status": "Replied"}})
        changed = self.client.get(f"{self.base}/bots/concierge", params={"since": board["version"]}).json()
        self.assertNotIn("unchanged", changed)
        self.assertIn("views", changed)

    def test_bad_requests(self):
        self.assertEqual(self.client.get(f"{self.base}/records/nope").status_code, 404)
        self.assertEqual(self.client.patch(f"{self.base}/records/{self.record['id']}", json={"values": {"status": "Archived"}}).status_code, 400)
        self.assertEqual(self.client.get(f"{self.base}/bots/Not_A_Bot").status_code, 400)


if __name__ == "__main__":
    unittest.main()
