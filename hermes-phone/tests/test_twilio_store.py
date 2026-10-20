"""Twilio signatures, TwiML and REST against a fake poster; the shared store's files."""

import os
import stat
import tempfile
import unittest
from pathlib import Path

from helpers import CALLBACK, SETTINGS, septic

import brief as B  # noqa: E402
import store as S  # noqa: E402
import twilio_api as T  # noqa: E402


class Signatures(unittest.TestCase):
    PARAMS = {"Digits": "1234", "To": "+18005551212", "From": "+14158675310", "Caller": "+14158675310", "CallSid": "CA1234567890ABCDE"}

    def test_twilios_published_example(self):
        self.assertEqual(T.signature("12345", "https://example.com/myapp.php?foo=1&bar=2", self.PARAMS), "L/OH5YylLD5NRKLltdqwSvS0BnU=")

    def test_voice_callbacks_are_signed_without_the_port(self):
        url = "https://node.example.ts.net:8443/twilio/status/tok"
        header = T.signature("secret", "https://node.example.ts.net/twilio/status/tok", self.PARAMS)
        self.assertTrue(T.valid("secret", url, self.PARAMS, header))

    def test_the_stream_handshake_with_or_without_a_trailing_slash(self):
        url = "wss://node.example.ts.net/twilio/stream/tok"
        self.assertTrue(T.valid("secret", url, {}, T.signature("secret", url, {})))
        self.assertTrue(T.valid("secret", url, {}, T.signature("secret", url + "/", {})))

    def test_wrong_or_missing(self):
        url = "wss://node.example.ts.net/twilio/stream/tok"
        self.assertFalse(T.valid("secret", url, {}, T.signature("other", url, {})))
        self.assertFalse(T.valid("secret", url, {}, T.signature("secret", "https://node.example.ts.net/twilio/stream/tok", {})))
        self.assertFalse(T.valid("secret", url, {}, None))
        self.assertFalse(T.valid("", url, {}, "x"))

    def test_twiml(self):
        self.assertEqual(T.stream_twiml('wss://h/twilio/stream/a"b'),
                         '<?xml version="1.0" encoding="UTF-8"?><Response><Connect><Stream url=\'wss://h/twilio/stream/a"b\'/></Connect><Hangup/></Response>')
        self.assertIn("<Reject/>", T.reject_twiml())


