"""Google Drive for the HermesBot app, through the Google sign-in Hermes already has. Read on the Hermes server only.

Hermes's google-workspace skill keeps an authorized-user OAuth token in `$HERMES_HOME/google_token.json` (with a refresh token).
This plugin reads it and refreshes the short-lived access token in memory. It never writes the file (the skill owns it), never
hands a token to anyone, and only makes read-only Drive calls, although the token may also carry Gmail or Calendar scopes.
Standard library only: the bots' script (`skills/drive/scripts/find.py`) uses it as well as the routes.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

TOKEN_FILE = "google_token.json"
#: Scopes that can read any of the account's files (drive.file only reaches files the app made itself).
DRIVE_SCOPES = ("https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/drive.readonly")
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
DRIVE_API = "https://www.googleapis.com/drive/v3"
LINK = "/api/plugins/hermes-drive/files/{id}/preview"
#: Drive file ids: letters, digits, `-` and `_`.
FILE_ID = re.compile(r"^[A-Za-z0-9_-]{10,200}$")
FOLDER = "application/vnd.google-apps.folder"
#: Google's own files are not files: they are exported, as a PDF to look at and as Office (or CSV) for the original.
GOOGLE_EXPORTS = {
    "application/vnd.google-apps.document": ("application/pdf", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    "application/vnd.google-apps.spreadsheet": ("application/pdf", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "application/vnd.google-apps.presentation": ("application/pdf", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    "application/vnd.google-apps.drawing": ("application/pdf", "image/png"),
}
#: Their text, for a bot reading one.
GOOGLE_TEXT = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}
KINDS = {
    "application/vnd.google-apps.document": "Google Doc",
    "application/vnd.google-apps.spreadsheet": "Google Sheet",
    "application/vnd.google-apps.presentation": "Google Slides",
    "application/vnd.google-apps.drawing": "Google Drawing",
    FOLDER: "Folder",
    "application/pdf": "PDF",
}

#: What the bots are told, once Drive is reachable. Short: it is part of every new session's prompt.
LINK_HINT = (
    "Google Drive files in the HermesBot app: when you find a Drive file the user may want to see, link it as "
    "[<name>](/api/plugins/hermes-drive/files/<id>/preview) with its Drive file id. The app opens that link in its own viewer "
    "through this server (Google Docs, Sheets and Slides as PDFs); do not download the file to show it."
)
SKILL_NAME = "drive"
#: Kept equal to the skill's frontmatter description (a test checks), which is what `skills_list` shows.
SKILL_DESCRIPTION = (
    "Find, read and show the user's Google Drive files (Docs, Sheets, Slides, PDFs, pictures): search by words, read a "
    "document's text, and link it so the HermesBot app opens it in its viewer."
)
SKILL_HINT = " To find or read the user's Drive files, load the skill hermes-drive:drive (skill_view)."


class Failure(Exception):
    """Something to say plainly, with the HTTP status a route answers with."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def drive_api() -> str:
    """Google's Drive API; tests point it at a local fake (only ever a loopback address)."""
    override = os.environ.get("HERMES_DRIVE_API_BASE", "").rstrip("/")

    return override if re.match(r"^http://(127\.0\.0\.1|localhost)(:\d+)?(/|$)", override) else DRIVE_API


def homes() -> list[Path]:
    """Where to look for the token: this process's Hermes home, the main home when that is a profile's, then ~/.hermes."""
    found: list[Path] = []
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")

    for candidate in (home, home.parent.parent if home.parent.name == "profiles" else None, Path.home() / ".hermes"):
        if candidate is not None and candidate not in found:
            found.append(candidate)

    return found


def parse_expiry(value: Any) -> float | None:
    """google-auth writes `2025-01-01T12:00:03.519291Z` (UTC); epoch seconds, or None when missing or unreadable."""
    if not isinstance(value, str) or not value.strip():
        return None

    text = value.strip().replace("Z", "+00:00")

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None

    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()


@dataclass(frozen=True)
class Credentials:
    client_id: str
    client_secret: str
    refresh_token: str
    token: str | None
    expiry: float | None
    token_uri: str
    scopes: tuple[str, ...]
    account: str | None

    @property
    def drive(self) -> bool:
        return any(scope in DRIVE_SCOPES for scope in self.scopes)

    def __repr__(self) -> str:  # never show the secrets in a log or a traceback
        return f"Credentials(account={self.account!r}, scopes={len(self.scopes)}, drive={self.drive})"


