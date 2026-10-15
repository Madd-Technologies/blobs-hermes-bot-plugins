"""Keep a dashboard collection fresh from a script, with no model call: what a bot's no_agent cron job or monitor script
runs instead of importing the plugin or opening the database.

    python3 $HERMES_HOME/plugins/hermes-dashboard/write.py upsert <collection> < records.json
    python3 $HERMES_HOME/plugins/hermes-dashboard/write.py update <record_id> < values.json
    python3 $HERMES_HOME/plugins/hermes-dashboard/write.py close|reopen <record_id>
    python3 $HERMES_HOME/plugins/hermes-dashboard/write.py note <record_id> <text>
    python3 $HERMES_HOME/plugins/hermes-dashboard/write.py query <collection> [--where JSON] [--limit N]
    python3 $HERMES_HOME/plugins/hermes-dashboard/write.py pending <collection> [--where JSON] [--retry-after 6h]

`records.json` is a list of `{source_key, values}` (or `{"records": [...]}`). Everything is checked like the
dashboard_* tools do (field types, the person's edits win, history), and the history says the bot's script did it.

The bot is the profile this runs as: `HERMES_HOME` (Hermes sets it for cron scripts), else the profile whose `scripts/`
folder this runs from. There is deliberately no way to name another bot. Stdlib only; any Python 3.9+.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import store as S  # noqa: E402


def whoami() -> tuple[str, Path]:
    """(profile, Hermes root) for the job running this."""
    raw = os.environ.get("HERMES_HOME", "").strip()
    home = Path(raw).expanduser() if raw else (Path.cwd().parent if Path.cwd().name == "scripts" else None)

    if home is None:
        raise SystemExit("write.py: HERMES_HOME isn't set; run it from a Hermes cron job or from your scripts/ folder")

    home = home.resolve()

    if home.parent.name == "profiles":
        return home.name, home.parent.parent

    return "default", home


def _seconds(text: str) -> int:
    match = re.fullmatch(r"(\d+)\s*([smhd]?)", text.strip().lower())

    if not match:
        raise SystemExit(f"write.py: --retry-after is like 30m, 6h or 1d, not {text!r}")

    return int(match.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86_400}[match.group(2)]


def _stdin_json():
    try:
        return json.load(sys.stdin)
    except ValueError as error:
        raise SystemExit(f"write.py: stdin isn't JSON: {error}") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="write.py", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    upsert = commands.add_parser("upsert")
    upsert.add_argument("collection")
    upsert.add_argument("--override-person", action="store_true", help="overwrite fields the person changed (only when they asked)")
    commands.add_parser("update").add_argument("record_id")
    commands.add_parser("close").add_argument("record_id")
    commands.add_parser("reopen").add_argument("record_id")
    note = commands.add_parser("note")
    note.add_argument("record_id")
    note.add_argument("text", nargs="+")
    query = commands.add_parser("query")
    query.add_argument("collection")
    query.add_argument("--where")
    query.add_argument("--limit", type=int, default=200)
    pending = commands.add_parser("pending")
    pending.add_argument("collection")
    pending.add_argument("--where")
    pending.add_argument("--retry-after", default="6h")
    args = parser.parse_args(argv)

    profile, root = whoami()
    store = S.Store(root / "hermes-dashboard")
    actor = S.Actor.script(profile)

    try:
        if args.command == "upsert":
            data = _stdin_json()
            records = data.get("records") if isinstance(data, dict) else data

            if not isinstance(records, list):
                raise SystemExit("write.py: upsert reads a list of {source_key, values}")

            added = updated = kept = 0

            for start in range(0, len(records), S.MAX_BATCH):
                for result in store.upsert(actor, args.collection, records[start : start + S.MAX_BATCH], override_person=args.override_person):
                    added += bool(result["created"])
                    updated += bool(result["changed"]) and not result["created"]
                    kept += bool(result["kept"])

            print(f"{added} added, {updated} updated, {len(records) - added - updated} unchanged" + (f", {kept} kept as your person changed them" if kept else ""))
        elif args.command == "update":
            result = store.update(actor, args.record_id, _stdin_json())
            print(f"updated {', '.join(result['changed']) or 'nothing'}" + (f"; kept {', '.join(result['kept'])} (your person changed them)" if result["kept"] else ""))
        elif args.command in ("close", "reopen"):
            result = store.set_closed(actor, args.record_id, args.command == "close")
            print(("closed" if args.command == "close" else "reopened") if result["changed"] else "already so")
        elif args.command == "note":
            store.add_note(actor, args.record_id, " ".join(args.text))
            print("noted")
        elif args.command == "query":
            where = json.loads(args.where) if args.where else None
            print(json.dumps(store.query(actor, args.collection, {"filter": where, "limit": args.limit})["records"], ensure_ascii=False, default=str))
        else:
            where = json.loads(args.where) if args.where else None
            print("\n".join(store.pending(actor, args.collection, where, _seconds(args.retry_after))))
    except (S.DashboardError, S.NotFound) as error:
        print(f"write.py: {error}", file=sys.stderr)

        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
