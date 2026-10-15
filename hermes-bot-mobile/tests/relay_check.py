"""Checks that a ntfy server relays our pushes byte for byte, the way UnifiedPush needs.

    ~/.hermes/hermes-agent/venv/bin/python hermes-bot-mobile/tests/relay_check.py https://ntfy.example

It publishes one encrypted push (made by the plugin's own code, to a throwaway device key) to a random, unsubscribed
`up…` topic, reads it back from ntfy and compares. Nobody is notified: no phone listens on that topic. The message
stays in ntfy's cache until it expires. Exit status 0 means the relay is fine.
"""

import base64
import json
import os
import secrets
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import webpush  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402


def main(base: str) -> int:
    base = base.rstrip("/")

    def open_url(request, timeout=10):
        return urllib.request.urlopen(request, timeout=timeout)

    with open_url(f"{base}/v1/health") as response:
        print("health:", response.read().decode().strip())

    device = ec.generate_private_key(ec.SECP256R1())
    p256dh = webpush.b64u_encode(device.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
    auth = webpush.b64u_encode(os.urandom(16))
    plain = json.dumps({"id": 1, "title": "Hermes", "body": "relay self-test", "type": "Results", "url": "hermesbot://inbox"}).encode()
    body = webpush.encrypt(plain, p256dh, auth)

    topic = "up" + secrets.token_urlsafe(9).replace("-", "x").replace("_", "y")[:12]
    request = urllib.request.Request(
        f"{base}/{topic}", data=body, method="POST", headers={"Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream", "TTL": "60", "Urgency": "low"}
    )

    try:
        with open_url(request) as response:
            print("publish:", response.status)
    except urllib.error.HTTPError as failure:
        print("publish refused:", failure.code, failure.read().decode()[:300])
        return 1

    with open_url(f"{base}/{topic}/json?poll=1") as response:
        lines = [json.loads(line) for line in response.read().decode().splitlines() if line.strip()]

    message = next((line for line in lines if line.get("event") == "message"), None)

    if message is None:
        print("the message was not stored")
        return 1

    print("stored with encoding:", message.get("encoding"))
    back = base64.b64decode(message["message"]) if message.get("encoding") == "base64" else message["message"].encode()
    same = back == body
    print("bytes identical after the relay:", same)

    if not same:
        return 1

    print("the device key can decrypt it:", json.loads(webpush.decrypt(back, device, auth))["body"])

    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)

    sys.exit(main(sys.argv[1]))
