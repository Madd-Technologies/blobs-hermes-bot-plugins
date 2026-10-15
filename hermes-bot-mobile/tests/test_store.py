import tempfile
import threading
import unittest
from pathlib import Path

from helpers import Phone  # noqa: F401  (also puts the plugin folder on sys.path)

import store  # noqa: E402


class Devices(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.clock = [1000]
        self.store = store.DeviceStore(Path(self.dir.name), clock=lambda: self.clock[0])
        self.phone = Phone()

    def tearDown(self):
        self.dir.cleanup()

    def add(self, phone=None, name="Test phone"):
        phone = phone or self.phone

        return self.store.upsert(phone.endpoint, phone.p256dh, phone.auth, name)

    def test_registers_and_lists(self):
        row = self.add()
        self.assertEqual(self.store.devices()[0]["id"], row["id"])
        self.assertEqual(self.store.devices()[0]["name"], "Test phone")

    def test_the_same_endpoint_replaces_its_row(self):
        first = self.add()
        again = Phone(self.phone.endpoint)
        second = self.store.upsert(again.endpoint, again.p256dh, again.auth, "Test phone (new keys)")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.store.devices()), 1)
        self.assertEqual(self.store.devices()[0]["p256dh"], again.p256dh)

    def test_a_file_with_secrets_is_private_to_the_owner(self):
        self.add()
        mode = (Path(self.dir.name) / "devices.json").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_rejects_what_cannot_be_pushed_to(self):
        for endpoint, p256dh, auth in [
            ("", self.phone.p256dh, self.phone.auth),
            ("ftp://x/y", self.phone.p256dh, self.phone.auth),
            ("https://", self.phone.p256dh, self.phone.auth),
            (self.phone.endpoint, "AAAA", self.phone.auth),
            (self.phone.endpoint, self.phone.p256dh, "AAAA"),
            (self.phone.endpoint, "not base64!!", self.phone.auth),
            ("https://x/" + "a" * store.MAX_ENDPOINT, self.phone.p256dh, self.phone.auth),
        ]:
            with self.subTest(endpoint=endpoint[:20], p256dh=p256dh[:8], auth=auth[:8]):
                with self.assertRaises(ValueError):
                    self.store.upsert(endpoint, p256dh, auth)

        self.assertEqual(self.store.devices(), [])

    def test_caps_the_number_of_devices(self):
        for number in range(store.MAX_DEVICES):
            self.add(Phone(f"https://ntfy.example/up{number}"))

        with self.assertRaises(ValueError):
            self.add(Phone("https://ntfy.example/one-too-many"))

        # An existing device can still re-register at the cap.
        self.add(Phone("https://ntfy.example/up0"))

    def test_removes_a_device(self):
        row = self.add()
        self.assertTrue(self.store.remove(row["id"]))
        self.assertFalse(self.store.remove(row["id"]))
        self.assertEqual(self.store.devices(), [])

    def test_records_how_the_last_push_went(self):
        row = self.add()
        self.clock[0] = 2000
        self.store.record(row["id"], "http 500")
        device = self.store.devices()[0]
        self.assertEqual((device["last_error"], device["last_attempt"]), ("http 500", 2000))
        self.assertNotIn("last_ok", device)

        self.clock[0] = 3000
        self.store.record(row["id"], None)
        device = self.store.devices()[0]
        self.assertEqual((device["last_error"], device["last_ok"]), (None, 3000))

    def test_concurrent_registrations_do_not_lose_rows(self):
        phones = [Phone(f"https://ntfy.example/up{n}") for n in range(8)]
        threads = [threading.Thread(target=self.add, args=(p,)) for p in phones]

        for thread in threads:
            thread.start()

        for thread in threads:
            thread.join()

        self.assertEqual(len(self.store.devices()), 8)

    def test_an_unreadable_file_reads_as_empty_instead_of_breaking_pushes(self):
        (Path(self.dir.name) / "devices.json").write_text("{not json")
        self.assertEqual(self.store.devices(), [])
        self.add()
        self.assertEqual(len(self.store.devices()), 1)


class Vapid(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = store.DeviceStore(Path(self.dir.name))

    def tearDown(self):
        self.dir.cleanup()

    def test_is_what_the_phones_push_library_accepts(self):
        # The library checks ^[A-Za-z0-9_-]{87}$ and throws on anything else, an empty string included.
        import re

        key = self.store.vapid_public()
        self.assertRegex(key, r"^[A-Za-z0-9_-]{87}$")
        self.assertTrue(re.fullmatch(r"[A-Za-z0-9_-]{87}", key))

    def test_is_a_real_p256_point(self):
        from cryptography.hazmat.primitives.asymmetric import ec

        point = store.webpush.b64u_decode(self.store.vapid_public())
        self.assertEqual((len(point), point[0]), (65, 4))
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)

    def test_is_made_once_and_kept(self):
        first = self.store.vapid_public()
        self.assertEqual(first, store.DeviceStore(Path(self.dir.name)).vapid_public())
        self.assertEqual(first, self.store.vapid_public())

    def test_private_half_matches_and_is_private_to_the_owner(self):
        import json

        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        public = self.store.vapid_public()
        path = Path(self.dir.name) / "vapid.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        number = int.from_bytes(store.webpush.b64u_decode(json.loads(path.read_text())["private"]), "big")
        derived = ec.derive_private_key(number, ec.SECP256R1()).public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.assertEqual(store.webpush.b64u_encode(derived), public)


class Settings(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = store.DeviceStore(Path(self.dir.name))

    def tearDown(self):
        self.dir.cleanup()

    def test_every_bot_is_loud_until_told_otherwise(self):
        self.assertEqual(self.store.level("anything"), "all")

    def test_a_bot_level_overrides_the_default_and_null_restores_it(self):
        self.store.set_settings("needs_you", {"juniper": "off"})
        self.assertEqual((self.store.level("juniper"), self.store.level("other")), ("off", "needs_you"))

        self.store.set_settings(None, {"juniper": None})
        self.assertEqual(self.store.level("juniper"), "needs_you")

    def test_rejects_an_unknown_level_without_changing_anything(self):
        self.store.set_settings("all", {"a": "off"})

        with self.assertRaises(ValueError):
            self.store.set_settings("loud", None)

        with self.assertRaises(ValueError):
            self.store.set_settings(None, {"b": "shout"})

        self.assertEqual(self.store.settings(), {"default": "all", "bots": {"a": "off"}})

    def test_ignores_junk_in_the_file(self):
        (Path(self.dir.name) / "settings.json").write_text('{"default": "nope", "bots": {"a": "off", "b": 3, "c": "x"}}')
        self.assertEqual(self.store.settings(), {"default": "all", "bots": {"a": "off"}})


if __name__ == "__main__":
    unittest.main()
