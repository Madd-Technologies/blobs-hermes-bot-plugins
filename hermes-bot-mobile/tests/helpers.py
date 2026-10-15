import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import webpush  # noqa: E402


class Phone:
    """A fake receiving device: the key pair a UnifiedPush distributor would hold, so a test can decrypt what was sent."""

    def __init__(self, endpoint: str = "https://ntfy.example/upAbC123"):
        self.private = ec.generate_private_key(ec.SECP256R1())
        public = self.private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.p256dh = webpush.b64u_encode(public)
        self.auth = webpush.b64u_encode(b"0123456789abcdef")
        self.endpoint = endpoint

    def read(self, body: bytes) -> bytes:
        return webpush.decrypt(body, self.private, self.auth)


class Recorder:
    """Stands in for the network: remembers every POST and answers with a chosen status."""

    def __init__(self, status: int = 201):
        self.status = status
        self.calls: list[tuple[str, dict, bytes]] = []

    def __call__(self, url, headers, content, timeout):
        self.calls.append((url, headers, content))

        return self.status