class Rest(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def post(url, fields, user, password, timeout):
            self.calls.append((url, fields, user, password))
            return self.reply

        self.reply = (201, {"sid": "CA123"})
        self.post = post

    def test_create_call(self):
        rest = T.Rest("AC1", "token", post=self.post)
        sid = rest.create_call("+12025550101", CALLBACK, "<Response/>", "https://h/s", "https://h/a", 1260)
        url, fields, user, password = self.calls[0]
        self.assertEqual(sid, "CA123")
        self.assertEqual(url, f"{T.API}/Accounts/AC1/Calls.json")
        self.assertEqual((user, password), ("AC1", "token"))
        pairs = dict(fields)
        self.assertEqual((pairs["To"], pairs["From"], pairs["MachineDetection"], pairs["AsyncAmd"], pairs["TimeLimit"]),
                         ("+12025550101", CALLBACK, "DetectMessageEnd", "true", "1260"))
        self.assertEqual([v for k, v in fields if k == "StatusCallbackEvent"], ["initiated", "ringing", "answered", "completed"])

    def test_call_status(self):
        seen = []

        def get(url, user, password, timeout):
            seen.append((url, user, password))
            return 200, {"sid": "CA1", "status": "busy"}

        self.assertEqual(T.Rest("AC1", "token", get=get).call_status("CA1"), "busy")
        self.assertEqual(seen, [(f"{T.API}/Accounts/AC1/Calls/CA1.json", "AC1", "token")])

    def test_an_api_key_is_preferred(self):
        T.Rest("AC1", "token", "SK1", "keysecret", post=self.post).hang_up("CA1")
        url, fields, user, password = self.calls[0]
        self.assertEqual((url.endswith("/Calls/CA1.json"), fields, user, password), (True, [("Status", "completed")], "SK1", "keysecret"))

    def test_refusals_carry_twilios_reason(self):
        self.reply = (400, {"code": 21215, "message": "Account not authorized to call +44... (Geo Permissions)"})
        with self.assertRaises(T.TwilioError) as caught:
            T.Rest("AC1", "token", post=self.post).create_call("+1", "+1", "", "", "", 60)
        self.assertEqual(caught.exception.code, 21215)

    def test_env_and_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            env = Path(folder) / ".env"
            env.write_text("# comment\nexport HERMES_PHONE_TWILIO_ACCOUNT_SID=AC1\nHERMES_PHONE_TWILIO_AUTH_TOKEN='tok'\n"
                           "HERMES_PHONE_TWILIO_PHONE_NUMBER=\"+12025550123\"\n")
            creds = T.credentials(T.read_env(env))
            self.assertEqual((creds["account_sid"], creds["auth_token"], creds["number"], creds["api_key"]), ("AC1", "tok", "+12025550123", ""))
            self.assertIsNone(T.credentials({"HERMES_PHONE_TWILIO_ACCOUNT_SID": "AC1"}))
            written = {"HERMES_PHONE_TWILIO_ACCOUNT_SID": "AC1", "HERMES_PHONE_TWILIO_AUTH_TOKEN": "tok", "HERMES_PHONE_TWILIO_PHONE_NUMBER": "+1 (202) 555-0123",
                       "HERMES_PHONE_TWILIO_API_KEY": "SK1", "HERMES_PHONE_TWILIO_API_SECRET": "keysecret"}
            self.assertEqual((T.credentials(written)["number"], T.credentials(written)["api_key"], T.credentials(written)["api_secret"]),
                             ("+12025550123", "SK1", "keysecret"))

    def test_hermes_own_twilio_names_are_never_read(self):
        # Hermes's SMS channel and telephony skill read these; keys under them are not this plugin's.
        shared = {"TWILIO_ACCOUNT_SID": "AC1", "TWILIO_AUTH_TOKEN": "tok", "TWILIO_PHONE_NUMBER": "+12025550123"}
        self.assertIsNone(T.credentials(shared))
        self.assertIsNone(T.credentials(shared | {"HERMES_PHONE_TWILIO_ACCOUNT_SID": "AC2"}))


class Store(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.t = [1000.0]
        self.store = S.Store(Path(self.dir.name), clock=lambda: self.t[0])
        self.brief, self.flags = B.check(septic(), SETTINGS, CALLBACK)

    def tearDown(self):
        self.dir.cleanup()

    def test_defaults_and_checked_changes(self):
        self.assertEqual(self.store.settings()["stage"], "test")
        self.store.update_settings({"name": "Alex", "daily_cap": 5, "hours": [9, 18]})
        self.assertEqual((self.store.settings()["name"], self.store.settings()["daily_cap"]), ("Alex", 5))
        for bad in ({"daily_cap": 500}, {"hours": [20, 8]}, {"stage": "prod"}, {"public_url": "http://x"}, {"unknown": 1}, {"transcripts": "yes"}):
            with self.subTest(bad=bad), self.assertRaises(S.SettingsError):
                self.store.update_settings(bad)

    def test_files_are_private(self):
        self.store.update_settings({"never_share": [{"label": "Home", "value": "12 Orchard Lane"}]})
        mode = stat.S_IMODE(os.stat(self.store.folder / "settings.json").st_mode)
        self.assertEqual(mode, 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.store.folder).st_mode), 0o700)

    def test_errands_and_calls(self):
        errand = self.store.new_errand("default", "s1", self.brief, self.flags)
        self.assertEqual(errand["status"], "pending")
        self.assertEqual(len(errand["calls"]), 2)
        self.assertNotEqual(errand["calls"][0]["token"], errand["calls"][1]["token"])
        self.assertIsNone(self.store.find_call(errand["calls"][0]["token"]))  # pending errands aren't dialable
        self.store.update_errand(errand["id"], lambda e: e.update(status="approved"))
        found = self.store.find_call(errand["calls"][1]["token"])
        self.assertEqual(found[1]["name"], "Harbor Pumping")
        self.store.update_call(errand["id"], found[1]["id"], lambda c: c.update(status="dialing"))
        self.assertEqual(self.store.errand(errand["id"])["calls"][1]["status"], "dialing")
        self.assertIsNone(self.store.find_call("nope"))
        self.assertIsNone(self.store.errand("../../etc"))

    def test_pending_errands_expire(self):
        errand = self.store.new_errand("default", "", self.brief, self.flags)
        self.t[0] += S.PENDING_HOURS * 3600 + 1
        self.store.expire_pending()
        self.assertEqual(self.store.errand(errand["id"])["status"], "expired")

    def test_ledger_and_daily_starts(self):
        self.store.remember_number("+12025550101", {"errand": "e_1"})
        self.assertIsNotNone(self.store.known_number("+12025550101", 14))
        self.t[0] += 15 * 86400
        self.assertIsNone(self.store.known_number("+12025550101", 14))
        for _ in range(3):
            self.store.log_start()
        self.assertEqual(self.store.starts_today(), 3)
        self.t[0] += 86401
        self.assertEqual(self.store.starts_today(), 0)

    def test_the_brief_hash_changes_with_the_brief(self):
        changed = dict(self.brief, goal="Something else.")
        self.assertNotEqual(S.brief_hash(self.brief), S.brief_hash(changed))

    def test_heartbeat(self):
        self.assertFalse(self.store.service()["running"])
        self.store.beat({"pid": 1})
        self.assertTrue(self.store.service()["running"])
        self.t[0] += 3 * S.HEARTBEAT_S + 1
        self.assertFalse(self.store.service()["running"])


if __name__ == "__main__":
    unittest.main()
