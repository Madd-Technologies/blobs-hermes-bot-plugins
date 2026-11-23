"""Morning briefing and 'while you were away' activity aggregation for hermes-bot-mobile."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

try:
    from ._storage import JsonFile, JsonLog, default_folder, hermes_root
except ImportError:  # tests import modules flat
    from _storage import JsonFile, JsonLog, default_folder, hermes_root  # type: ignore[no-redef]


class BriefingStore:
    def __init__(self, folder: Path | None = None, clock=time.time, root: Path | None = None):
        self._clock = clock
        self._root = Path(root) if root is not None else None
        self.folder = Path(folder) if folder is not None else default_folder(self._root)
        self._acks = JsonFile(self.folder / "briefing.json", dict)
        self._log = JsonLog(self.folder / "activity.jsonl")

    def ack(self, client: str = "default", at: float | None = None) -> dict:
        key = str(client or "default").strip()[:64] or "default"
        ts = float(at if at is not None else self._clock())

        def change(data: dict) -> float:
            data[key] = ts
            return ts

        saved = self._acks.update(change)
        return {"ok": True, "since": saved}

    def last_ack(self, client: str = "default") -> float:
        key = str(client or "default").strip()[:64] or "default"
        raw = self._acks.read().get(key)

        if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
            return float(raw)

        return float(self._clock()) - 86_400.0

    def record_event(
        self,
        kind: str,
        profile: str,
        summary: str,
        *,
        session: str = "",
        url: str = "",
        status: str = "ok",
        at: float | None = None,
    ) -> dict:
        now = float(at if at is not None else self._clock())
        entry = {
            "at": now,
            "kind": str(kind or "event"),
            "profile": str(profile or "default"),
            "summary": str(summary or "")[:240],
            "status": str(status or "ok"),
        }
        if session:
            entry["session"] = str(session)
        if url:
            entry["url"] = str(url)

        return self._log.append(entry, now=now)

    def _dashboard_events(self, cutoff: float, profiles: set[str] | None) -> list[dict]:
        base = self._root if self._root is not None else hermes_root()
        candidates = [
            base / "hermes-dashboard" / "dashboard.db",
            base / "dashboard.db",
        ]
        db_path = next((path for path in candidates if path.is_file()), None)

        if db_path is None:
            return []

        events: list[dict] = []

        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row

            try:
                rows = conn.execute(
                    "SELECT r.id, r.collection, r.updated_at, r.closed_at, c.owner "
                    "FROM records r LEFT JOIN collections c ON c.key = r.collection "
                    "WHERE r.updated_at >= ? ORDER BY r.updated_at DESC LIMIT 100",
                    (cutoff,),
                ).fetchall()
            finally:
                conn.close()

            for row in rows:
                owner = str(row["owner"] or "")
                profile = owner.split(":", 1)[1] if owner.startswith("bot:") else "default"
                if profiles is not None and profile not in profiles:
                    continue
                closed = bool(row["closed_at"])
                events.append(
                    {
                        "at": float(row["updated_at"]),
                        "kind": "dashboard",
                        "profile": profile,
                        "summary": f"Updated record {row['id']}" if not closed else f"Closed record {row['id']}",
                        "status": "done" if closed else "ok",
                    }
                )
        except Exception:
            return []

        return events

    def activity(
        self,
        since: float | None = None,
        *,
        client: str = "default",
        limit: int = 100,
        profiles: list[str] | None = None,
    ) -> dict:
        now = float(self._clock())
        cutoff = float(since) if since is not None else self.last_ack(client)
        allowed = {str(p) for p in profiles if p} if profiles is not None else None
        cap = max(1, min(int(limit), 500))

        events = [
            row
            for row in self._log.read()
            if float(row.get("at") or 0) >= cutoff and (allowed is None or row.get("profile") in allowed)
        ]
        events.extend(self._dashboard_events(cutoff, allowed))
        events.sort(key=lambda item: float(item.get("at") or 0), reverse=True)
        events = events[:cap]

        counts = {
            "total": len(events),
            "completed": 0,
            "failed": 0,
            "approvals": 0,
            "replies": 0,
            "routines": 0,
        }
        by_bot: dict[str, dict] = {}

        for item in events:
            status = str(item.get("status") or "ok").lower()
            kind = str(item.get("kind") or "")
            profile = str(item.get("profile") or "default")
            at = float(item.get("at") or 0)

            if status == "failed":
                counts["failed"] += 1
            else:
                counts["completed"] += 1

            if kind == "approval":
                counts["approvals"] += 1
            elif kind == "reply":
                counts["replies"] += 1
            elif kind == "routine":
                counts["routines"] += 1

            bucket = by_bot.setdefault(profile, {"total": 0, "completed": 0, "failed": 0, "latest_at": 0.0})
            bucket["total"] += 1
            if status == "failed":
                bucket["failed"] += 1
            else:
                bucket["completed"] += 1
            if at > bucket["latest_at"]:
                bucket["latest_at"] = at

        return {
            "since": cutoff,
            "now": now,
            "counts": counts,
            "by_bot": by_bot,
            "events": events,
        }
