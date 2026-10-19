"""G.711 μ-law at 8 kHz, the audio Twilio Media Streams and Grok's `audio/pcmu` both speak, and keypad tones made from it.

Pure Python because `audioop` is gone from Python 3.13+. Only small things are encoded here (keypad tones, silence); the
call's own audio passes between Twilio and Grok untouched.
"""

from __future__ import annotations

import base64
import math
from typing import Iterable, List

RATE = 8000
#: Twilio and Grok both move 20 ms frames: 160 μ-law bytes.
FRAME = 160
SILENCE = 0xFF

_BIAS, _CLIP = 0x84, 32635

_ROWS, _COLS = (697, 770, 852, 941), (1209, 1336, 1477, 1633)
_KEYS = ("123A", "456B", "789C", "*0#D")
KEYPAD = "0123456789*#"
#: Long enough for slow phone menus (Pipecat uses the same over Twilio).
TONE_S, GAP_S = 0.3, 0.2


def encode_sample(sample: int) -> int:
    sign = 0x80 if sample < 0 else 0
    sample = min(abs(int(sample)), _CLIP) + _BIAS
    exponent = min(7, max(0, (sample >> 7).bit_length() - 1))
    mantissa = (sample >> (exponent + 3)) & 0x0F

    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def decode_sample(byte: int) -> int:
    byte = ~byte & 0xFF
    sign, exponent, mantissa = byte & 0x80, (byte >> 4) & 7, byte & 0x0F
    sample = (((mantissa << 3) + _BIAS) << exponent) - _BIAS

    return -sample if sign else sample


def encode(samples: Iterable[float]) -> bytes:
    return bytes(encode_sample(int(s)) for s in samples)


def decode(data: bytes) -> List[int]:
    return [decode_sample(b) for b in data]


def frames(data: bytes) -> List[str]:
    """Base64 frames of 20 ms, padded with silence, as a Twilio `media` message carries them."""
    if len(data) % FRAME:
        data += bytes([SILENCE]) * (FRAME - len(data) % FRAME)

    return [base64.b64encode(data[i : i + FRAME]).decode() for i in range(0, len(data), FRAME)]


def level(payload: str) -> float:
    """Rough loudness of one base64 μ-law frame (mean absolute sample), for telling speech from quiet."""
    data = base64.b64decode(payload)

    return sum(abs(decode_sample(b)) for b in data) / max(1, len(data))


def seconds(payload: str) -> float:
    """How much audio one base64 μ-law frame holds."""
    return (len(payload) * 3 // 4 - payload[-2:].count("=")) / RATE if payload else 0.0


def tone(digit: str, amplitude: int = 9000) -> bytes:
    """One keypad digit as μ-law: TONE_S of its two frequencies, then GAP_S of silence."""
    row = next(i for i, keys in enumerate(_KEYS) if digit in keys)
    low, high = _ROWS[row], _COLS[_KEYS[row].index(digit)]
    on = [amplitude * (math.sin(2 * math.pi * low * n / RATE) + math.sin(2 * math.pi * high * n / RATE)) for n in range(int(RATE * TONE_S))]

    return encode(on) + bytes([SILENCE]) * int(RATE * GAP_S)


_TONES: dict = {}


def keypad(digits: str) -> List[str]:
    """Twilio media frames that press `digits` (only 0-9, * and #) on whatever phone menu is listening."""
    out = b""

    for digit in digits:
        if digit not in KEYPAD:
            raise ValueError(f"not a keypad key: {digit!r}")
        if digit not in _TONES:
            _TONES[digit] = tone(digit)
        out += _TONES[digit]

    return frames(out)


# A plain Goertzel detector, used by the tests to prove the tones survive μ-law.


def _goertzel(block, freq):
    k = 2 * math.cos(2 * math.pi * freq / RATE)
    s1 = s2 = 0.0

    for x in block:
        s1, s2 = x + k * s1 - s2, s1

    return s1 * s1 + s2 * s2 - k * s1 * s2


def detect(samples: List[int], block: int = 205) -> str:
    found, last = [], None

    for i in range(0, len(samples) - block, block):
        part = samples[i : i + block]

        if sum(x * x for x in part) / block < 1e5:
            last = None
            continue

        rows = [_goertzel(part, f) for f in _ROWS]
        cols = [_goertzel(part, f) for f in _COLS]
        r, c = max(range(4), key=rows.__getitem__), max(range(4), key=cols.__getitem__)
        clear = all(rows[r] > 6.3 * p for j, p in enumerate(rows) if j != r) and all(cols[c] > 6.3 * p for j, p in enumerate(cols) if j != c)
        digit = _KEYS[r][c] if clear and abs(10 * math.log10(rows[r] / cols[c])) < 8 else None

        if digit and digit != last:
            found.append(digit)
        last = digit

    return "".join(found)
