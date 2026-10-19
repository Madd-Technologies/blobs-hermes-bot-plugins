"""The bot's tools and the app's routes (through FastAPI's test client), on a throwaway Hermes root."""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from helpers import CALLBACK, ROOT, SETTINGS, septic

import phone_tools  # noqa: E402
import store as S  # noqa: E402

BASE = "/api/plugins/hermes-phone"
ENV = "TWILIO_ACCOUNT_SID=AC1\nTWILIO_AUTH_TOKEN=tok\nTWILIO_PHONE_NUMBER=+12025550123\n"


def load_api():
    name = "hermes_dashboard_plugin_hermes-phone-test"
    spec = importlib.util.spec_from_file_location(name, ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class Root:
    def __init__(self, env=ENV):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name)
        if env:
            (self.path / ".env").write_text(env)
        self.store = S.Store(self.path)
        self.store.update_settings({"name": "Alex", "never_share": SETTINGS["never_share"], "do_not_call": SETTINGS["do_not_call"]})
        self.pushes = []

    def tools(self, profile="default"):
        return phone_tools.PhoneTools(root=self.path, profile=lambda: profile, pusher=lambda *a, **k: self.pushes.append((a, k)))


class Tools(unittest.TestCase):
    def setUp(self):
        self.root = Root()

    def tearDown(self):
        self.root.dir.cleanup()

    def call(self, profile="default", **extra):
        return json.loads(self.root.tools(profile).phone_call(septic(**extra), session_id="sess1"))

    def test_a_brief_waits_for_the_persons_ok(self):
        out = self.call()
        self.assertEqual((out["status"], out["calls"]), ("waiting_for_ok", 2))
        errand = self.root.store.errand(out["errand_id"])
        self.assertEqual((errand["status"], errand["session_id"], errand["profile"]), ("pending", "sess1", "default"))
        (args, kwargs), = self.root.pushes
        self.assertEqual((args[1], kwargs["body"]), ("call", "2 calls wait for your OK: Septic pump-out quotes"))

    def test_flags_are_reported_to_the_bot(self):
        facts = [{"label": "Service address", "value": "12 Orchard Lane, Pinecrest"}]
        out = self.call(facts=facts)
        self.assertIn("never-share", out["flagged_for_their_tick"][0])

    def test_refusals(self):
        self.assertIn("never go on a call", self.call(facts=[{"label": "Gate PIN", "value": "1234"}])["error"])
        self.assertIn("isn't allowed", self.call(profile="harbor")["error"])
        no_line = Root(env="")
        try:
            self.assertIn("isn't set up", json.loads(no_line.tools().phone_call(septic()))["error"])
        finally:
            no_line.dir.cleanup()

    def test_the_report_labels_what_came_from_the_call(self):
        errand_id = self.call()["errand_id"]

        def finish(e):
            e["status"] = "done"
            e["calls"][0].update(status="done", answered_by="human", outcome={"status": "quoted", "base_price": "$385", "they_asked": "Send a photo of the lid"},
                                 answers=[{"question": "Is the lid exposed?", "answer": "Yes"}],
                                 transcript=[{"who": "them", "text": "It's $385.", "t": 3.0}])
            e["calls"][1].update(status="done", outcome={"status": "no_answer"})

        self.root.store.update_errand(errand_id, finish)
        out = json.loads(self.root.tools().phone_call_report({}))
        self.assertEqual(out["note"], phone_tools.THIRD_PARTY_NOTE)
        first = out["errands"][0]["calls"][0]
        self.assertEqual(first["from_the_call"], {"base_price": "$385"})
        self.assertEqual(first["they_asked"], "Send a photo of the lid")
        self.assertEqual(first["your_person_answered"], [{"question": "Is the lid exposed?", "answer": "Yes"}])
        self.assertNotIn("transcript", first)
        with_words = json.loads(self.root.tools().phone_call_report({"errand_id": errand_id, "transcript": True}))
        self.assertEqual(with_words["errands"][0]["calls"][0]["transcript"], [{"who": "them", "text": "It's $385."}])
        self.assertIn("No such", json.loads(self.root.tools("harbor").phone_call_report({"errand_id": errand_id}))["error"])


