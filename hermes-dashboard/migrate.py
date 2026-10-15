"""Moves hermes-dashboard 0.1's snapshot panels (`<profile>.json`) into the database, once per bot.

A list or checklist panel becomes a collection of its own (fields for what 0.1 items had, and a status of Open / Done) with
its items as records and a view showing them; what the person marked done is closed; a metrics panel becomes a `stat` view
and a summary panel a `summary` view. The JSON files are left where they are, as a backup.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

try:
    from . import store as S
except ImportError:  # the tests import the modules flat
    import store as S  # type: ignore[no-redef]

log = logging.getLogger("hermes-dashboard")

FIELDS = [
    {"key": "title", "label": "Title", "type": "text", "required": True},
    {"key": "detail", "label": "Detail", "type": "text"},
    {"key": "body", "label": "Notes from the bot", "type": "longtext"},
    {"key": "status", "label": "Status", "type": "status", "stages": [{"name": "Open"}, {"name": "Done", "done": True}]},
    {"key": "needs_you", "label": "Needs you", "type": "checkbox"},
    {"key": "at", "label": "When", "type": "datetime"},
    {"key": "due", "label": "Due", "type": "date"},
    {"key": "link", "label": "Link", "type": "link"},
    {"key": "phone", "label": "Phone", "type": "phone"},
    {"key": "email", "label": "Email", "type": "email"},
    {"key": "page", "label": "Page", "type": "url"},
]


def _values(item: dict) -> dict:
    values = {key: item.get(key) for key in ("title", "detail", "body", "at", "due", "link") if item.get(key) not in (None, "")}

    if item.get("needs_you"):
        values["needs_you"] = True

    url = str((item.get("action") or {}).get("url") or "")

    if url.lower().startswith("tel:"):
        values["phone"] = url[4:]
    elif url.lower().startswith("mailto:"):
        values["email"] = url[7:].split("?")[0]
    elif url.startswith("https://"):
        values["page"] = url

    return values


def migrate_profile(store: "S.Store", profile: str, data: dict) -> int:
    """Moves one bot's 0.1 panels into the database. Returns how many panels moved."""
    actor = S.Actor.bot(profile)
    panels = data.get("panels") if isinstance(data.get("panels"), dict) else {}
    done = data.get("done") if isinstance(data.get("done"), dict) else {}
    moved = 0

    for panel in sorted(panels.values(), key=lambda entry: (entry.get("order", 0), entry.get("title", ""))):
        pid = re.sub(r"[^a-z0-9_-]", "-", str(panel.get("id") or "panel").lower())[:60] or "panel"
        title = str(panel.get("title") or pid)[:40]
        kind = panel.get("kind")

        try:
            if kind in ("list", "checklist"):
                store.create_collection(actor, {"id": pid, "title": title, "fields": FIELDS})
                items = [item for item in panel.get("items") or [] if isinstance(item, dict) and item.get("title")]

                for start in range(0, len(items), S.MAX_BATCH):
                    batch = items[start : start + S.MAX_BATCH]
                    results = store.upsert(actor, pid, [{"source_key": item.get("id"), "values": _values(item)} for item in batch])

                    for item, result in zip(batch, results):
                        marked = (done.get(panel.get("id")) or {}).get(item.get("id"))

                        if marked:
                            closed = store.set_closed(actor, result["record"]["id"], True)
                            _backdate(store, closed["record"]["id"], int(marked))

                store.set_view(
                    actor,
                    {
                        "id": pid,
                        "title": title,
                        "kind": kind,
                        "collection": pid,
                        "config": {
                            "show": {"title": "title", "subtitle": "detail", "meta": "due" if kind == "checklist" else "at"},
                            "needs_you": {"field": "needs_you", "op": "eq", "value": True},
                            "sort": [{"field": "due", "dir": "asc"}] if kind == "checklist" else [{"field": "at", "dir": "desc"}],
                        },
                    },
                )
            elif kind == "metrics":
                metrics = [metric for metric in panel.get("metrics") or [] if isinstance(metric, dict)][:6]

                if metrics:
                    store.set_view(actor, {"id": pid, "title": title, "kind": "stat", "config": {"metrics": metrics}})
            elif kind == "summary" and panel.get("summary"):
                store.set_view(actor, {"id": pid, "title": title, "kind": "summary", "config": {"text": panel["summary"]}})
            else:
                continue

            moved += 1
        except (S.DashboardError, S.NotFound) as error:
            log.warning("hermes-dashboard: panel %s/%s was not moved: %s", profile, pid, error)

    return moved


def _backdate(store: "S.Store", record_id: str, closed_at: int) -> None:
    """A moved item keeps when the person marked it done, not when it moved."""
    with store._db(write=True) as conn:
        conn.execute("UPDATE records SET closed_at = ? WHERE id = ?", (closed_at, record_id))


def migrate_folder(store: "S.Store") -> list[str]:
    """Moves every bot's 0.1 file that has not moved yet. Returns the bots moved this time."""
    folder: Path = store.folder

    try:
        files = sorted(folder.glob("*.json"))
    except OSError:
        return []

    with store._db() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = 'migrated_v1'").fetchone()
        done = set(json.loads(row["value"])) if row else set()

    moved = []

    for path in files:
        profile = path.stem

        if profile in done or not S.PROFILE.match(profile):
            continue

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue

        migrate_profile(store, profile, data if isinstance(data, dict) else {})
        done.add(profile)
        moved.append(profile)

        with store._db(write=True) as conn:
            conn.execute("INSERT INTO meta (key, value) VALUES ('migrated_v1', ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value", (json.dumps(sorted(done)),))

    return moved
