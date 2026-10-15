"""Web Push message encryption (RFC 8291, content-encoding aes128gcm), standalone.

UnifiedPush hands an app an endpoint URL plus a P-256 public key (`p256dh`) and a 16-byte `auth` secret. A server
that wants the app's push service to show a notification while the app is not running must encrypt the payload to
those two values; the Android connector decrypts it and only then tells the app it is a real message. A plain
POST reaches the app's JavaScript but never becomes a notification.

Only `cryptography` is needed, which Hermes already depends on, so installing the plugin adds no packages.
"""

from __future__ import annotations

import base64
import os
import struct

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

#: A push service may reject a record larger than this, and one record is all a notification needs.
RECORD_SIZE = 4096
#: Room for the 16-byte GCM tag and the 1-byte padding delimiter inside one record.
MAX_PLAINTEXT = RECORD_SIZE - 16 - 1


def b64u_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _hkdf(salt: bytes, info: bytes, length: int, ikm: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def _public_bytes(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def encrypt(
    plaintext: bytes,
    p256dh: str,
    auth: str,
    *,
    salt: bytes | None = None,
    sender_private: ec.EllipticCurvePrivateKey | None = None,
) -> bytes:
    """The request body to POST to the endpoint (send it with `Content-Encoding: aes128gcm`).

    `p256dh` and `auth` are base64url, exactly as the app reported them. `salt` and `sender_private` exist so a
    test can reproduce the RFC's published vector; in use they are random per message.
    """
    if len(plaintext) > MAX_PLAINTEXT:
        raise ValueError(f"payload is {len(plaintext)} bytes; one record holds at most {MAX_PLAINTEXT}")

    ua_public_bytes = b64u_decode(p256dh)
    auth_secret = b64u_decode(auth)

    if len(ua_public_bytes) != 65 or ua_public_bytes[0] != 4:
        raise ValueError("p256dh must be an uncompressed P-256 point (65 bytes)")

    if len(auth_secret) != 16:
        raise ValueError("auth must be 16 bytes")

    ua_public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public_bytes)
    private = sender_private or ec.generate_private_key(ec.SECP256R1())
    as_public_bytes = _public_bytes(private.public_key())
    salt = salt if salt is not None else os.urandom(16)

    shared = private.exchange(ec.ECDH(), ua_public)
    # RFC 8291 section 3.4: the key material is bound to both public keys.
    ikm = _hkdf(auth_secret, b"WebPush: info\x00" + ua_public_bytes + as_public_bytes, 32, shared)
    cek = _hkdf(salt, b"Content-Encoding: aes128gcm\x00", 16, ikm)
    nonce = _hkdf(salt, b"Content-Encoding: nonce\x00", 12, ikm)

    # 0x02 marks the last (here: only) record; no further padding.
    ciphertext = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)

    # RFC 8188 header: salt, record size, key id length, key id (the sender's public key).
    return salt + struct.pack(">IB", RECORD_SIZE, len(as_public_bytes)) + as_public_bytes + ciphertext


def decrypt(body: bytes, ua_private: ec.EllipticCurvePrivateKey, auth: str) -> bytes:
    """The inverse of `encrypt`, as the receiving device does it. Used by tests, never by the plugin at runtime."""
    salt = body[:16]
    record_size, id_len = struct.unpack(">IB", body[16:21])
    as_public_bytes = body[21 : 21 + id_len]
    ciphertext = body[21 + id_len :]

    if record_size < len(ciphertext):
        raise ValueError("more than one record: not produced by this module")

    ua_public_bytes = _public_bytes(ua_private.public_key())
    as_public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public_bytes)
    shared = ua_private.exchange(ec.ECDH(), as_public)
    ikm = _hkdf(b64u_decode(auth), b"WebPush: info\x00" + ua_public_bytes + as_public_bytes, 32, shared)
    cek = _hkdf(salt, b"Content-Encoding: aes128gcm\x00", 16, ikm)
    nonce = _hkdf(salt, b"Content-Encoding: nonce\x00", 12, ikm)
    record = AESGCM(cek).decrypt(nonce, ciphertext, None)
    end = record.rstrip(b"\x00")

    if not end.endswith(b"\x02"):
        raise ValueError("missing the final-record delimiter")

    return end[:-1]