def read_credentials(path: Path) -> Credentials | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

    if not isinstance(data, dict) or not all(isinstance(data.get(key), str) and data.get(key) for key in ("client_id", "client_secret", "refresh_token")):
        return None

    scopes = data.get("scopes") or []
    scopes = tuple(scopes.split()) if isinstance(scopes, str) else tuple(str(scope) for scope in scopes if isinstance(scope, str))

    return Credentials(
        client_id=data["client_id"],
        client_secret=data["client_secret"],
        refresh_token=data["refresh_token"],
        token=data.get("token") if isinstance(data.get("token"), str) else None,
        expiry=parse_expiry(data.get("expiry")),
        token_uri=data.get("token_uri") if isinstance(data.get("token_uri"), str) else DEFAULT_TOKEN_URI,
        scopes=scopes,
        account=data.get("account") if isinstance(data.get("account"), str) and data.get("account") else None,
    )


def load_credentials(where: list[Path] | None = None) -> Credentials | None:
    """The first usable Google token in the Hermes homes, or None when there is none (the routes then say Drive is not set up)."""
    for home in where if where is not None else homes():
        credentials = read_credentials(home / TOKEN_FILE)

        if credentials is not None:
            return credentials

    return None


def _safe_token_uri(uri: str) -> bool:
    """The client secret only goes to an https token endpoint (or a loopback one, for tests)."""
    return uri.startswith("https://") or bool(re.match(r"^http://(127\.0\.0\.1|localhost)(:\d+)?/", uri))


class TokenCache:
    """A fresh access token, refreshed in memory when the stored one is near its end. Thread-safe; never writes the file."""

    def __init__(self, loader: Callable[[], Credentials | None] = load_credentials, opener: Callable[..., Any] = urllib.request.urlopen, clock: Callable[[], float] = time.time):
        self._loader = loader
        self._opener = opener
        self._clock = clock
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires = 0.0
        self._for: str | None = None

    def invalidate(self) -> None:
        with self._lock:
            self._token, self._expires = None, 0.0

    def get(self, force: bool = False) -> str:
        credentials = self._loader()

        if credentials is None:
            raise Failure("Google Drive is not set up on this server: there is no google_token.json from the google-workspace skill.", 503)

        if not credentials.drive:
            raise Failure("This server's Google sign-in has no Drive access: run the google-workspace skill's setup again with Drive.", 503)

        with self._lock:
            now = self._clock()

            # A new sign-in (another refresh token) starts over.
            if self._for != credentials.refresh_token:
                self._token, self._expires, self._for = None, 0.0, credentials.refresh_token

            if not force and self._token and self._expires > now + 60:
                return self._token

            if not force and not self._token and credentials.token and credentials.expiry and credentials.expiry > now + 60:
                self._token, self._expires = credentials.token, credentials.expiry
                return self._token

            self._token, self._expires = self._refresh(credentials, now)
            return self._token

    def _refresh(self, credentials: Credentials, now: float) -> tuple[str, float]:
        if not _safe_token_uri(credentials.token_uri):
            raise Failure("The Google token's token_uri is not an https address; refusing to send the client secret there.")

        body = urllib.parse.urlencode({"client_id": credentials.client_id, "client_secret": credentials.client_secret, "refresh_token": credentials.refresh_token, "grant_type": "refresh_token"}).encode()
        request = urllib.request.Request(credentials.token_uri, data=body, headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})

        try:
            with self._opener(request, timeout=20) as response:
                answer = json.load(response)
        except urllib.error.HTTPError as error:
            if error.code in (400, 401):
                raise Failure("Google refused this server's sign-in (it may have been revoked): run the google-workspace skill's setup again.") from None

            raise Failure(f"Google's sign-in answered HTTP {error.code}.") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            raise Failure(f"Can't reach Google to sign in ({type(error).__name__}).") from None

        token = answer.get("access_token") if isinstance(answer, dict) else None

        if not isinstance(token, str) or not token:
            raise Failure("Google's sign-in gave no access token.")

        return token, now + float(answer.get("expires_in") or 3600)


def search_query(words: str) -> str:
    """A Drive full-text search for these words, quotes and backslashes escaped, trash left out."""
    escaped = words.replace("\\", "\\\\").replace("'", "\\'")

    return f"fullText contains '{escaped}' and trashed = false"


RECENT_QUERY = f"trashed = false and mimeType != '{FOLDER}'"
FILE_FIELDS = "id,name,mimeType,size,modifiedTime,owners(displayName),webViewLink"
LIST_FIELDS = f"files({FILE_FIELDS})"


def kind_of(mime: str) -> str:
    if mime in KINDS:
        return KINDS[mime]

    if mime.startswith("image/"):
        return "Picture"

    if mime.startswith("video/"):
        return "Video"

    if mime.startswith("text/"):
        return "Text"

    return "File"


def summary(item: dict[str, Any]) -> dict[str, Any]:
    """A Drive file as the routes and the script show it."""
    owners = item.get("owners") or []
    owner = owners[0].get("displayName") if owners and isinstance(owners[0], dict) else None
    mime = str(item.get("mimeType") or "")

    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "mime_type": mime,
        "kind": kind_of(mime),
        "modified": item.get("modifiedTime"),
        "owner": owner,
        "size": int(item["size"]) if str(item.get("size") or "").isdigit() else None,
    }