class Routes(unittest.TestCase):
    def setUp(self):
        self.root = Root()
        self.api = load_api()
        self.api.root = lambda: self.root.path
        app = FastAPI()
        app.include_router(self.api.router, prefix=BASE)
        self.client = TestClient(app)

    def tearDown(self):
        self.root.dir.cleanup()

    def errand(self, **extra):
        return json.loads(self.root.tools().phone_call(septic(**extra)))["errand_id"]

    def test_status(self):
        out = self.client.get(f"{BASE}/status").json()
        self.assertEqual((out["line"], out["public_url"], out["stage"], out["service"]["running"]),
                         ({"set": True, "number": "…23"}, False, "test", False))

    def test_never_share_values_never_come_back(self):
        out = self.client.get(f"{BASE}/settings").json()
        self.assertEqual(out["never_share"][0], {"label": "Home address", "hint": "…st"})
        self.assertNotIn("Orchard", json.dumps(out))
        out = self.client.put(f"{BASE}/settings", json={"never_share_add": [{"label": "Email", "value": "alex@example.com"}],
                                                         "never_share_remove": ["Personal cell"], "never_share": []}).json()
        self.assertEqual([e["label"] for e in out["never_share"]], ["Home address", "Email"])
        self.assertEqual(self.root.store.settings()["never_share"][1]["value"], "alex@example.com")
        self.assertEqual(self.client.put(f"{BASE}/settings", json={"daily_cap": 999}).status_code, 400)

    def test_approve_needs_every_flag_ticked(self):
        facts = septic()["facts"] + [{"label": "Service address", "value": "12 Orchard Lane, Pinecrest", "only_if_asked": True}]
        errand_id = self.errand(facts=facts)
        refused = self.client.post(f"{BASE}/errands/{errand_id}/approve", json={})
        self.assertEqual((refused.status_code, refused.json()["detail"]["reason"]), (409, "unticked"))
        ok = self.client.post(f"{BASE}/errands/{errand_id}/approve", json={"ticked": [3]}).json()
        self.assertEqual((ok["status"], ok["ticked"], ok["decided"]["by"]), ("approved", [3], "app"))
        self.assertEqual(ok["decided"]["hash"], S.brief_hash(ok["brief"]))
        self.assertNotIn("token", ok["calls"][0])
        self.assertEqual(self.client.post(f"{BASE}/errands/{errand_id}/approve", json={"ticked": [3]}).status_code, 409)

    def test_striking_out_facts_and_calls(self):
        facts = [{"label": "Service address", "value": "12 Orchard Lane"}] + septic()["facts"]
        errand_id = self.errand(facts=facts)
        ok = self.client.post(f"{BASE}/errands/{errand_id}/approve", json={"remove_facts": [0], "remove_calls": [0]}).json()
        self.assertEqual(ok["status"], "approved")
        self.assertEqual(len(ok["brief"]["facts"]), 3)
        self.assertEqual([(c["name"], c["index"]) for c in ok["calls"]], [("Harbor Pumping", 0)])
        self.assertEqual([c["name"] for c in ok["brief"]["calls"]], ["Harbor Pumping"])

    def test_ticks_follow_the_facts_after_a_removal(self):
        facts = septic()["facts"] + [{"label": "Service address", "value": "12 Orchard Lane"}]
        errand_id = self.errand(facts=facts)
        ok = self.client.post(f"{BASE}/errands/{errand_id}/approve", json={"remove_facts": [0], "ticked": [3]}).json()
        self.assertEqual((ok["status"], ok["ticked"]), ("approved", [2]))

    def test_decline_cancel_hangup_and_answer(self):
        errand_id = self.errand()
        self.assertEqual(self.client.post(f"{BASE}/errands/{errand_id}/decline").json()["status"], "declined")
        errand_id = self.errand()
        self.client.post(f"{BASE}/errands/{errand_id}/approve", json={})
        call_id = self.root.store.errand(errand_id)["calls"][0]["id"]
        self.root.store.update_call(errand_id, call_id, lambda c: c.update(status="live", question={"text": "Is the lid exposed?", "answer": None}))
        late = self.client.post(f"{BASE}/errands/{errand_id}/calls/{self.root.store.errand(errand_id)['calls'][1]['id']}/answer", json={"text": "Yes"})
        self.assertEqual(late.status_code, 409)
        refused = self.client.post(f"{BASE}/errands/{errand_id}/calls/{call_id}/answer", json={"text": "Fine up to $400"})
        self.assertEqual((refused.status_code, refused.json()["detail"]["reason"]), (400, "not_on_a_call"))
        answered = self.client.post(f"{BASE}/errands/{errand_id}/calls/{call_id}/answer", json={"text": "  Yes,\n it is  "}).json()
        self.assertEqual(answered["calls"][0]["question"]["answer"], "Yes, it is")
        cancelled = self.client.post(f"{BASE}/errands/{errand_id}/cancel").json()
        self.assertEqual((cancelled["status"], cancelled["calls"][0]["hangup"], cancelled["calls"][1]["status"]), ("cancelled", True, "cancelled"))

    def test_lists(self):
        self.errand()
        rows = self.client.get(f"{BASE}/errands?open=true").json()["errands"]
        self.assertEqual((rows[0]["title"], rows[0]["status"], rows[0]["calls"]), ("Septic pump-out quotes", "pending", 2))
        self.assertEqual(self.client.get(f"{BASE}/errands/e_nope").status_code, 404)


if __name__ == "__main__":
    unittest.main()
