import json
import tempfile
import unittest
from pathlib import Path

from helpers import Phone, Recorder

import notify  # noqa: E402
import push_api  # noqa: E402
import store  # noqa: E402


class PushApi(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.folder = Path(self.dir.name)
        self.store = store.DeviceStore(self.folder)
        self.phone = Phone()
        self.store.upsert(self.phone.endpoint, self.phone.p256dh, self.phone.auth, "Test phone")
        self.net = Recorder(201)

    def tearDown(self):
        self.dir.cleanup()

    def send(self, kind="call", **extra):
        args = {"profile": "juniper", "bot": "Juniper", "body": "3 calls are ready for your OK", "key": "errand-1",
                "folder": self.folder, "post": self.net, **extra}
        return push_api.send(kind, **args)

    def test_a_call_push_reaches_the_phone_on_the_calls_channel(self):
        self.assertEqual(self.send(), {"sent": 1, "devices": 1})
        url, headers, body = self.net.calls[0]
        payload = json.loads(self.phone.read(body))
        self.assertEqual(url, self.phone.endpoint)
        self.assertEqual((payload["title"], payload["type"], payload["body"]), ("Juniper", "Calls", "3 calls are ready for your OK"))
        self.assertEqual(payload["url"], notify.INBOX)
        self.assertEqual((headers["TTL"], headers["Urgency"]), ("3600", "high"))

    def test_a_question_on_the_line_expires_in_a_minute(self):
        self.send("call_question", body="Pine Valley asks: is the lid exposed?", url="hermesbot://bot/juniper")
        url, headers, body = self.net.calls[0]
        payload = json.loads(self.phone.read(body))
        self.assertEqual((headers["TTL"], payload["type"], payload["url"]), ("60", "Calls", "hermesbot://bot/juniper"))

    def test_the_same_key_replaces_its_notification(self):
        self.send()
        self.send(body="Done: 2 quotes")
        first, second = (json.loads(self.phone.read(call[2])) for call in self.net.calls)
        self.assertEqual(first["id"], second["id"])

    def test_a_bot_set_to_off_gets_nothing(self):
        self.store.set_settings(None, {"juniper": "off"})
        self.assertEqual(self.send(), {"sent": 0, "skipped": "off"})
        self.assertEqual(self.net.calls, [])

    def test_other_kinds_stay_with_the_hooks(self):
        for kind in ("approval", "question", "reply", "routine"):
            with self.assertRaises(ValueError):
                self.send(kind)

    def test_the_body_is_one_short_line(self):
        self.send(body="Pine Valley asks:\n\n" + "x" * 300)
        payload = json.loads(self.phone.read(self.net.calls[0][2]))
        self.assertLessEqual(len(payload["body"]), notify.BODY_LIMIT)
        self.assertNotIn("\n", payload["body"])

    def test_a_failing_device_is_counted_not_raised(self):
        self.net.status = 500
        self.assertEqual(self.send(), {"sent": 0, "devices": 1})


if __name__ == "__main__":
    unittest.main()
