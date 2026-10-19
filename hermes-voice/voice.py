"""hermes-voice's core: each bot's voice settings, the xAI account a call uses, and the one-time secret the phone opens a call with.

The phone never sees the Grok sign-in or an xAI key. `mint` trades one for a short-lived client secret
(`POST /v1/realtime/client_secrets`) and returns what the phone needs to open `wss://api.x.ai/v1/realtime` itself: the URL, the
secret (sent as the `xai-client-secret.<value>` WebSocket subprotocol) and the first `session.update` body. A secret opens one
connection and is only checked when it does; the call then runs on (checked against xAI on 2026-10-18).

Minting, the persona and the two tools are adapted from hermes-xai's `voice.py` (MIT, (c) 2026 Julien Talbot; see
THIRD_PARTY_NOTICES.md). Vendor contract: https://docs.x.ai/developers/model-capabilities/audio/speech-to-speech and
https://docs.x.ai/developers/model-capabilities/audio/ephemeral-tokens
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

API_BASE = "https://api.x.ai/v1"
CLIENT_SECRETS_URL = f"{API_BASE}/realtime/client_secrets"
REALTIME_URL = "wss://api.x.ai/v1/realtime"
PROTOCOL_PREFIX = "xai-client-secret."
#: Long enough to connect; a secret opens one connection and a call outlives it, so there is no reason to keep it longer.
SECRET_TTL_S = 60

MODEL = "grok-voice-latest"  # grok-voice-3.0 in October 2026
TRANSCRIPTION_MODEL = "grok-transcribe"  # captions of what the person says
VOICES = ("eve", "ara", "rex", "sal", "leo")
DEFAULT_VOICE = "eve"
ACCOUNTS = ("sign-in", "key")
#: The voice layer only talks; the bot does the thinking, so the fastest answer wins.
REASONING_EFFORT = "none"

PROFILE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
MAX_NAME = 64
MAX_ABOUT = 500

PERSONA = """# Role
You are {name}, a calm, friendly voice assistant.{about} You are the voice of an agent that works for the user: the agent does the work, you hand it the tasks.

# Objective
Understand what the user wants, hand real work to the agent with `hermes_task`, and say the result when it arrives.

# Flow
- Anything that needs facts, current information, files, the web, memory, or an action: call `hermes_task` with a clear, complete request in the user's own words.
- When you call it, say one very short sentence ("on it"), then wait. Never guess a result.
- A message starting with "[Hermes result]" is the agent's answer: say it in one or two plain sentences. If it says the task failed, say so simply.
- A message starting with "[Hermes needs you]" means the agent is waiting for the user to approve or answer something: say it in one short sentence and ask them to tap it on their screen. Never approve or answer it yourself.
- A correction ("no, Thursday") on a running task: call `hermes_task` again with the corrected request.
- Greetings, small talk, or repeating what was just said: answer directly, no tool.
- When the user clearly says goodbye, say goodbye in one sentence, then call `end_call`.

# Limits
- Never say something is done before the result arrives.
- Do not name the tools or the agent; say "let me check", "it's sent".
- If the request is unclear, ask one short question.

# Style
Speak in the user's language. One or two short spoken sentences per turn, no lists, no symbols."""

TOOLS = [
    {
        "type": "function",
        "name": "hermes_task",
        "description": (
            "Hand a task to the agent (act, search, check, write, remember, use a service). Returns a receipt at once; the "
            'result arrives later in a message starting with "[Hermes result]".'
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "request": {
                    "type": "string",
                    "description": "The complete request, as the user would type it, with every useful detail.",
                }
            },
            "required": ["request"],
        },
    },
    {
        "type": "function",
        "name": "end_call",
        "description": "End the conversation, only after a clear goodbye from the user and your own goodbye.",
        "parameters": {"type": "object", "properties": {}},
    },
]

#: (url, headers, json body, timeout) -> (status, parsed body). Tests pass a fake.
Poster = Callable[[str, Dict[str, str], Dict[str, Any], float], Tuple[int, Any]]


