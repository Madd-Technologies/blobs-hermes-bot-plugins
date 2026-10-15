"""Search and read Paperless-ngx documents for a bot, and give links the HermesBot app opens.

    python3 find.py search <words> [--limit 8]
    python3 find.py recent [--limit 8]
    python3 find.py text <id> [--max-chars 8000]

Part of the hermes-paperless plugin's `paperless` skill. Paperless's address and token come from the same place as the plugin's
(`paperless.py`: the environment, then `$HERMES_HOME/.env`, then the main Hermes home's `.env` for a bot in a profile). The token
is only ever sent to Paperless; nothing here prints it. Standard library only, so any `python3` on the server runs it.
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
LINK = "/api/plugins/hermes-paperless/documents/{id}/preview"
TIMEOUT_S = 20


class Failure(Exception):
    """Something to tell the bot (and so the user) in one plain line; `code` is the exit status."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


_MODULE = "_hermes_paperless_find_settings"


def _settings_module():
    """The plugin's `paperless.py`, loaded by path once (its dataclass needs the module registered while it loads)."""
    if _MODULE not in sys.modules:
        spec = importlib.util.spec_from_file_location(_MODULE, PLUGIN / "paperless.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[_MODULE] = module
        spec.loader.exec_module(module)

    return sys.modules[_MODULE]


def load_settings():
    """The plugin's settings; a bot in a profile has its own HERMES_HOME, so the main home's `.env` is tried after it."""
    module = _settings_module()
    settings = module.load_settings()

    if settings is None and PLUGIN.parent.name == "plugins":
        settings = module.load_settings(env_file=PLUGIN.parent.parent / ".env")

    if settings is None:
        raise Failure("Paperless is not set up on this server: PAPERLESS_URL and PAPERLESS_TOKEN (or PAPERLESS_TOKEN_FILE) are missing from its .env.", 2)

    return settings


def get(settings, path: str, params: dict[str, Any] | None = None) -> Any:
    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    request = urllib.request.Request(f"{settings.url}{path}{query}", headers={"Authorization": f"Token {settings.token}", "Accept": "application/json"})

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code in (401, 403):
            raise Failure(f"Paperless at {settings.host} refused this server's token (HTTP {error.code}).") from None

        if error.code == 404:
            raise Failure("Paperless has no such document.") from None

        raise Failure(f"Paperless at {settings.host} answered HTTP {error.code}.") from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        reason = getattr(error, "reason", error)
        raise Failure(f"Can't reach Paperless at {settings.host}: {reason}.") from None


def label(document: dict[str, Any]) -> str:
    """The document's title as a link label the app reads: no brackets or line breaks."""
    title = re.sub(r"\s+", " ", str(document.get("title") or "")).strip().replace("[", "(").replace("]", ")")

    return title or f"Document {document.get('id')}"


def link(document: dict[str, Any]) -> str:
    return f"[{label(document)}]({LINK.format(id=int(document['id']))})"


def snippet(document: dict[str, Any], limit: int = 200) -> str:
    """Where the words matched (Paperless's highlights, without their markup), else the start of the text."""
    hit = document.get("__search_hit__") or {}
    text = hit.get("highlights") or document.get("content") or ""
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", str(text))).strip()

    return text if len(text) <= limit else f"{text[: limit - 1].rstrip()}…"


def names(settings, kind: str, ids: set[int]) -> dict[int, str]:
    """Correspondents' (or document types') names by id; best effort, since they only make a line read better."""
    if not ids:
        return {}

    try:
        found = get(settings, f"/api/{kind}/", {"id__in": ",".join(str(i) for i in sorted(ids)), "page_size": len(ids)})
    except Failure:
        return {}

    return {item["id"]: item.get("name") or "" for item in found.get("results", []) if isinstance(item.get("id"), int)}


def describe(document: dict[str, Any], senders: dict[int, str]) -> str:
    parts = [f"id {document.get('id')}"]

    if document.get("created"):
        parts.append(str(document["created"])[:10])

    if senders.get(document.get("correspondent")):
        parts.append(f"from {senders[document['correspondent']]}")

    if document.get("page_count"):
        parts.append(f"{document['page_count']} page{'s' if document['page_count'] != 1 else ''}")

    return " · ".join(parts)


def listing(settings, documents: list[dict[str, Any]], heading: str) -> str:
    if not documents:
        return f"{heading}: none."

    senders = names(settings, "correspondents", {d["correspondent"] for d in documents if isinstance(d.get("correspondent"), int)})
    lines = [f"{heading}. To show one in the HermesBot app, put its link in your reply exactly as written.", ""]

    for number, document in enumerate(documents, 1):
        lines.append(f"{number}. {link(document)}")
        lines.append(f"   {describe(document, senders)}")

        if text := snippet(document):
            lines.append(f"   “{text}”")

    return "\n".join(lines)


def search(settings, words: str, limit: int) -> str:
    found = get(settings, "/api/documents/", {"query": words, "page_size": limit, "truncate_content": "true"})
    documents = found.get("results", [])[:limit]
    total = found.get("count", len(documents))

    return listing(settings, documents, f"{total} document{'s' if total != 1 else ''} for “{words}”, best match first" + (f" (showing {len(documents)})" if total > len(documents) else ""))


def recent(settings, limit: int) -> str:
    found = get(settings, "/api/documents/", {"ordering": "-created", "page_size": limit, "truncate_content": "true"})

    return listing(settings, found.get("results", [])[:limit], "Newest documents")


def text(settings, document_id: int, max_chars: int) -> str:
    document = get(settings, f"/api/documents/{document_id}/")
    senders = names(settings, "correspondents", {document["correspondent"]} if isinstance(document.get("correspondent"), int) else set())
    content = str(document.get("content") or "").strip()
    shown = content[:max_chars]
    what = "no text (Paperless found none)" if not content else f"first {len(shown)} of {len(content)} characters" if len(content) > max_chars else f"{len(content)} characters"

    return "\n".join([link(document), describe(document, senders), f"--- text: {what} ---", shown] if content else [link(document), describe(document, senders), f"--- {what} ---"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="find.py", description="Search and read Paperless-ngx documents; links open in the HermesBot app.")
    commands = parser.add_subparsers(dest="command", required=True)
    find = commands.add_parser("search", help="full-text search, best match first")
    find.add_argument("words", nargs="+")
    find.add_argument("--limit", type=int, default=8)
    newest = commands.add_parser("recent", help="the newest documents")
    newest.add_argument("--limit", type=int, default=8)
    read = commands.add_parser("text", help="a document's text")
    read.add_argument("id", type=int)
    read.add_argument("--max-chars", type=int, default=8000)
    args = parser.parse_args(argv)

    try:
        settings = load_settings()

        if args.command == "search":
            print(search(settings, " ".join(args.words).strip(), max(1, min(args.limit, 25))))
        elif args.command == "recent":
            print(recent(settings, max(1, min(args.limit, 25))))
        else:
            print(text(settings, args.id, max(200, min(args.max_chars, 50_000))))
    except Failure as failure:
        print(failure, file=sys.stderr)

        return failure.code

    return 0


if __name__ == "__main__":
    sys.exit(main())
