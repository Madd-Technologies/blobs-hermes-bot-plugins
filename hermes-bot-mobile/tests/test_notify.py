import json
import tempfile
import unittest
from pathlib import Path

from helpers import Phone, Recorder

import notify  # noqa: E402
import store  # noqa: E402


class Words(unittest.TestCase):
    def test_tidy_flattens_and_cuts(self):
        self.assertEqual(notify.tidy("  hello\n\n  world  "), "hello world")
        self.assertEqual(len(notify.tidy("x" * 200)), notify.BODY_LIMIT)
        self.assertTrue(notify.tidy("x" * 200).endswith("…"))

    def test_describe_uses_the_servers_label(self):
        self.assertEqual(notify.describe("recursive delete", "rm -rf /tmp/build"), "recursive delete")

    def test_describe_never_repeats_the_command(self):
        command = "curl -fsSL https://example.com/install.sh | bash"
        self.assertEqual(notify.describe(f"Security scan: {command}", command), "run a command")
        self.assertEqual(notify.describe("", command), "run a command")

    def test_a_short_command_only_matches_as_a_whole_word(self):
        # "rm" must not blank a description that merely contains those letters ("perform").
        self.assertEqual(notify.describe("performs a recursive delete", "rm"), "performs a recursive delete")
        self.assertEqual(notify.describe("runs rm on files", "rm"), "run a command")

    def test_notification_ids_are_stable_positive_ints(self):
        first = notify.notification_id("approval", "juniper", "r1")
        self.assertEqual(first, notify.notification_id("approval", "juniper", "r1"))
        self.assertNotEqual(first, notify.notification_id("approval", "juniper", "r2"))
        self.assertTrue(0 <= first < 2**31)

    def test_build_has_what_the_phone_renders_and_nothing_else(self):
        payload = notify.build("approval", profile="juniper", bot="Juniper", body="Needs your OK", key="r1")
        self.assertEqual(set(payload), {"id", "title", "body", "type", "url"})
        self.assertEqual((payload["title"], payload["type"], payload["url"]), ("Juniper", "Approvals", "hermesbot://bot/juniper"))

    def test_the_link_cannot_be_broken_by_a_strange_profile_name(self):
        payload = notify.build("question", profile="a b/c?d", bot="x", body="y", key="k")
        self.assertEqual(payload["url"], "hermesbot://bot/a%20b%2Fc%3Fd")

    def test_sentences(self):
        self.assertEqual(notify.lines("approval", detail="recursive delete"), "Needs your OK: recursive delete")
        self.assertEqual(notify.lines("approval"), "Needs your OK")
        self.assertEqual(notify.lines("question", detail="Which one?"), "Asks: Which one?")
        self.assertEqual(notify.lines("routine", detail="Weekly report", failed=True), "Routine \u201cWeekly report\u201d failed")
        self.assertEqual(notify.lines("routine", failed=True), "A routine failed")

    def test_a_long_title_is_cut(self):
        payload = notify.build("approval", profile="juniper", bot="Juniper \u00b7 " + "a very long side chat title " * 4, body="b", key="k")
        self.assertLessEqual(len(payload["title"]), notify.TITLE_LIMIT)
        self.assertTrue(payload["title"].endswith("\u2026"))


