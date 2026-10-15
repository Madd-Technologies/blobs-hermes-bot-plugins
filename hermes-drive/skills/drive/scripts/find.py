"""Search and read Google Drive files for a bot, and give links the HermesBot app opens.

    python3 find.py search <words> [--limit 8]
    python3 find.py recent [--limit 8]
    python3 find.py text <id> [--max-chars 8000]

Part of the hermes-drive plugin's `drive` skill. It uses the Google sign-in Hermes's google-workspace skill keeps
(`google_token.json`, looked for in this Hermes home, then the main one for a bot in a profile), refreshes the access token in
memory only, sends it only to Google, and never prints it. Read-only Drive calls only. Standard library only.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

PLUGIN = Path(__file__).resolve().parents[3]
_MODULE = "_hermes_drive_find_core"
TIMEOUT_S = 30


def _core():
    """The plugin's `drive.py`, loaded by path once (its dataclass needs the module registered while it loads)."""
    if _MODULE not in sys.modules:
        spec = importlib.util.spec_from_file_location(_MODULE, PLUGIN / "drive.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[_MODULE] = module
        spec.loader.exec_module(module)

    return sys.modules[_MODULE]


class Failure(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


class Drive:
    def __init__(self):
        core = _core()
        where = core.homes()

        # Installed at <main home>/plugins/hermes-drive: that home holds the token even when this bot runs in a profile.
        if PLUGIN.parent.name == "plugins" and PLUGIN.parent.parent not in where:
            where.append(PLUGIN.parent.parent)

        self.cache = core.TokenCache(loader=lambda: core.load_credentials(where))

    def token(self, force: bool = False) -> str:
        try:
            return self.cache.get(force)
        except _core().Failure as failure:
            raise Failure(str(failure), 2 if failure.status == 503 else 1) from None

    def get(self, path: str, params: dict[str, Any], raw: bool = False) -> Any:
        url = f"{_core().drive_api()}{path}?{urllib.parse.urlencode(params)}"

        for attempt in (0, 1):
            request = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token(force=attempt == 1)}", "Accept": "*/*" if raw else "application/json"})

            try:
                with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
                    return response.read() if raw else json.load(response)
            except urllib.error.HTTPError as error:
                if error.code == 401 and attempt == 0:
                    continue

                raise Failure(self._explain(error)) from None
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                raise Failure(f"Can't reach Google Drive: {getattr(error, 'reason', error)}.") from None

        raise Failure("Google Drive refused this server's sign-in.")

    @staticmethod
    def _explain(error: urllib.error.HTTPError) -> str:
        try:
            reason = (json.load(error).get("error", {}).get("errors") or [{}])[0].get("reason", "")
        except Exception:
            reason = ""

        if reason == "exportSizeLimitExceeded":
            return "This Google file is too big for Drive to export. Show its link instead."

        if error.code == 404:
            return "Drive has no such file, or this account can't see it."

        return f"Google Drive answered HTTP {error.code}{f' ({reason})' if reason else ''}."


def label(item: dict[str, Any]) -> str:
    """The file's name as a link label the app reads: no brackets or line breaks."""
    name = re.sub(r"\s+", " ", str(item.get("name") or "")).strip().replace("[", "(").replace("]", ")")

    return name or f"File {item.get('id')}"


def link(item: dict[str, Any]) -> str:
    return f"[{label(item)}]({_core().LINK.format(id=item['id'])})"


def describe(item: dict[str, Any]) -> str:
    summary = _core().summary(item)
    parts = [summary["kind"]]

    if summary["modified"]:
        parts.append(str(summary["modified"])[:10])

    if summary["owner"]:
        parts.append(f"owner {summary['owner']}")

    return " · ".join(parts)


def listing(files: list[dict[str, Any]], heading: str) -> str:
    if not files:
        return f"{heading}: none."

    lines = [f"{heading}. To show one in the HermesBot app, put its link in your reply exactly as written.", ""]

    for number, item in enumerate(files, 1):
        lines.append(f"{number}. {link(item)}")
        lines.append(f"   {describe(item)}")

    return "\n".join(lines)


def search(drive: Drive, words: str, limit: int) -> str:
    core = _core()
    found = drive.get("/files", {"q": core.search_query(words), "pageSize": limit, "fields": core.LIST_FIELDS, "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"})

    return listing(found.get("files", [])[:limit], f"Files for “{words}”, best match first")


def recent(drive: Drive, limit: int) -> str:
    core = _core()
    found = drive.get("/files", {"q": core.RECENT_QUERY, "orderBy": "modifiedTime desc", "pageSize": limit, "fields": core.LIST_FIELDS, "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"})

    return listing(found.get("files", [])[:limit], "Last modified files")


def text(drive: Drive, file_id: str, max_chars: int) -> str:
    core = _core()

    if not core.FILE_ID.match(file_id):
        raise Failure("That is not a Drive file id.", 2)

    item = drive.get(f"/files/{file_id}", {"fields": core.FILE_FIELDS, "supportsAllDrives": "true"})
    mime = str(item.get("mimeType") or "")

    if mime in core.GOOGLE_TEXT:
        body = drive.get(f"/files/{file_id}/export", {"mimeType": core.GOOGLE_TEXT[mime]}, raw=True)
    elif mime.startswith("text/") or mime in ("application/json", "application/xml"):
        body = drive.get(f"/files/{file_id}", {"alt": "media", "supportsAllDrives": "true"}, raw=True)
    else:
        return "\n".join([link(item), describe(item), f"--- no text here for a {core.kind_of(mime).lower()}: show its link instead ---"])

    content = body.decode("utf-8", errors="replace").strip()
    shown = content[:max_chars]
    what = "empty" if not content else f"first {len(shown)} of {len(content)} characters" if len(content) > max_chars else f"{len(content)} characters"

    return "\n".join([link(item), describe(item), f"--- text: {what} ---", shown] if content else [link(item), describe(item), "--- text: empty ---"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="find.py", description="Search and read Google Drive files; links open in the HermesBot app.")
    commands = parser.add_subparsers(dest="command", required=True)
    find = commands.add_parser("search", help="full-text search, best match first")
    find.add_argument("words", nargs="+")
    find.add_argument("--limit", type=int, default=8)
    newest = commands.add_parser("recent", help="the last modified files")
    newest.add_argument("--limit", type=int, default=8)
    read = commands.add_parser("text", help="a document's text")
    read.add_argument("id")
    read.add_argument("--max-chars", type=int, default=8000)
    args = parser.parse_args(argv)

    try:
        drive = Drive()

        if args.command == "search":
            print(search(drive, " ".join(args.words).strip(), max(1, min(args.limit, 25))))
        elif args.command == "recent":
            print(recent(drive, max(1, min(args.limit, 25))))
        else:
            print(text(drive, args.id, max(200, min(args.max_chars, 50_000))))
    except Failure as failure:
        print(failure, file=sys.stderr)

        return failure.code

    return 0


if __name__ == "__main__":
    sys.exit(main())
