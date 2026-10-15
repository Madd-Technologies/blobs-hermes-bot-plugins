import base64
import sys
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ec

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import webpush  # noqa: E402


def private_from_b64u(text: str) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int.from_bytes(webpush.b64u_decode(text), "big"), ec.SECP256R1())


# RFC 8291 section 5, "Push Message Encryption Example" (the body is copied from the RFC text, not typed from memory).
RFC = {
    "plaintext": b"When I grow up, I want to be a watermelon",
    "as_private": "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw",
    "ua_private": "q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94",
    "ua_public": "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
    "auth": "BTBZMqHH6r4Tts7J_aSIgg",
    "salt": "DGv6ra1nlYgDCS1FRnbzlw",
    "body": "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN",
}


class RfcVector(unittest.TestCase):
    def test_reproduces_the_published_example_byte_for_byte(self):
        body = webpush.encrypt(
            RFC["plaintext"],
            RFC["ua_public"],
            RFC["auth"],
            salt=webpush.b64u_decode(RFC["salt"]),
            sender_private=private_from_b64u(RFC["as_private"]),
        )
        self.assertEqual(webpush.b64u_encode(body), RFC["body"])

    def test_the_device_can_decrypt_the_published_example(self):
        body = webpush.b64u_decode(RFC["body"])
        self.assertEqual(webpush.decrypt(body, private_from_b64u(RFC["ua_private"]), RFC["auth"]), RFC["plaintext"])


class RoundTrip(unittest.TestCase):
    def setUp(self):
        self.device = ec.generate_private_key(ec.SECP256R1())
        self.p256dh = webpush.b64u_encode(webpush._public_bytes(self.device.public_key()))
        self.auth = webpush.b64u_encode(b"0123456789abcdef")

    def test_a_message_only_the_device_can_read(self):
        payload = b'{"id":1,"title":"Juniper needs you","body":"Allow a command?"}'
        body = webpush.encrypt(payload, self.p256dh, self.auth)
        self.assertNotIn(b"Juniper", body)
        self.assertEqual(webpush.decrypt(body, self.device, self.auth), payload)

    def test_every_message_is_different(self):
        a = webpush.encrypt(b"same", self.p256dh, self.auth)
        b = webpush.encrypt(b"same", self.p256dh, self.auth)
        self.assertNotEqual(a, b, "fresh salt and key each time")

    def test_the_wrong_device_cannot_read_it(self):
        body = webpush.encrypt(b"secret", self.p256dh, self.auth)
        other = ec.generate_private_key(ec.SECP256R1())
        with self.assertRaises(Exception):
            webpush.decrypt(body, other, self.auth)

    def test_the_wrong_auth_secret_cannot_read_it(self):
        body = webpush.encrypt(b"secret", self.p256dh, self.auth)
        with self.assertRaises(Exception):
            webpush.decrypt(body, self.device, webpush.b64u_encode(b"fedcba9876543210"))

    def test_the_header_says_one_4096_byte_record_keyed_by_the_sender(self):
        body = webpush.encrypt(b"x", self.p256dh, self.auth)
        self.assertEqual(int.from_bytes(body[16:20], "big"), 4096)
        self.assertEqual(body[20], 65)
        self.assertEqual(body[21], 4, "an uncompressed point")

    def test_the_largest_payload_fits_and_one_more_byte_does_not(self):
        biggest = b"a" * webpush.MAX_PLAINTEXT
        self.assertEqual(webpush.decrypt(webpush.encrypt(biggest, self.p256dh, self.auth), self.device, self.auth), biggest)
        with self.assertRaises(ValueError):
            webpush.encrypt(biggest + b"a", self.p256dh, self.auth)

    def test_bad_keys_are_refused_before_any_crypto(self):
        with self.assertRaises(ValueError):
            webpush.encrypt(b"x", webpush.b64u_encode(b"\x04" + b"\x00" * 10), self.auth)
        with self.assertRaises(ValueError):
            webpush.encrypt(b"x", self.p256dh, webpush.b64u_encode(b"short"))


if __name__ == "__main__":
    unittest.main()
