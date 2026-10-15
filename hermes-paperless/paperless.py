"""Where Paperless-ngx is and how to sign in to it, read on the Hermes server only.

The address and API token come from the environment Hermes runs with, or from `$HERMES_HOME/.env` (where a bot's own Paperless
skill keeps them). A few common names are accepted for each, so an existing setup works as it is, and the token may instead sit
in a file of its own named by `PAPERLESS_TOKEN_FILE`. The token never leaves this
server: the phone asks the plugin's routes, and the plugin asks Paperless.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

URL_NAMES = ("PAPERLESS_URL", "PAPERLESS_BASE_URL", "PAPERLESS_API_URL", "PAPERLESS_HOST", "PAPERLESS_NGX_URL")
TOKEN_NAMES = ("PAPERLESS_TOKEN", "PAPERLESS_API_TOKEN", "PAPERLESS_API_KEY", "PAPERLESS_NGX_TOKEN")
#: A file holding the token instead (so a secret kept in its own file is not copied into .env): read on each request.
TOKEN_FILE_NAMES = ("PAPERLESS_TOKEN_FILE", "PAPERLESS_API_TOKEN_FILE")

#: What the bots are told, once Paperless is set up. Short: it is part of every new session's prompt.
LINK_HINT = (
    "Paperless documents in the HermesBot app: when you find a Paperless document the user may want to see, link it as "
    "[<title>](/api/plugins/hermes-paperless/documents/<id>/preview) with its numeric id. The app opens that link in its own "
    "viewer through this server; do not download the file to show it."
)

#: The skill this plugin ships (`skills/paperless/`), as bots see it: `hermes-paperless:paperless`.
SKILL_NAME = "paperless"
#: Kept equal to the skill's frontmatter description (a test checks), which is what `skills_list` shows.
SKILL_DESCRIPTION = (
    "Find, read and show the user's Paperless-ngx documents (receipts, bills, letters, contracts, manuals): search by words, "
    "read a document's text, and link it so the HermesBot app opens it in its viewer."
)
#: Added to the prompt paragraph when the skill is registered: plugin skills are not in the prompt's skill index, so this is how
#: a bot learns it is there.
SKILL_HINT = " To find or read the user's Paperless documents, load the skill hermes-paperless:paperless (skill_view)."


@dataclass(frozen=True)
class Settings:
    url: str
    token: str

    @property
    def host(self) -> str:
        return urlsplit(self.url).netloc


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def read_env_file(path: Path) -> dict[str, str]:
    """`KEY=value` lines of a dotenv file (`export`, quotes and comments allowed). Missing or unreadable is empty."""
    values: dict[str, str] = {}

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values

    for raw in lines:
        line = raw.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, _, value = line.removeprefix("export ").partition("=")
        value = value.strip()

        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()

        values[key.strip()] = value

    return values


def normalize_url(url: str) -> str:
    """`paperless.lan:8000/api/` -> `http://paperless.lan:8000`: the server's root, without the API path."""
    value = url.strip().rstrip("/")

    if not value.lower().startswith(("http://", "https://")):
        value = f"http://{value}"

    if value.lower().endswith("/api"):
        value = value[: -len("/api")]

    return value


def load_settings(environ: dict[str, str] | None = None, env_file: Path | None = None) -> Settings | None:
    """Paperless's address and token, or None when either is missing (the routes then say it is not set up)."""
    env = os.environ if environ is None else environ
    file_values: dict[str, str] | None = None

    def first(names: tuple[str, ...]) -> str | None:
        nonlocal file_values

        for name in names:
            if env.get(name):
                return env[name]

        if file_values is None:
            file_values = read_env_file(env_file or hermes_home() / ".env")

        for name in names:
            if file_values.get(name):
                return file_values[name]

        return None

    url, token = first(URL_NAMES), first(TOKEN_NAMES)

    if not token:
        token = read_token_file(first(TOKEN_FILE_NAMES))

    return Settings(normalize_url(url), token.strip()) if url and token else None


def read_token_file(path: str | None) -> str | None:
    """The token in a file of its own (`~` allowed): its first non-empty line. Missing or unreadable is none."""
    if not path:
        return None

    try:
        lines = Path(path).expanduser().read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    return next((line.strip() for line in lines if line.strip()), None)
