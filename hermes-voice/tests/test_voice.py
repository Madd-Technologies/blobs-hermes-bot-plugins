"""hermes-voice against fakes: the routes through FastAPI's test client, a fake xAI and fake account readers.

    python -m unittest discover -s hermes-voice/tests
"""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
SIGN_IN = "sign-in-token-not-real"
FRESH = "fresh-sign-in-token-not-real"
KEY = "xai-key-not-real"
SECRET = "client-secret-not-real"
BASE = "/api/plugins/hermes-voice"


def load_api():
    """Load plugin_api.py the way `hermes dashboard` does: by path, under its own module name."""
    name = "hermes_dashboard_plugin_hermes-voice-test"
    spec = importlib.util.spec_from_file_location(name, ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)

    return module


class FakeXai:
    """Answers `POST /realtime/client_secrets` with the replies queued, and remembers what it was asked."""

    def __init__(self, *replies):
        self.replies = list(replies) or [(200, {"value": SECRET, "expires_at": 1_900_000_000})]
        self.calls = []

    def __call__(self, url, headers, payload, timeout):
        self.calls.append({"url": url, "headers": headers, "payload": payload})
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]

        if isinstance(reply, Exception):
            raise reply

        return reply


class FakeAccounts:
    """The server's sign-in and the bot's key, as the plugin's readers would find them."""

    def __init__(self, sign_in=SIGN_IN, key=""):
        self.token = sign_in
        self.key = key
        self.refreshes = []

    def readers(self, core):
        def sign_in(refresh, force):
            if force:
                self.refreshes.append(force)
                self.token = FRESH
            if not self.token:
                raise RuntimeError("no xai-oauth tokens")
            return self.token

        return core.Accounts(sign_in, lambda: self.key)