class VoiceError(Exception):
    """A refusal the phone can act on: `reason` is one of off, no_sign_in, no_key, rejected, unreachable, invalid, unknown_bot."""

    def __init__(self, reason: str, message: str, status: int):
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.status = status

    def detail(self) -> Dict[str, str]:
        return {"reason": self.reason, "message": self.message}


class Accounts:
    """The two ways a bot can pay for a call, both read inside the request's profile scope (Hermes 0.21.6+ sets it from
    `?profile=`): the server's Grok sign-in and the bot's own `XAI_API_KEY`. Tests replace the two readers."""

    def __init__(self, sign_in: Callable[[bool, bool], str], key: Callable[[], str]):
        #: (refresh_if_expiring, force_refresh) -> access token, or raises when there is no usable sign-in.
        self._sign_in = sign_in
        self._key = key

    def has_sign_in(self) -> bool:
        """Whether a sign-in is stored, without asking xAI (an expired token still counts: minting refreshes it)."""
        try:
            return bool(self._sign_in(False, False))
        except Exception:
            return False

    def has_key(self) -> bool:
        try:
            return bool(self._key())
        except Exception:
            return False

    def sign_in(self, force: bool = False) -> str:
        try:
            token = self._sign_in(True, force)
        except Exception:
            token = ""

        if not token:
            raise VoiceError("no_sign_in", "This server has no Grok sign-in that works. Sign in to xAI Grok in Hermes again.", 409)

        return token

    def key(self) -> str:
        key = ""

        try:
            key = self._key()
        except Exception:
            pass

        if not key:
            raise VoiceError("no_key", "This bot has no XAI_API_KEY. Add one in the bot's Keys.", 409)

        return key


def hermes_accounts() -> Accounts:
    """The readers inside Hermes: its own sign-in helper (it falls back to the root sign-in for a bot without one, refreshes
    under Hermes's lock and writes the rotated grant back) and the profile-scoped secret store."""

    def sign_in(refresh: bool, force: bool) -> str:
        from hermes_cli.auth import resolve_xai_oauth_runtime_credentials

        credentials = resolve_xai_oauth_runtime_credentials(refresh_if_expiring=refresh, force_refresh=force)
        return str(credentials.get("api_key") or "").strip()

    def key() -> str:
        from agent.secret_scope import get_secret

        try:
            return str(get_secret("XAI_API_KEY", "") or "").strip()
        except Exception:  # several profiles and none bound: the process env may hold another bot's key
            return ""

    return Accounts(sign_in, key)


