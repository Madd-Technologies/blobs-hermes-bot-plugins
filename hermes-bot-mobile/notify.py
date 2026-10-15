"""Turning "a bot needs you" into a push on the phone, without ever blocking the bot.

A notification carries as little as possible: the bot and the chat or routine it is about, what it wants in a few words,
and where tapping it should land. **Never the command**, and never a result: a lock screen is not private. Only what
needs the person is pushed (an approval, a question, a routine that broke), never that a reply finished or a routine ran. The app fetches the real thing
(with its Allow / Deny buttons) once the person opens it.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import quote

try:
    from . import webpush
    from .store import DeviceStore
except ImportError:  # the tests import the modules flat
    import webpush  # type: ignore[no-redef]
    from store import DeviceStore  # type: ignore[no-redef]

log = logging.getLogger("hermes-bot-mobile")

#: The notification channel the phone files each kind under (its name is what the person sees in Android settings).
CHANNELS = {"approval": "Approvals", "question": "Questions", "routine": "Routines", "test": "Results"}

#: Seconds a push may wait in the push service if the phone is off, and how it is flagged for delivery.
TTL_SECONDS = {"approval": 300, "question": 3600, "routine": 3600, "test": 60}
URGENCY = {"approval": "high", "question": "high", "routine": "normal", "test": "high"}

BODY_LIMIT = 80
#: Room for "Bot · the chat's title".
TITLE_LIMIT = 60
SEND_TIMEOUT = 10.0


def tidy(text: str, limit: int = BODY_LIMIT) -> str:
    """One line, no markup noise, cut at `limit` with an ellipsis."""
    flat = re.sub(r"\s+", " ", str(text or "")).strip().strip("`*_#> ")

    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def describe(description: str, command: str) -> str:
    """What an approval is for, from the server's own description, or a generic line when that would expose the command.

    The description is normally a short label ("recursive delete"). Some scanners quote what they found, so it is
    redacted, and dropped if it repeats a stretch of the command.
    """
    text = str(description or "")

    try:
        from agent.redact import redact_sensitive_text

        text = redact_sensitive_text(text, force=True)
    except Exception:  # Hermes internals unavailable (tests): fall through to the command check below
        pass

    command = str(command or "").strip()
    window = 12

    if len(command) >= window:
        quoted = any(command[i : i + window] in text for i in range(0, len(command) - window + 1, 6))
    else:
        quoted = bool(command) and command in text.split()

    if quoted:
        text = ""

    return tidy(text) or "run a command"


def notification_id(*parts: str) -> int:
    """A positive 31-bit id, the same for the same thing, so a repeated push replaces its notification."""
    return zlib.crc32("\x1f".join(parts).encode()) & 0x7FFFFFFF


#: The app's Needs you list: where a push lands when the chat it is about cannot be told.
INBOX = "hermesbot://inbox"


def link(profile: str, *, chat: str = "", room: str = "", page: str = "") -> str:
    """Where tapping a push lands in the app: a group room, a page of the bot (`routines`), or the exact chat the push is
    about (its persistent session id: the bot's main chat or a side chat). With none of those, the bot's usual chat."""
    if room:
        return f"hermesbot://room/{quote(room, safe='')}"

    base = f"hermesbot://bot/{quote(profile, safe='')}"

    if page:
        return f"{base}/{page}"

    return f"{base}?chat={quote(chat, safe='')}" if chat else base


def build(kind: str, *, profile: str, bot: str, body: str, key: str, url: str | None = None) -> dict:
    """The payload the phone's push library renders by itself while the app is closed.

    `url` opens the app where the push is about (the notification is the first tap; Allow / Deny is the second).
    """
    return {
        "id": notification_id(kind, profile, key),
        "title": tidy(bot, TITLE_LIMIT) or "Hermes",
        "body": tidy(body),
        "type": CHANNELS[kind],
        "url": url or link(profile),
    }


def lines(kind: str, *, detail: str = "", failed: bool = False) -> str:
    """The sentence under the title: what the bot needs, in its own words where they are safe to show."""
    if kind == "approval":
        return f"Needs your OK: {detail}" if detail else "Needs your OK"

    if kind == "question":
        return f"Asks: {detail}" if detail else "Has a question for you"

    # A routine is only pushed when it broke.
    return f"Routine “{detail}” failed" if detail else "A routine failed"


@dataclass
class Delivery:
    device: str
    ok: bool
    status: int | None = None
    error: str | None = None
    gone: bool = False


def http_post(url: str, headers: dict, content: bytes, timeout: float) -> int:
    import httpx

    # No redirects: an endpoint that redirects is not one this plugin should follow into the network.
    return httpx.post(url, content=content, headers=headers, timeout=timeout, follow_redirects=False).status_code


class Dedupe:
    """Remembers what was already pushed for a few minutes, so a retried hook does not buzz the phone twice."""

    def __init__(self, keep_seconds: float = 600, clock=time.monotonic):
        self._keep = keep_seconds
        self._clock = clock
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def first(self, key: str) -> bool:
        now = self._clock()

        with self._lock:
            self._seen = {k: t for k, t in self._seen.items() if now - t < self._keep}

            if key in self._seen:
                return False

            self._seen[key] = now

            return True


class Pusher:
    def __init__(self, store: DeviceStore, post=None, background: bool = True):
        self.store = store
        self._post = post
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="hermes-bot-mobile") if background else None

    def send(self, kind: str, payload: dict) -> None:
        """Queue a push and return at once; a hook must never wait on the network."""
        if self._pool is None:
            self.deliver(kind, payload)
        else:
            self._pool.submit(self._guarded, kind, payload)

    def _guarded(self, kind: str, payload: dict) -> None:
        try:
            self.deliver(kind, payload)
        except Exception:
            log.exception("push failed")

    def deliver(self, kind: str, payload: dict, only: str | None = None) -> list[Delivery]:
        """Encrypt for each registered device (or just `only`) and post it. Devices the push service says are gone
        are forgotten."""
        plain = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()

        return [self._one(device, kind, plain) for device in self.store.devices() if only is None or device.get("id") == only]

    def _one(self, device: dict, kind: str, plain: bytes) -> Delivery:
        identity = device.get("id", "")

        try:
            body = webpush.encrypt(plain, device["p256dh"], device["auth"])
            headers = {
                "Content-Encoding": "aes128gcm",
                "Content-Type": "application/octet-stream",
                "TTL": str(TTL_SECONDS.get(kind, 3600)),
                "Urgency": URGENCY.get(kind, "normal"),
            }
            status = (self._post or http_post)(device["endpoint"], headers, body, SEND_TIMEOUT)
        except Exception as failure:
            error = type(failure).__name__
            self.store.record(identity, error)

            return Delivery(identity, False, error=error)

        if 200 <= status < 300:
            self.store.record(identity, None)

            return Delivery(identity, True, status=status)

        # 404 and 410 are how a push service says the subscription no longer exists (RFC 8030 section 7.3).
        if status in (404, 410):
            self.store.remove(identity)

            return Delivery(identity, False, status=status, error="gone", gone=True)

        error = f"http {status}"
        self.store.record(identity, error)

        return Delivery(identity, False, status=status, error=error)
