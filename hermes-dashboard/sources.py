"""Live sources: panels that read their data when someone looks, with no records, no job and no model.

A view can be bound to a source instead of a collection: a sqlite query on a file in the bot's own Hermes folder, a JSON
URL, a calendar (.ics file or URL), or a script in the bot's own `scripts/` folder that prints JSON. The source's rows
are typed like a collection's (the types are inferred once, when the bot sets the view, and kept), so every panel kind
that draws records (list, table, number, chart) draws them unchanged. Reads are cached for a short while and only happen
when the app asks; a source that fails keeps showing its last good rows, marked with when they were read.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

KINDS = ("sqlite", "json", "ics", "script")
MAX_ROWS = 200
MAX_BYTES = 512 * 1024
TIMEOUT_S = 10
DEFAULT_TTL_S = 60
MIN_TTL_S = 15
MAX_TTL_S = 3600
VIEW_KINDS = ("list", "table", "number", "chart")
KEY = re.compile(r"[^a-z0-9_]+")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ISO_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$")


class SourceError(ValueError):
    """A source that can't be used or read; the message says what to change."""


# -- where a bot's files are ---------------------------------------------------------------------------------------


def bot_home(root: Path, profile: str) -> Path:
    return root if profile == "default" else root / "profiles" / profile


def plugin_installed(root: Path, profile: str) -> bool | None:
    """Whether this bot has the dashboard plugin (Hermes loads plugins per profile): its folder in the bot's plugins and the
    bot's own `plugins.enabled` naming it. None for a bot that doesn't exist, or a config that can't be read."""
    home = bot_home(root, profile)

    if not home.is_dir():
        return None

    if not (home / "plugins" / "hermes-dashboard").is_dir():
        return False

    try:
        import yaml

        config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return False
    except Exception:
        return None

    enabled = (config.get("plugins") or {}).get("enabled") if isinstance(config, dict) else None

    return isinstance(enabled, list) and "hermes-dashboard" in enabled


def _inside(path: Path, folder: Path, what: str) -> Path:
    resolved = path.resolve()

    try:
        resolved.relative_to(folder.resolve())
    except ValueError:
        raise SourceError(f"{what} must be inside {folder}") from None

    return resolved


# -- checking what a bot asks for ----------------------------------------------------------------------------------


def clean_source(raw, home: Path) -> dict:
    """A source definition, checked: `{kind, ...}` plus `ttl` (seconds between reads, default 60)."""
    if not isinstance(raw, dict):
        raise SourceError("source is {kind: sqlite|json|ics|script, ...}")

    kind = str(raw.get("kind") or "").strip().lower()

    if kind not in KINDS:
        raise SourceError(f"source kind must be one of {', '.join(KINDS)}")

    ttl = max(MIN_TTL_S, min(int(raw.get("ttl") or DEFAULT_TTL_S), MAX_TTL_S))
    source: dict = {"kind": kind, "ttl": ttl}

    if kind == "sqlite":
        path = str(raw.get("path") or "").strip()
        query = str(raw.get("query") or "").strip().rstrip(";")

        if not path or not query:
            raise SourceError("a sqlite source needs path (a file in your Hermes folder) and query (one SELECT)")

        if not re.match(r"(?is)^\s*(select|with)\b", query) or ";" in query:
            raise SourceError("a sqlite source's query is a single SELECT")

        _inside(home / path if not Path(path).is_absolute() else Path(path), home, "a sqlite source's path")
        source.update(path=path, query=query[:4000])

    elif kind in ("json", "ics"):
        url = str(raw.get("url") or "").strip()
        path = str(raw.get("path") or "").strip()

        if url:
            if not re.match(r"^https?://[^\s]+$", url):
                raise SourceError("a source url is http:// or https://")

            source["url"] = url[:1000]
        elif kind == "ics" and path:
            _inside(home / path if not Path(path).is_absolute() else Path(path), home, "a calendar file")
            source["path"] = path
        else:
            raise SourceError("a json source needs url; a calendar needs url or path")

        if kind == "json" and raw.get("rows"):
            source["rows"] = str(raw["rows"])[:200]

        if kind == "ics":
            source["days"] = max(1, min(int(raw.get("days") or 14), 366))

    else:
        script = str(raw.get("script") or "").strip()

        if not script:
            raise SourceError("a script source needs script: a file in your scripts/ folder that prints JSON")

        _inside(home / "scripts" / script, home / "scripts", "a source script")
        source["script"] = script

    return source


# -- reading -------------------------------------------------------------------------------------------------------


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "hermes-dashboard"})

    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # noqa: S310 (http/https checked above)
        data = response.read(MAX_BYTES + 1)

    if len(data) > MAX_BYTES:
        raise SourceError(f"the source answered more than {MAX_BYTES // 1024} KB")

    return data


