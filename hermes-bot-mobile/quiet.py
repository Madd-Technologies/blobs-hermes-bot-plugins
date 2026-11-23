"""Quiet hours and notification suppression policy for hermes-bot-mobile."""

from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    from ._storage import JsonFile, default_folder
except ImportError:  # tests import modules flat
    from _storage import JsonFile, default_folder  # type: ignore[no-redef]

HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
URGENT_KINDS = frozenset({"approval", "call_question"})

DEFAULT_QUIET = {
    "enabled": False,
    "start": "22:00",
    "end": "07:00",
    "timezone": "UTC",
    "allow_urgent": True,
    "days": [0, 1, 2, 3, 4, 5, 6],
    "muted_until": None,
}


def _minutes(hhmm: str) -> int:
    match = HHMM.match(hhmm)
    if not match:
        raise ValueError("time must be HH:MM (24-hour)")
    return int(match.group(1)) * 60 + int(match.group(2))


def _normalize(raw: dict) -> dict:
    days_raw = raw.get("days")
    if isinstance(days_raw, list):
        days = sorted({int(d) for d in days_raw if isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= 6})
    else:
        days = list(DEFAULT_QUIET["days"])

    tz_raw = str(raw.get("timezone") or "UTC")
    try:
        ZoneInfo(tz_raw)
    except ZoneInfoNotFoundError:
        tz_raw = "UTC"

    start = str(raw.get("start") or "22:00")
    end = str(raw.get("end") or "07:00")
    if not HHMM.match(start):
        start = "22:00"
    if not HHMM.match(end):
        end = "07:00"

    muted = raw.get("muted_until")
    muted_until = float(muted) if isinstance(muted, (int, float)) and not isinstance(muted, bool) else None

    return {
        "enabled": bool(raw.get("enabled", False)),
        "start": start,
        "end": end,
        "timezone": tz_raw,
        "allow_urgent": bool(raw.get("allow_urgent", True)),
        "days": days,
        "muted_until": muted_until,
    }


class QuietHours:
    def __init__(self, folder: Path | None = None, clock=time.time):
        self._clock = clock
        self.folder = Path(folder) if folder is not None else default_folder()
        self._file = JsonFile(self.folder / "quiet_hours.json", dict)

    def get(self) -> dict:
        return _normalize(self._file.read())

    def set(self, patch: dict) -> dict:
        if not isinstance(patch, dict):
            raise ValueError("quiet hours patch must be an object")

        merged = {**self.get(), **patch}

        if "start" in patch:
            _minutes(str(patch["start"]))
            merged["start"] = str(patch["start"])

        if "end" in patch:
            _minutes(str(patch["end"]))
            merged["end"] = str(patch["end"])

        if "timezone" in patch:
            tz = str(patch["timezone"] or "").strip()
            try:
                ZoneInfo(tz)
            except ZoneInfoNotFoundError as failure:
                raise ValueError(f"unknown timezone: {tz!r}") from failure
            merged["timezone"] = tz

        if "days" in patch:
            days = patch["days"]
            if not isinstance(days, list) or any(
                not isinstance(d, int) or isinstance(d, bool) or not 0 <= d <= 6 for d in days
            ):
                raise ValueError("days must be a list of integers 0..6")
            merged["days"] = sorted(set(days))

        if "muted_until" in patch:
            muted = patch["muted_until"]
            if muted is not None and (isinstance(muted, bool) or not isinstance(muted, (int, float))):
                raise ValueError("muted_until must be epoch seconds or null")

        if "enabled" in patch and not isinstance(patch["enabled"], bool):
            raise ValueError("enabled must be boolean")

        if "allow_urgent" in patch and not isinstance(patch["allow_urgent"], bool):
            raise ValueError("allow_urgent must be boolean")

        clean = _normalize(merged)

        def save(data: dict) -> dict:
            data.clear()
            data.update(clean)
            return dict(clean)

        return self._file.update(save)

    def is_quiet(self, now: float | None = None) -> bool:
        ts = float(now if now is not None else self._clock())
        cfg = self.get()

        muted_until = cfg.get("muted_until")
        if muted_until is not None and float(muted_until) > ts:
            return True

        if not cfg.get("enabled"):
            return False

        days = set(cfg["days"])
        if not days:
            return False

        tz = ZoneInfo(cfg["timezone"])
        local = datetime.fromtimestamp(ts, tz=tz)
        cur = local.hour * 60 + local.minute
        start = _minutes(cfg["start"])
        end = _minutes(cfg["end"])
        weekday = local.weekday()

        if start == end:
            return weekday in days

        if start < end:
            return start <= cur < end and weekday in days

        # Overnight window (e.g. 22:00 -> 07:00): post-midnight hours belong to the previous weekday's schedule.
        if cur >= start:
            return weekday in days
        if cur < end:
            return ((weekday - 1) % 7) in days

        return False

    def should_suppress(self, kind: str, now: float | None = None) -> bool:
        if kind == "test":
            return False

        if not self.is_quiet(now):
            return False

        cfg = self.get()
        if cfg.get("allow_urgent", True) and kind in URGENT_KINDS:
            return False

        return True
