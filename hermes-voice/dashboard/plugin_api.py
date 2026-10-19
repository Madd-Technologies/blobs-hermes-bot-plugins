"""The phone's side of hermes-voice: each bot's voice settings, and the one-time secret a call opens with.

Mounted by Hermes under `/api/plugins/hermes-voice/`, behind the dashboard's own sign-in. Every bot route takes `?profile=<bot>`,
which Hermes 0.21.6+ also uses to scope the request to that bot's home and secrets, so the bot's own `XAI_API_KEY` and the
right sign-in are read. One copy of this plugin, in the default profile, serves every bot.

  GET  /status               {version, provider, model}
  GET  /bot?profile=         {enabled, voice, account, available: {sign_in, key}, ready, model}
  PUT  /bot?profile=         {enabled?, voice?, account?} -> the same as GET
  POST /session?profile=     {name?, about?} -> {url, protocol_prefix, client_secret, expires_at, model, account, session}

Refusals are `{"detail": {"reason", "message"}}`. Nothing here ever returns or logs the sign-in token or a key.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

_MODULE = "_hermes_voice_core"
log = logging.getLogger("hermes-voice")


def _core():
    """`voice.py`, loaded once by path: Hermes imports this file apart from the plugin package."""
    if _MODULE not in sys.modules:
        spec = importlib.util.spec_from_file_location(_MODULE, Path(__file__).resolve().parent.parent / "voice.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[_MODULE] = module
        spec.loader.exec_module(module)

    return sys.modules[_MODULE]


def _root() -> Path:
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root()


#: Tests replace these: the Hermes root, the account readers and the poster that asks xAI.
root = _root
accounts = lambda: _core().hermes_accounts()  # noqa: E731
poster = None


def _store():
    return _core().Store(root() / "hermes-voice")


def _version() -> str:
    try:
        import yaml

        manifest = yaml.safe_load((Path(__file__).resolve().parent.parent / "plugin.yaml").read_text(encoding="utf-8")) or {}
        return str(manifest.get("version") or "")
    except Exception:
        return ""


def _bot(profile: str) -> str:
    """A bot this server has: `default`, or a folder under the root's `profiles/`."""
    core = _core()

    if not core.PROFILE.match(profile or ""):
        raise core.VoiceError("invalid", "That is not a bot name.", 400)
    if profile != "default" and not (root() / "profiles" / profile).is_dir():
        raise core.VoiceError("unknown_bot", f"No bot called {profile} on this server.", 404)

    return profile


def _refusal(error) -> JSONResponse:
    log.info("hermes-voice: refused (%s)", error.reason)
    return JSONResponse(status_code=error.status, content={"detail": error.detail()})


class Change(BaseModel):
    enabled: Optional[bool] = None
    voice: Optional[str] = None
    account: Optional[str] = None


class Caller(BaseModel):
    name: Optional[str] = None
    about: Optional[str] = None


router = APIRouter()


@router.get("/status")
def status() -> Dict[str, Any]:
    return {"version": _version(), "provider": "xai", "model": _core().MODEL}


@router.get("/bot")
def get_bot(profile: str = Query(...)):
    core = _core()

    try:
        return core.view(_store().get(_bot(profile)), accounts())
    except core.VoiceError as error:
        return _refusal(error)


@router.put("/bot")
def put_bot(change: Change, profile: str = Query(...)):
    core = _core()

    try:
        bot = _bot(profile)
        saved = _store().update(bot, core.check_change(change.model_dump()))
        return core.view(saved, accounts())
    except core.VoiceError as error:
        return _refusal(error)


@router.post("/session")
def session(caller: Optional[Caller] = None, profile: str = Query(...)):
    core = _core()
    caller = caller or Caller()

    try:
        bot = _bot(profile)
        readers = accounts()
        settings = core.view(_store().get(bot), readers)
        out = core.mint(settings, readers, core.persona(caller.name, caller.about, bot), poster)
    except core.VoiceError as error:
        return _refusal(error)

    log.info("hermes-voice: a call secret for %s (%s, expires_at=%s)", bot, out["account"], out["expires_at"])
    return out