def _read_sqlite(source: dict, home: Path) -> list[dict]:
    path = _inside(home / source["path"] if not Path(source["path"]).is_absolute() else Path(source["path"]), home, "a sqlite source's path")

    if not path.is_file():
        raise SourceError(f"no file at {source['path']}")

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    deadline = time.monotonic() + TIMEOUT_S

    def authorizer(action, *_args):
        # Read only: a SELECT, its reads and functions; everything else is refused.
        return sqlite3.SQLITE_OK if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION) else sqlite3.SQLITE_DENY

    try:
        conn.set_authorizer(authorizer)
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
        cursor = conn.execute(source["query"])
        names = [column[0] for column in cursor.description or []]

        return [dict(zip(names, row)) for row in cursor.fetchmany(MAX_ROWS)]
    except sqlite3.Error as error:
        raise SourceError(f"the query failed: {error}") from None
    finally:
        conn.close()


def _read_json(source: dict) -> list[dict]:
    try:
        data = json.loads(_fetch(source["url"]))
    except (ValueError, OSError) as error:
        raise SourceError(f"the url didn't answer JSON: {error}") from None

    for part in (source.get("rows") or "").split("."):
        if part:
            data = data.get(part) if isinstance(data, dict) else None

    if isinstance(data, dict):
        data = [data]

    if not isinstance(data, list):
        raise SourceError("the JSON has no list of rows there; set rows to its path, like data.items")

    return [row if isinstance(row, dict) else {"value": row} for row in data[:MAX_ROWS]]


def _ics_time(value: str, params: str) -> tuple[str, bool]:
    """An event time as a local ISO datetime (all-day events at midnight), and whether it is all-day."""
    value = value.strip()

    if "VALUE=DATE" in params or re.match(r"^\d{8}$", value):
        return datetime(int(value[:4]), int(value[4:6]), int(value[6:8])).isoformat(), True

    stamp = datetime.strptime(value.rstrip("Z")[:15], "%Y%m%dT%H%M%S")

    if value.endswith("Z"):
        stamp = stamp.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)

    return stamp.isoformat(), False


def _read_ics(source: dict, home: Path, now: float) -> list[dict]:
    if source.get("url"):
        text = _fetch(source["url"]).decode("utf-8", "replace")
    else:
        path = _inside(home / source["path"] if not Path(source["path"]).is_absolute() else Path(source["path"]), home, "a calendar file")
        text = path.read_text("utf-8", "replace")[:MAX_BYTES]

    # Unfold continuation lines (RFC 5545 §3.1), then read each event's few fields that a panel needs.
    lines = re.sub(r"\r?\n[ \t]", "", text).splitlines()
    events, event = [], None

    for line in lines:
        if line == "BEGIN:VEVENT":
            event = {}
        elif line == "END:VEVENT" and event is not None:
            events.append(event)
            event = None
        elif event is not None and ":" in line:
            head, _, value = line.partition(":")
            name, _, params = head.partition(";")
            name = name.upper()

            if name in ("DTSTART", "DTEND"):
                try:
                    stamp, all_day = _ics_time(value, params)
                    event["start" if name == "DTSTART" else "end"] = stamp

                    if name == "DTSTART":
                        event["all_day"] = all_day
                except ValueError:
                    pass
            elif name in ("SUMMARY", "LOCATION", "URL", "DESCRIPTION"):
                event[{"SUMMARY": "title"}.get(name, name.lower())] = value.replace("\\n", "\n").replace("\\,", ",").replace("\\;", ";")[:500]

    horizon = now + source.get("days", 14) * 86_400
    today = datetime.combine(date.fromtimestamp(now), datetime.min.time()).timestamp()

    def when(event) -> float:
        return datetime.fromisoformat(event["start"]).timestamp()

    # Still to come (an all-day event counts all day; a timed one until an hour after it starts), within `days`.
    upcoming = [event for event in events if event.get("start") and (when(event) >= (today if event.get("all_day") else now - 3600)) and when(event) <= horizon]

    return sorted(upcoming, key=when)[:MAX_ROWS]