class Store:
    """Each bot's saved voice settings, one JSON file under the Hermes root (`hermes-voice/bots.json`)."""

    def __init__(self, folder: Path):
        self.path = Path(folder) / "bots.json"
        self._lock = threading.Lock()

    def _read(self) -> Dict[str, Dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}

        bots = data.get("bots") if isinstance(data, dict) else None
        return {k: v for k, v in bots.items() if isinstance(v, dict)} if isinstance(bots, dict) else {}

    def get(self, profile: str) -> Dict[str, Any]:
        with self._lock:
            return dict(self._read().get(profile, {}))

    def update(self, profile: str, change: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            bots = self._read()
            saved = {**bots.get(profile, {}), **change}
            bots[profile] = saved
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(prefix=".bots.", dir=self.path.parent)

            try:
                with os.fdopen(handle, "w", encoding="utf-8") as file:
                    json.dump({"bots": bots}, file, indent=1, sort_keys=True)
                os.replace(temporary, self.path)
            except BaseException:
                Path(temporary).unlink(missing_ok=True)
                raise

            return dict(saved)


def check_change(change: Dict[str, Any]) -> Dict[str, Any]:
    """The settings a phone may change, checked; anything else is refused rather than silently dropped."""
    clean: Dict[str, Any] = {}

    for field, value in change.items():
        if value is None:
            continue
        if field == "enabled" and isinstance(value, bool):
            clean[field] = value
        elif field == "voice" and value in VOICES:
            clean[field] = value
        elif field == "account" and value in ACCOUNTS:
            clean[field] = value
        else:
            raise VoiceError("invalid", f"{field} cannot be set to {value!r}.", 400)

    return clean


def view(saved: Dict[str, Any], accounts: Accounts) -> Dict[str, Any]:
    """A bot's settings as the phone shows them. A bot never set has calls on (installing the plugin was the opt-in) and uses
    the Grok sign-in when the server has one, otherwise its own key."""
    available = {"sign_in": accounts.has_sign_in(), "key": accounts.has_key()}
    account = saved.get("account") if saved.get("account") in ACCOUNTS else ("sign-in" if available["sign_in"] else "key")

    return {
        "enabled": saved.get("enabled") if isinstance(saved.get("enabled"), bool) else True,
        "voice": saved.get("voice") if saved.get("voice") in VOICES else DEFAULT_VOICE,
        "account": account,
        "available": available,
        "ready": available["sign_in" if account == "sign-in" else "key"],
        "model": MODEL,
    }


def _clean(text: Any, limit: int) -> str:
    return _CONTROL.sub(" ", str(text or "")).strip()[:limit]


def persona(name: Any, about: Any, profile: str) -> str:
    name = _clean(name, MAX_NAME) or profile.replace("-", " ").replace("_", " ").title()
    about = _clean(about, MAX_ABOUT)

    return PERSONA.format(name=name, about=f" What you are for: {about}" if about else "")


def session_update(voice: str, instructions: str) -> Dict[str, Any]:
    """The `session` object of the phone's first `session.update`."""
    pcm = {"type": "audio/pcm", "rate": 24000}

    return {
        "voice": voice,
        "instructions": instructions,
        "reasoning": {"effort": REASONING_EFFORT},
        "turn_detection": {"type": "server_vad"},
        "tools": TOOLS,
        "audio": {"input": {"format": pcm, "transcription": {"model": TRANSCRIPTION_MODEL}}, "output": {"format": pcm}},
    }


def post_json(url: str, headers: Dict[str, str], payload: Dict[str, Any], timeout: float) -> Tuple[int, Any]:
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST", headers=headers)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as error:
        return error.code, None


def _ask(bearer: str, post: Poster) -> Tuple[int, Any]:
    try:
        return post(
            CLIENT_SECRETS_URL,
            {"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"},
            {"expires_after": {"seconds": SECRET_TTL_S}},
            20.0,
        )
    except Exception as error:  # DNS, TLS, timeout
        raise VoiceError("unreachable", f"xAI could not be reached ({type(error).__name__}).", 502) from None


def mint(settings: Dict[str, Any], accounts: Accounts, instructions: str, post: Optional[Poster] = None) -> Dict[str, Any]:
    """A one-time secret, the URL and the first `session.update` body for one call. Raises `VoiceError`; never returns or
    logs the sign-in token or the key."""
    if not settings["enabled"]:
        raise VoiceError("off", "Calls are off for this bot.", 403)

    post = post or post_json
    signed_in = settings["account"] == "sign-in"
    bearer = accounts.sign_in() if signed_in else accounts.key()
    status, body = _ask(bearer, post)

    if status == 401 and signed_in:  # another process may have rotated the grant: refresh once and ask again
        bearer = accounts.sign_in(force=True)
        status, body = _ask(bearer, post)

    if status == 403:
        plan = "the Grok plan on this sign-in" if signed_in else "this key's xAI team"
        raise VoiceError("rejected", f"xAI refused a voice call for {plan} (403).", 502)
    if status >= 400 or not isinstance(body, dict):
        raise VoiceError("rejected", f"xAI refused the call ({status}).", 502)

    nested = body.get("client_secret") if isinstance(body.get("client_secret"), dict) else {}
    value = body.get("value") or nested.get("value")
    expires = body.get("expires_at") or nested.get("expires_at")

    if not isinstance(value, str) or not value or value == bearer:
        raise VoiceError("rejected", "xAI gave an unexpected answer.", 502)

    return {
        "url": f"{REALTIME_URL}?model={MODEL}",
        "protocol_prefix": PROTOCOL_PREFIX,
        "client_secret": value,
        "expires_at": int(expires) if isinstance(expires, (int, float)) else None,
        "model": MODEL,
        "account": settings["account"],
        "session": session_update(settings["voice"], instructions),
    }
