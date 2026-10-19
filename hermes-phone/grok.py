"""The xAI side of a call: which account pays, and the realtime socket.

A phone call uses the bot's own Voice setting when hermes-voice is installed (`~/.hermes/hermes-voice/bots.json`: the voice,
and the Grok sign-in or the bot's `XAI_API_KEY`), and otherwise the sign-in when the server has one. The sign-in is read
and refreshed through Hermes's own helper, so Hermes keeps managing it. Nothing here logs a token or a key.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

try:
    from . import persona as P
    from . import twilio_api
except ImportError:  # the tests import the modules flat
    import persona as P  # type: ignore[no-redef]
    import twilio_api  # type: ignore[no-redef]

PROTOCOL_PREFIX = "xai-client-secret."
VOICES = ("eve", "ara", "rex", "sal", "leo")


class GrokError(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def home_of(root: Path, profile: str) -> Path:
    return Path(root) if profile == "default" else Path(root) / "profiles" / profile


def voice_settings(root: Path, profile: str) -> Dict[str, Any]:
    try:
        data = json.loads((Path(root) / "hermes-voice" / "bots.json").read_text(encoding="utf-8"))
        saved = (data.get("bots") or {}).get(profile) or {}
    except (OSError, ValueError, AttributeError):
        saved = {}
    return saved if isinstance(saved, dict) else {}


def hermes_sign_in() -> str:
    from hermes_cli.auth import resolve_xai_oauth_runtime_credentials

    return str(resolve_xai_oauth_runtime_credentials(refresh_if_expiring=True, force_refresh=False).get("api_key") or "").strip()


def bearer(root: Path, profile: str, sign_in: Optional[Callable[[], str]] = None) -> Tuple[str, str, str]:
    """(token, account, voice) for this bot's calls. Raises GrokError when the chosen account isn't there."""
    saved = voice_settings(root, profile)
    voice = saved.get("voice") if saved.get("voice") in VOICES else "eve"
    read_sign_in = sign_in or hermes_sign_in
    key = twilio_api.read_env(home_of(root, profile) / ".env").get("XAI_API_KEY", "").strip()

    def signed_in() -> str:
        try:
            return read_sign_in()
        except Exception:
            return ""

    account = saved.get("account")
    if account not in ("sign-in", "key"):
        token = signed_in()
        if token:
            return token, "sign-in", voice
        account = "key"
    if account == "sign-in":
        token = signed_in()
        if not token:
            raise GrokError("no_sign_in", "This server has no Grok sign-in that works.")
        return token, "sign-in", voice
    if not key:
        raise GrokError("no_key", "This bot has no XAI_API_KEY.")
    return key, "key", voice


async def connect(token: str = "", secret: str = "", url: str = P.REALTIME_URL):
    """The realtime socket: with a bearer token (the service), or a one-time client secret (the red-team runner)."""
    from websockets.asyncio.client import connect as ws_connect

    if secret:
        return await ws_connect(url, subprotocols=[PROTOCOL_PREFIX + secret], max_size=2**22, ping_interval=20)
    return await ws_connect(url, additional_headers={"Authorization": f"Bearer {token}"}, max_size=2**22, ping_interval=20)
