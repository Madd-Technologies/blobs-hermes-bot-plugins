"""Read-only SQLite usage telemetry across bots for hermes-bot-mobile."""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from ._storage import hermes_root
except ImportError:  # tests import modules flat
    from _storage import hermes_root  # type: ignore[no-redef]


def _epoch_s(value) -> float:
    if value is None or isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        num = float(value)
        return num / 1000.0 if num > 10_000_000_000 else num
    if isinstance(value, str):
        stripped = value.strip()
        try:
            num = float(stripped)
            return num / 1000.0 if num > 10_000_000_000 else num
        except ValueError:
            try:
                parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
                return parsed.timestamp()
            except ValueError:
                return 0.0
    return 0.0


def _discover_dbs(root: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    default_db = root / "state.db"
    if default_db.is_file():
        found["default"] = default_db

    profiles_dir = root / "profiles"
    if profiles_dir.is_dir():
        for child in sorted(profiles_dir.iterdir(), key=lambda p: p.name):
            candidate = child / "state.db"
            if child.is_dir() and candidate.is_file():
                found[child.name] = candidate

    return found


def _table_cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def summary(
    days: int = 7,
    *,
    now: float | None = None,
    root: Path | None = None,
    db_paths: dict[str, Path] | None = None,
) -> dict:
    clean_days = max(1, min(int(days), 90))
    cur_now = float(now if now is not None else time.time())
    cutoff = cur_now - clean_days * 86_400

    base_root = Path(root) if root is not None else hermes_root()
    dbs = dict(db_paths) if db_paths is not None else _discover_dbs(base_root)

    totals = {"sessions": 0, "messages": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost_usd": 0.0}
    by_bot: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    daily_map: dict[str, dict] = {}

    def day_bucket(ts: float) -> dict:
        key = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        return daily_map.setdefault(key, {"date": key, "sessions": 0, "messages": 0, "tokens": 0, "cost_usd": 0.0})

    for profile, path in sorted(dbs.items()):
        db_file = Path(path)
        if not db_file.is_file():
            continue

        bot_stats = by_bot.setdefault(
            profile,
            {"sessions": 0, "messages": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cost_usd": 0.0},
        )

        try:
            conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
        except sqlite3.Error:
            continue

        try:
            session_cols = _table_cols(conn, "sessions")
            if session_cols:
                time_col = next((c for c in ("updated_at", "started_at", "created_at") if c in session_cols), None)
                select_cols = [c for c in ("id", "model", "input_tokens", "output_tokens", "total_tokens", "cost_usd") if c in session_cols]
                if time_col:
                    select_cols.append(time_col)
                if select_cols:
                    for row in conn.execute(f"SELECT {', '.join(select_cols)} FROM sessions").fetchall():
                        ts = _epoch_s(row[time_col]) if time_col else cur_now
                        if time_col and ts < cutoff:
                            continue

                        in_tok = int(row["input_tokens"] or 0) if "input_tokens" in session_cols else 0
                        out_tok = int(row["output_tokens"] or 0) if "output_tokens" in session_cols else 0
                        tok = int(row["total_tokens"] or (in_tok + out_tok)) if "total_tokens" in session_cols else (in_tok + out_tok)
                        cost = float(row["cost_usd"] or 0.0) if "cost_usd" in session_cols else 0.0
                        model = str(row["model"] or "unknown") if "model" in session_cols else "unknown"

                        totals["sessions"] += 1
                        totals["input_tokens"] += in_tok
                        totals["output_tokens"] += out_tok
                        totals["total_tokens"] += tok
                        totals["cost_usd"] = round(totals["cost_usd"] + cost, 6)

                        bot_stats["sessions"] += 1
                        bot_stats["input_tokens"] += in_tok
                        bot_stats["output_tokens"] += out_tok
                        bot_stats["total_tokens"] += tok
                        bot_stats["cost_usd"] = round(bot_stats["cost_usd"] + cost, 6)

                        m_bucket = by_model.setdefault(model, {"sessions": 0, "tokens": 0, "cost_usd": 0.0})
                        m_bucket["sessions"] += 1
                        m_bucket["tokens"] += tok
                        m_bucket["cost_usd"] = round(m_bucket["cost_usd"] + cost, 6)

                        d_bucket = day_bucket(ts)
                        d_bucket["sessions"] += 1
                        d_bucket["tokens"] += tok
                        d_bucket["cost_usd"] = round(d_bucket["cost_usd"] + cost, 6)

            msg_cols = _table_cols(conn, "messages")
            if msg_cols:
                msg_time = next((c for c in ("created_at", "timestamp", "at") if c in msg_cols), None)
                query = f"SELECT {msg_time} FROM messages" if msg_time else "SELECT 1"
                for row in conn.execute(query).fetchall():
                    ts = _epoch_s(row[0]) if msg_time else cur_now
                    if msg_time and ts < cutoff:
                        continue
                    totals["messages"] += 1
                    bot_stats["messages"] += 1
                    day_bucket(ts)["messages"] += 1
        except sqlite3.Error:
            pass
        finally:
            conn.close()

    daily = [daily_map[k] for k in sorted(daily_map)]

    return {
        "days": clean_days,
        "since": int(cutoff),
        "now": int(cur_now),
        "totals": totals,
        "by_bot": by_bot,
        "by_model": by_model,
        "daily": daily,
    }