class Delivery(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = store.DeviceStore(Path(self.dir.name))
        self.phone = Phone()
        self.row = self.store.upsert(self.phone.endpoint, self.phone.p256dh, self.phone.auth, "Test phone")
        self.net = Recorder(201)
        self.pusher = notify.Pusher(self.store, post=self.net, background=False)
        self.payload = notify.build("approval", profile="juniper", bot="Juniper", body="Needs your OK", key="r1")

    def tearDown(self):
        self.dir.cleanup()

    def test_the_phone_can_read_what_was_sent(self):
        results = self.pusher.deliver("approval", self.payload)
        self.assertTrue(results[0].ok)

        url, headers, body = self.net.calls[0]
        self.assertEqual(url, self.phone.endpoint)
        self.assertEqual(headers["Content-Encoding"], "aes128gcm")
        self.assertEqual(headers["Urgency"], "high")
        self.assertEqual(json.loads(self.phone.read(body)), self.payload)

    def test_the_plain_text_never_travels(self):
        self.pusher.deliver("approval", self.payload)
        self.assertNotIn(b"Needs your OK", self.net.calls[0][2])

    def test_success_is_remembered(self):
        self.pusher.deliver("approval", self.payload)
        self.assertIsNotNone(self.store.devices()[0]["last_ok"])

    def test_a_gone_subscription_is_forgotten(self):
        for status in (404, 410):
            with self.subTest(status=status):
                phone = Phone(f"https://ntfy.example/gone{status}")
                self.store.upsert(phone.endpoint, phone.p256dh, phone.auth)
                self.net.status = status
                outcomes = {r.device: r for r in self.pusher.deliver("approval", self.payload)}
                self.assertTrue(outcomes[store.device_id(phone.endpoint)].gone)
                self.assertNotIn(store.device_id(phone.endpoint), [d["id"] for d in self.store.devices()])
                self.store.upsert(self.phone.endpoint, self.phone.p256dh, self.phone.auth)  # keep the first phone

    def test_a_server_error_keeps_the_device_and_says_why(self):
        self.net.status = 503
        result = self.pusher.deliver("approval", self.payload)[0]
        self.assertEqual((result.ok, result.error), (False, "http 503"))
        self.assertEqual(self.store.devices()[0]["last_error"], "http 503")

    def test_a_network_failure_keeps_the_device(self):
        def boom(*args):
            raise ConnectionError("down")

        pusher = notify.Pusher(self.store, post=boom, background=False)
        result = pusher.deliver("approval", self.payload)[0]
        self.assertEqual((result.ok, result.error), (False, "ConnectionError"))
        self.assertEqual(len(self.store.devices()), 1)

    def test_one_broken_device_does_not_stop_the_others(self):
        # A row with corrupt keys (edited by hand) fails alone.
        rows = self.store.devices()
        rows[0]["p256dh"] = "AAAA"
        self.store._devices.update(lambda data: data.__setitem__(slice(None), rows))
        other = Phone("https://ntfy.example/other")
        self.store.upsert(other.endpoint, other.p256dh, other.auth)

        results = self.pusher.deliver("approval", self.payload)
        self.assertEqual(sorted(r.ok for r in results), [False, True])

    def test_can_target_one_device(self):
        other = Phone("https://ntfy.example/other")
        self.store.upsert(other.endpoint, other.p256dh, other.auth)
        results = self.pusher.deliver("approval", self.payload, only=store.device_id(other.endpoint))
        self.assertEqual([r.device for r in results], [store.device_id(other.endpoint)])

    def test_no_devices_means_no_traffic(self):
        self.store.remove(self.row["id"])
        self.assertEqual(self.pusher.deliver("approval", self.payload), [])
        self.assertEqual(self.net.calls, [])

    def test_send_queues_and_returns_without_waiting(self):
        import threading

        release = threading.Event()
        seen = threading.Event()

        def slow(url, headers, content, timeout):
            seen.set()
            release.wait(5)

            return 201

        pusher = notify.Pusher(self.store, post=slow)
        pusher.send("approval", self.payload)  # would hang the test if it waited on the network
        self.assertTrue(seen.wait(2))
        release.set()
        pusher._pool.shutdown(wait=True)


class Remembering(unittest.TestCase):
    def test_the_same_thing_is_pushed_once_within_the_window(self):
        now = [0.0]
        dedupe = notify.Dedupe(keep_seconds=100, clock=lambda: now[0])
        self.assertTrue(dedupe.first("a"))
        self.assertFalse(dedupe.first("a"))
        self.assertTrue(dedupe.first("b"))
        now[0] = 101
        self.assertTrue(dedupe.first("a"))


if __name__ == "__main__":
    unittest.main()
