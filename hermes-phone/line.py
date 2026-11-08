"""What the person hears and says on a call they listen in on or take over: pure, so it is tested without sockets.

- **Listening:** the other party's audio comes from Twilio in steady 20 ms frames, while what is sent to them (the voice,
  keypad tones) arrives from Grok in bursts faster than it plays. Each frame from Twilio is the clock: it is mixed with the
  next 20 ms of what was sent (dropped when Twilio is told to `clear`), so the person hears both sides as the other party
  does. The mix goes out in 100 ms chunks of 8 kHz μ-law.
- **Taking over:** the person's microphone (8 kHz μ-law from the app) goes to Twilio as it comes, and is mixed into what the
  voice hears, a frame at a time, so the voice keeps an ear on the whole conversation.

Backlogs are capped so a slow phone or a stalled socket costs a little audio, never a growing delay.
"""

from __future__ import annotations

import base64
import binascii
from typing import Optional, Tuple

try:
    from . import ulaw
except ImportError:  # the tests import the modules flat
    import ulaw  # type: ignore[no-redef]

CHUNK = 800  # 100 ms of 8 kHz μ-law
#: The most of what was sent to them that waits to be heard (Grok can run a reply well ahead of Twilio's playback).
SENT_MAX = 8000 * 60
#: The most of the person's microphone that waits to be mixed into the voice's input.
MIC_MAX = 8000 // 2


def _bytes(payload: str) -> bytes:
    try:
        return base64.b64decode(payload or "", validate=True)
    except (binascii.Error, ValueError):
        return b""


class Line:
    def __init__(self) -> None:
        self.sent = bytearray()
        self.mic = bytearray()
        self.down = bytearray()

    def to_them(self, payload: str) -> None:
        """Audio Twilio was given to play (the voice, keypad tones). Never the person's own microphone."""
        self.sent += _bytes(payload)
        del self.sent[:-SENT_MAX]

    def cleared(self) -> None:
        self.sent.clear()

    def from_person(self, payload: str) -> bytes:
        """The person's microphone: what goes to Twilio as is, and is kept for the voice's input."""
        data = _bytes(payload)
        self.mic += data
        del self.mic[:-MIC_MAX]
        return data

    def stop_mic(self) -> None:
        self.mic.clear()

    def from_them(self, payload: str, listening: bool) -> Tuple[str, Optional[str]]:
        """A frame of the other party: (what the voice hears, base64; a 100 ms chunk for the person, or None)."""
        frame = _bytes(payload)
        n = len(frame)
        mine, self.mic[:n] = bytes(self.mic[:n]), b""
        to_voice = payload if not mine else base64.b64encode(ulaw.mix(frame, mine)).decode()
        if not listening:
            self.sent[:n] = b""
            self.down.clear()
            return to_voice, None
        ours, self.sent[:n] = bytes(self.sent[:n]), b""
        self.down += ulaw.mix(frame, ours)
        if len(self.down) < CHUNK:
            return to_voice, None
        chunk, self.down[:] = bytes(self.down), b""
        return to_voice, base64.b64encode(chunk).decode()
