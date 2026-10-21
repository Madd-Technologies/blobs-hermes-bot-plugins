"""Approvals bound to exactly what the person saw: a digest of a draft, and the record of the person approving it.

A draft field with `approve` is something the person approves before a bot acts on it (an email to send, say). Its
digest is SHA-256 over a canonical JSON document of everything that would go out: the draft's text and format, the
fields it covers (who it is to, the subject...) and the manifest of its attachments, bound to its record. The app
computes the same digest from what it drew and only lets the person approve when the two match; pressing the button
sends that digest, the dashboard refuses it if the draft changed since, and stores the approval.

A plugin that does the sending (an email plugin) checks before it sends: build the digest of what it is about to send
with `digest_of(...)` and compare it with the approved one from `latest(...)`; then report what happened with `report(...)`
so the person sees "Sent exactly what you approved", or that nothing went out.

Standard library only, so another plugin can load this file by path:

    spec = importlib.util.spec_from_file_location("hermes_dashboard_approvals", "<plugins>/hermes-dashboard/approvals.py")

The canonical form: `json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":"))`, UTF-8, of
`{"v": 1, "collection", "record", "draft", "format", "text", "covers": {key: value}, "attachments": [{"name", "ref",
"mime", "size", "sha256"}]}`, with null for anything missing.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

OUTCOMES = ("sent", "refused")

TABLE = """
CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY AUTOINCREMENT, record TEXT NOT NULL, draft TEXT NOT NULL, action TEXT, digest TEXT NOT NULL,
  at INTEGER NOT NULL, outcome TEXT, outcome_digest TEXT, outcome_at INTEGER, detail TEXT
);
CREATE INDEX IF NOT EXISTS approvals_record ON approvals (record, draft, id);
"""


def canonical(doc: dict) -> bytes:
    return json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _manifest(attachments) -> list[dict]:
    items = []

    for item in attachments or []:
        item = item if isinstance(item, dict) else {}
        items.append({"name": item.get("name"), "ref": item.get("path") or item.get("url"), "mime": item.get("mime"), "size": item.get("size"), "sha256": item.get("sha256")})

    return items


def digest_of(collection: str, record: str, draft: str, fmt: str, text: str, covers: dict | None = None, attachments: list | None = None) -> str:
    """The digest of one draft as it would go out (hex SHA-256)."""
    doc = {
        "v": 1,
        "collection": collection,
        "record": record,
        "draft": draft,
        "format": fmt or "plain",
        "text": text or "",
        "covers": {key: (covers or {}).get(key) for key in sorted(covers or {})},
        "attachments": _manifest(attachments),
    }

    return hashlib.sha256(canonical(doc)).hexdigest()


def covered(field: dict) -> list[str]:
    """The fields a draft's approval covers: its `covers` and the field naming who it is for."""
    keys = list(field.get("covers") or [])

    if field.get("to") and field["to"] not in keys:
        keys.append(field["to"])

    return sorted(keys)


def record_digest(collection: dict, record: dict, draft_key: str) -> str:
    """The digest of a record's draft as it is stored now."""
    field = next(item for item in collection["fields"] if item["key"] == draft_key)
    values = record.get("values") or {}

    return digest_of(
        collection["key"],
        record["id"],
        draft_key,
        field.get("format") or "plain",
        str(values.get(draft_key) or ""),
        {key: values.get(key) for key in covered(field)},
        values.get(field["attachments"]) if field.get("attachments") else None,
    )


# -- reading and reporting from another plugin -----------------------------------------------------------------------


def default_db() -> Path:
    try:
        from hermes_constants import get_default_hermes_root

        root = Path(get_default_hermes_root())
    except Exception:
        root = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")

        if root.parent.name == "profiles":
            root = root.parent.parent

    return root / "hermes-dashboard" / "dashboard.db"


def _connect(db_path=None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path or default_db()), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000")

    return conn


def _row(row) -> dict:
    approval = {"digest": row["digest"], "at": row["at"], "action": row["action"], "draft": row["draft"]}

    if row["outcome"]:
        approval["outcome"] = {"outcome": row["outcome"], "digest": row["outcome_digest"], "at": row["outcome_at"], "detail": row["detail"]}

    return approval


def latest(record_id: str, draft: str | None = None, db_path=None) -> dict | None:
    """The person's latest approval of a record's draft: `{digest, at, action, draft, outcome?}`, or None."""
    conn = _connect(db_path)

    try:
        query = "SELECT * FROM approvals WHERE record = ?" + (" AND draft = ?" if draft else "") + " ORDER BY id DESC LIMIT 1"
        row = conn.execute(query, (record_id, draft) if draft else (record_id,)).fetchone()
    except sqlite3.OperationalError:  # no approvals yet on this dashboard
        return None
    finally:
        conn.close()

    return _row(row) if row else None


def report(record_id: str, draft: str, digest: str, outcome: str, detail: str | None = None, db_path=None, clock=time.time) -> bool:
    """What a sending plugin did with the latest approval: `sent` (with the digest of what went out) or `refused` (and
    why). False when the person approved nothing on that draft."""
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome is one of {', '.join(OUTCOMES)}")

    conn = _connect(db_path)

    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT id FROM approvals WHERE record = ? AND draft = ? ORDER BY id DESC LIMIT 1", (record_id, draft)).fetchone()

        if row is None:
            conn.execute("ROLLBACK")
            return False

        now = int(clock())
        conn.execute("UPDATE approvals SET outcome = ?, outcome_digest = ?, outcome_at = ?, detail = ? WHERE id = ?", (outcome, str(digest or "")[:64], now, str(detail or "")[:500] or None, row["id"]))
        conn.execute(
            "INSERT INTO changes (record, at, by_kind, by_name, field, old, new) VALUES (?, ?, 'script', 'sender', ?, NULL, ?)",
            (record_id, now, "_sent" if outcome == "sent" else "_refused", json.dumps(str(detail or "") or None)),
        )
        # The send is the end of the button's job.
        conn.execute("UPDATE records SET running = NULL, updated_at = ? WHERE id = ?", (now, record_id))
        conn.execute("COMMIT")

        return True
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")

        raise
    finally:
        conn.close()