def _read_script(source: dict, home: Path) -> list[dict]:
    scripts = home / "scripts"
    path = _inside(scripts / source["script"], scripts, "a source script")

    if not path.is_file():
        raise SourceError(f"no script {source['script']} in {scripts}")

    argv = ["bash", str(path)] if path.suffix in (".sh", ".bash") else [sys.executable, str(path)]
    env = {**os.environ, "HERMES_HOME": str(home)}

    try:
        done = subprocess.run(argv, cwd=scripts, env=env, capture_output=True, timeout=TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        raise SourceError(f"the script took longer than {TIMEOUT_S} s") from None

    if done.returncode != 0:
        raise SourceError(f"the script failed: {done.stderr.decode('utf-8', 'replace').strip()[-300:]}")

    try:
        data = json.loads(done.stdout[:MAX_BYTES] or b"[]")
    except ValueError:
        raise SourceError("the script must print JSON: a list of objects, or {rows: [...]}") from None

    data = data.get("rows", [data]) if isinstance(data, dict) else data

    return [row if isinstance(row, dict) else {"value": row} for row in data[:MAX_ROWS]] if isinstance(data, list) else []


def read(source: dict, home: Path, now: float | None = None) -> list[dict]:
    """The source's rows as plain dicts (keys normalised to lower_snake, values as the source gave them)."""
    now = time.time() if now is None else now
    kind = source["kind"]
    rows = _read_sqlite(source, home) if kind == "sqlite" else _read_json(source) if kind == "json" else _read_ics(source, home, now) if kind == "ics" else _read_script(source, home)

    return [{_normal(key): value for key, value in row.items()} for row in rows]


def _normal(key) -> str:
    key = KEY.sub("_", str(key).strip().lower()).strip("_") or "value"

    return key if key[0].isalpha() else f"f_{key}"[:40]


# -- types, and rows as records ------------------------------------------------------------------------------------


def _infer(values: list) -> str:
    present = [value for value in values if value not in (None, "")]

    if not present:
        return "text"

    if all(isinstance(value, bool) for value in present):
        return "checkbox"

    if all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in present):
        return "number"

    if all(isinstance(value, str) and ISO_DATE.match(value) for value in present):
        return "date"

    if all(isinstance(value, str) and ISO_DATETIME.match(value) for value in present):
        return "datetime"

    if all(isinstance(value, str) and value.startswith(("https://", "http://")) for value in present):
        return "url"

    return "text"


def infer_fields(rows: list[dict], declared: dict | None = None) -> list[dict]:
    """Fields for a source's rows: the types the bot declared (`{key: type}`), else inferred from the values."""
    declared = {_normal(key): str(kind) for key, kind in (declared or {}).items()}
    keys: list[str] = []

    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)

    for key in declared:
        if key not in keys:
            keys.append(key)

    return [{"key": key, "label": key.replace("_", " ").capitalize(), "type": declared.get(key) or _infer([row.get(key) for row in rows])} for key in keys[:60]]


def _value(field: dict, value):
    if value in (None, ""):
        return None

    kind = field["type"]

    if kind == "datetime" and isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None

    if kind in ("number", "money", "percent", "rating") and not isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    if kind == "checkbox":
        return bool(value)

    return value if isinstance(value, (int, float, bool)) else str(value)[:1000]


def as_records(rows: list[dict], fields: list[dict], now: float) -> list[dict]:
    """Source rows shaped like records, so the view engine computes them; ids start with `live:` (not openable)."""
    stamp = int(now)

    return [
        {
            "id": f"live:{index}",
            "values": {field["key"]: _value(field, row.get(field["key"])) for field in fields},
            "person_fields": {},
            "created_at": stamp,
            "updated_at": stamp,
            "closed_at": None,
            "running": None,
            "muted_until": None,
        }
        for index, row in enumerate(rows)
    ]


# -- the cache -----------------------------------------------------------------------------------------------------

_cache: dict[str, dict] = {}
_lock = threading.Lock()


def cached(key: str, source: dict, home: Path, now: float, fresh: bool = False) -> dict:
    """`{rows, as_of, error}` for a view's source: the last read while it is younger than the source's ttl, else a new
    read. A failed read keeps the last good rows and reports the error."""
    with _lock:
        entry = _cache.get(key)

        if entry and not fresh and entry["source"] == source and now - entry["checked"] < source["ttl"]:
            return entry

    try:
        rows, as_of, error = read(source, home, now), int(now), None
    except (SourceError, OSError, ValueError) as failure:
        rows, as_of, error = (entry["rows"], entry["as_of"], str(failure)) if entry and entry["source"] == source else ([], None, str(failure))

    entry = {"source": source, "rows": rows, "as_of": as_of, "error": error, "checked": now}

    with _lock:
        _cache[key] = entry

    return entry


def peek(key: str) -> dict:
    """The last read of a view's source without reading it again (empty when it was never read)."""
    with _lock:
        return _cache.get(key) or {"rows": [], "as_of": None, "error": None}


def forget(key: str) -> None:
    with _lock:
        _cache.pop(key, None)
