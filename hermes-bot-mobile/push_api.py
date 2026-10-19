"""A small, stable way for this server's other plugins and services to push to the person's phones (since 0.1.7).

They use the phones this plugin registered and its VAPID key, which live in files under the Hermes root (`store.py`) that
separate processes already share. So another process, such as hermes-phone's call service, loads this module from the
installed plugin and calls `send`:

    send("call", profile="default", bot="Juniper", body="3 calls are ready for your OK", key="errand-3f2a",
         url="hermesbot://inbox")

Only the kinds in `KINDS` can be sent this way; approvals, questions and replies stay with this plugin's own hooks. A bot set
to `off` in the app's Notifications gets nothing. The phone's lock-screen settings for the "Calls" channel decide what shows.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

try:
    from . import notify
    from .store import DeviceStore
except ImportError:  # the tests import the modules flat
    import notify  # type: ignore[no-redef]
    from store import DeviceStore  # type: ignore[no-redef]

#: `call`: a phone errand waits for the person's OK, or finished. `call_question`: a call on the line waits ~60 s for an answer.
KINDS = ("call", "call_question")
VERSION = 1


def send(kind: str, *, profile: str, bot: str, body: str, key: str, url: Optional[str] = None, folder: Optional[Path] = None,
         post=None) -> dict:
    """Push to every registered phone now (not in the background: the caller is a service with its own threads) and say how it
    went: `{"sent": n, "devices": n}`, or `{"sent": 0, "skipped": "off"}`. Never raises for a device that fails; raises
    `ValueError` for a kind it doesn't send."""
    if kind not in KINDS:
        raise ValueError(f"push_api sends only {', '.join(KINDS)}")

    store = DeviceStore(folder) if folder is not None else DeviceStore()

    if store.level(profile) == "off":
        return {"sent": 0, "skipped": "off"}

    payload = notify.build(kind, profile=profile, bot=bot, body=notify.lines(kind, detail=notify.redact(body)), key=key,
                           url=url or notify.INBOX)
    deliveries = notify.Pusher(store, post=post, background=False).deliver(kind, payload)

    return {"sent": sum(1 for delivery in deliveries if delivery.ok), "devices": len(deliveries)}
