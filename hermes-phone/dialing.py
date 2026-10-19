"""Phone numbers a call may go to: the US and Canada only, never premium or Caribbean-rate numbers.

Twilio's Geo Permissions should be set the same way on the account; this check is the plugin's own, so a mistake there
doesn't open international or premium calling.
"""

from __future__ import annotations

import re
from typing import Optional

#: +1 area codes that are other countries (the Caribbean and Bermuda): billed as international and a common fraud target.
FOREIGN_NANP = frozenset({
    "242", "246", "264", "268", "284", "345", "441", "473", "649", "658", "664", "721", "758", "767", "784", "809", "829",
    "849", "868", "869", "876",
})
#: Premium-rate and pay-per-call prefixes.
PREMIUM = frozenset({"900", "976"})
#: Not callable: N11 service codes (911, 411, ...).
_SERVICE = re.compile(r"^[2-9]11$")


class NumberError(ValueError):
    pass


def normalize(text: str) -> str:
    """`+1NXXNXXXXXX` from what a person or a web page writes ("(202) 555-0123", "1-202-555-0123", "+1 202 555 0123")."""
    raw = str(text or "").strip()

    if re.search(r"(ext\.?|x)\s*\d", raw, re.I):
        raise NumberError("Extensions aren't dialed: give the main number; the call can use the phone menu.")
    if re.search(r"[A-Za-z]", raw):
        raise NumberError("A phone number can't contain letters (write vanity numbers as digits).")

    digits = re.sub(r"\D", "", raw)

    if raw.startswith("+") and not digits.startswith("1"):
        raise NumberError("Only US and Canadian numbers can be called.")
    if raw.startswith("011") or raw.startswith("00"):
        raise NumberError("Only US and Canadian numbers can be called.")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        raise NumberError("That isn't a 10-digit US or Canadian number.")

    area, exchange = digits[:3], digits[3:6]

    if area[0] in "01" or exchange[0] in "01":
        raise NumberError("That isn't a valid US or Canadian number.")
    if area in FOREIGN_NANP:
        raise NumberError("That area code is outside the US and Canada (billed as international), so it can't be called.")
    if area in PREMIUM or exchange == "976":
        raise NumberError("Premium-rate numbers can't be called.")
    if _SERVICE.match(area) or _SERVICE.match(exchange):
        raise NumberError("Service codes like 911 or 411 can't be called.")

    return "+1" + digits


def try_normalize(text: str) -> Optional[str]:
    try:
        return normalize(text)
    except NumberError:
        return None


def masked(number: str) -> str:
    """What a log may show of a number: its last two digits."""
    digits = re.sub(r"\D", "", str(number or ""))

    return f"…{digits[-2:]}" if len(digits) >= 2 else "…"