class Voice(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.home = Path(self.dir.name)
        (self.home / "profiles" / "harbor").mkdir(parents=True)
        self.api = load_api()
        self.core = self.api._core()
        self.xai = FakeXai()
        self.accounts = FakeAccounts()
        self.api.root = lambda: self.home
        self.api.accounts = lambda: self.accounts.readers(self.core)
        self.api.poster = lambda *args: self.xai(*args)
        app = FastAPI()
        app.include_router(self.api.router, prefix=BASE)
        self.client = TestClient(app)

    def tearDown(self):
        self.dir.cleanup()

        for key in [k for k in sys.modules if k.startswith("_hermes_voice_core") or k.startswith("hermes_dashboard_plugin_hermes-voice-test")]:
            del sys.modules[key]

    def bot(self, profile="default"):
        return self.client.get(f"{BASE}/bot", params={"profile": profile})

    def change(self, profile="default", **body):
        return self.client.put(f"{BASE}/bot", params={"profile": profile}, json=body)

    def call(self, profile="default", **body):
        return self.client.post(f"{BASE}/session", params={"profile": profile}, json=body or None)

    def assertRefused(self, response, status, reason):
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.json()["detail"]["reason"], reason)

    def assertNoCredentials(self, text):
        for credential in (SIGN_IN, FRESH, KEY):
            self.assertNotIn(credential, text)

    # Settings

    def test_status_names_the_version_and_provider(self):
        manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))
        body = self.client.get(f"{BASE}/status").json()
        self.assertEqual(body, {"version": manifest["version"], "provider": "xai", "model": "grok-voice-latest"})
        self.assertEqual(json.loads((ROOT / "dashboard" / "manifest.json").read_text())["version"], manifest["version"])

    def test_a_bot_never_set_has_calls_on_with_the_sign_in(self):
        self.assertEqual(self.bot().json(), {
            "enabled": True,
            "voice": "eve",
            "account": "sign-in",
            "available": {"sign_in": True, "key": False},
            "ready": True,
            "model": "grok-voice-latest",
        })

    def test_without_a_sign_in_a_bot_defaults_to_its_own_key(self):
        self.accounts = FakeAccounts(sign_in="", key=KEY)
        body = self.bot("harbor").json()
        self.assertEqual((body["account"], body["ready"], body["available"]), ("key", True, {"sign_in": False, "key": True}))

    def test_with_neither_the_bot_is_not_ready(self):
        self.accounts = FakeAccounts(sign_in="")
        body = self.bot().json()
        self.assertEqual((body["account"], body["ready"]), ("key", False))

    def test_changes_are_kept_per_bot(self):
        body = self.change("harbor", enabled=False, voice="rex", account="key").json()
        self.assertEqual((body["enabled"], body["voice"], body["account"], body["ready"]), (False, "rex", "key", False))
        self.assertEqual(self.bot("harbor").json()["voice"], "rex")
        self.assertEqual(self.bot().json()["voice"], "eve")
        self.assertEqual(self.change("harbor", voice="ara").json()["enabled"], False)

        saved = json.loads((self.home / "hermes-voice" / "bots.json").read_text())
        self.assertEqual(saved, {"bots": {"harbor": {"account": "key", "enabled": False, "voice": "ara"}}})

    def test_refuses_settings_it_does_not_know(self):
        self.assertRefused(self.change(voice="morgan"), 400, "invalid")
        self.assertRefused(self.change(account="api"), 400, "invalid")
        self.assertFalse((self.home / "hermes-voice" / "bots.json").exists())

    def test_refuses_bots_the_server_does_not_have(self):
        self.assertRefused(self.bot("juniper"), 404, "unknown_bot")
        self.assertRefused(self.bot("../default"), 400, "invalid")
        self.assertRefused(self.change("juniper", voice="rex"), 404, "unknown_bot")
        self.assertRefused(self.call("juniper"), 404, "unknown_bot")

    def test_a_damaged_settings_file_reads_as_defaults(self):
        (self.home / "hermes-voice").mkdir()
        (self.home / "hermes-voice" / "bots.json").write_text("{not json")
        self.assertEqual(self.bot().json()["voice"], "eve")
        self.assertEqual(self.change(voice="sal").json()["voice"], "sal")

    # Calls

    def test_a_call_on_the_sign_in(self):
        response = self.call(name="Harbor", about="Keeps the house running")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()

        self.assertEqual(self.xai.calls[0]["url"], "https://api.x.ai/v1/realtime/client_secrets")
        self.assertEqual(self.xai.calls[0]["headers"]["Authorization"], f"Bearer {SIGN_IN}")
        self.assertEqual(self.xai.calls[0]["payload"], {"expires_after": {"seconds": 60}})

        self.assertEqual(body["url"], "wss://api.x.ai/v1/realtime?model=grok-voice-latest")
        self.assertEqual((body["protocol_prefix"], body["client_secret"], body["expires_at"]), ("xai-client-secret.", SECRET, 1_900_000_000))
        self.assertEqual(body["account"], "sign-in")

        session = body["session"]
        self.assertEqual(session["voice"], "eve")
        self.assertIn("You are Harbor,", session["instructions"])
        self.assertIn("What you are for: Keeps the house running", session["instructions"])
        self.assertIn("[Hermes needs you]", session["instructions"])
        self.assertEqual([tool["name"] for tool in session["tools"]], ["hermes_task", "end_call"])
        self.assertEqual(session["audio"]["input"], {"format": {"type": "audio/pcm", "rate": 24000}, "transcription": {"model": "grok-transcribe"}})
        self.assertNotIn("resumption", session)
        self.assertNoCredentials(response.text)

    def test_a_call_on_the_bots_key_uses_the_key_and_its_voice(self):
        self.accounts = FakeAccounts(key=KEY)
        self.change("harbor", account="key", voice="leo")
        body = self.call("harbor").json()
        self.assertEqual(self.xai.calls[0]["headers"]["Authorization"], f"Bearer {KEY}")
        self.assertEqual((body["account"], body["session"]["voice"]), ("key", "leo"))
        self.assertIn("You are Harbor,", body["session"]["instructions"])

    def test_never_switches_account_on_its_own(self):
        self.change(account="key")
        self.assertRefused(self.call(), 409, "no_key")
        self.accounts = FakeAccounts(sign_in="", key=KEY)
        self.change(account="sign-in")
        self.assertRefused(self.call(), 409, "no_sign_in")
        self.assertEqual(self.xai.calls, [])

    def test_no_call_when_calls_are_off(self):
        self.change(enabled=False)
        self.assertRefused(self.call(), 403, "off")
        self.assertEqual(self.xai.calls, [])

    def test_a_rotated_sign_in_is_refreshed_once(self):
        self.xai = FakeXai((401, None), (200, {"value": SECRET, "expires_at": 1_900_000_000}))
        response = self.call()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.accounts.refreshes, [True])
        self.assertEqual([c["headers"]["Authorization"] for c in self.xai.calls], [f"Bearer {SIGN_IN}", f"Bearer {FRESH}"])

    def test_a_key_is_not_retried(self):
        self.accounts = FakeAccounts(key=KEY)
        self.change(account="key")
        self.xai = FakeXai((401, None))
        self.assertRefused(self.call(), 502, "rejected")
        self.assertEqual(len(self.xai.calls), 1)

    def test_a_plan_that_does_not_include_voice_says_so(self):
        self.xai = FakeXai((403, None))
        response = self.call()
        self.assertRefused(response, 502, "rejected")
        self.assertIn("Grok plan", response.json()["detail"]["message"])

    def test_xai_unreachable(self):
        self.xai = FakeXai(TimeoutError("slow"))
        self.assertRefused(self.call(), 502, "unreachable")

    def test_an_answer_that_echoes_the_credential_is_refused(self):
        self.xai = FakeXai((200, {"value": SIGN_IN}))
        response = self.call()
        self.assertRefused(response, 502, "rejected")
        self.assertNoCredentials(response.text)

    def test_the_older_nested_answer_shape_is_read(self):
        self.xai = FakeXai((200, {"client_secret": {"value": SECRET, "expires_at": 5}}))
        body = self.call().json()
        self.assertEqual((body["client_secret"], body["expires_at"]), (SECRET, 5))

    def test_the_persona_takes_only_a_clean_short_name(self):
        body = self.call("harbor", name="Har\nbor\x00" + "x" * 200, about="a" * 900).json()
        instructions = body["session"]["instructions"]
        first = instructions.splitlines()[1]
        self.assertTrue(first.startswith("You are Har bor "), first)
        self.assertNotIn("\x00", instructions)
        self.assertNotIn("a" * 501, instructions)

        self.assertIn("You are Harbor,", self.call("harbor").json()["session"]["instructions"])

    def test_logs_never_carry_a_credential_or_the_secret(self):
        with self.assertLogs("hermes-voice", level="INFO") as logs:
            self.call()
            self.xai = FakeXai((403, None))
            self.call()

        text = "\n".join(logs.output)
        self.assertNoCredentials(text)
        self.assertNotIn(SECRET, text)


if __name__ == "__main__":
    unittest.main()
