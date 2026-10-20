"""The phone app's side of hermes-bot-mobile: register a device, change how loud each bot is, send a test, read what
each bot in a group room is doing right now, set up where a bot's commands run (its SSH key and a connection check),
restart this dashboard (and the gateway) from the app, Taildrop a file from this server to one of the person's devices,
push what waits on the person (Needs you) to the app the moment it changes, and answer a kanban worker's approval.

Mounted by `hermes serve` under `/api/plugins/hermes-bot-mobile/`, behind the dashboard's own sign-in, so only someone
who can already use the server can reach it. Hermes imports this file on its own, apart from the plugin package,
so it loads the package's modules by path and shares state with the hooks through the files in `store.py`.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import sys
import threading
import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

_PACKAGE = "_hermes_bot_mobile_package"


def _package():
    """The plugin package (its `store`, `notify`, `webpush`), loaded once by path the way Hermes loads it."""
    if _PACKAGE in sys.modules:
        return sys.modules[_PACKAGE]

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(_PACKAGE, root / "__init__.py", submodule_search_locations=[str(root)])
    module = importlib.util.module_from_spec(spec)
    module.__package__ = _PACKAGE
    module.__path__ = [str(root)]
    sys.modules[_PACKAGE] = module
    spec.loader.exec_module(module)

    return module


def _modules():
    _package()

    return importlib.import_module(f"{_PACKAGE}.store"), importlib.import_module(f"{_PACKAGE}.notify")


def _activity():
    _package()

    return importlib.import_module(f"{_PACKAGE}.activity")


def _runs_on():
    _package()

    return importlib.import_module(f"{_PACKAGE}.runs_on")


def _restart():
    _package()

    return importlib.import_module(f"{_PACKAGE}.restart")


#: The one restarter of this dashboard process: a restart waiting for the chats to finish lives in it.
_RESTARTER = None
_RESTARTER_LOCK = threading.Lock()


def _restarter():
    global _RESTARTER

    with _RESTARTER_LOCK:
        if _RESTARTER is None:
            _RESTARTER = _restart().Restarter()

        return _RESTARTER


def _taildrop():
    _package()

    return importlib.import_module(f"{_PACKAGE}.taildrop")


#: The one Taildrop sender of this dashboard process: its running sends live in it.
_TAILDROP = None
_TAILDROP_LOCK = threading.Lock()


def _sender():
    global _TAILDROP

    with _TAILDROP_LOCK:
        if _TAILDROP is None:
            _TAILDROP = _taildrop().Taildrop()

        return _TAILDROP


def _needs_you_module():
    _package()

    return importlib.import_module(f"{_PACKAGE}.needs_you")


def _kanban_module():
    _package()

    return importlib.import_module(f"{_PACKAGE}.kanban_approvals")


#: The one watch of this dashboard's prompts: it swaps Hermes's open-requests table once per process.
_NEEDS_YOU = None
_NEEDS_YOU_LOCK = threading.Lock()


def _needs_you():
    global _NEEDS_YOU

    with _NEEDS_YOU_LOCK:
        if _NEEDS_YOU is None:
            _NEEDS_YOU = _needs_you_module().NeedsYou(kanban=_kanban_module().KanbanApprovals())

        return _NEEDS_YOU


def start_needs_you() -> bool:
    """Starts pushing Needs you to the app. Called when the dashboard mounts this file; a Hermes without the table (or
    a process without the gateway, like `hermes plugins validate`) just leaves the app on its own check."""
    try:
        return _needs_you().install()
    except Exception:
        return False


def _refusal(refusal) -> JSONResponse:
    content = {"detail": refusal.detail, **refusal.extra}
    headers = {"Retry-After": str(refusal.extra["retry_after"])} if "retry_after" in refusal.extra else None

    return JSONResponse(status_code=refusal.status, content=content, headers=headers)


def _profile_home(profile: str) -> Path:
    """A bot's own folder: the Hermes root for `default`, `profiles/<name>` under it for the others."""
    from hermes_constants import get_default_hermes_root

    root = get_default_hermes_root()

    return root if profile == "default" else root / "profiles" / profile


def _profile_exists(profile: str) -> bool:
    """A bot this server has: `default`, or a folder under the root's `profiles/`."""
    return profile == "default" or _profile_home(profile).is_dir()


def _keys():
    return _runs_on().Keys(_profile_home)


def _known_bot(profile: str) -> str:
    runs_on = _runs_on()

    try:
        runs_on.check_profile(profile)
    except runs_on.Invalid as failure:
        raise HTTPException(status_code=422, detail=str(failure))

    if not _profile_exists(profile):
        raise HTTPException(status_code=404, detail=f"No bot called {profile} on this server.")

    return profile


router = APIRouter()


class DeviceIn(BaseModel):
    endpoint: str
    p256dh: str
    auth: str
    name: str = ""


class SettingsIn(BaseModel):
    default: str | None = None
    #: profile -> "all" | "needs_you" | "off", or null to follow the default
    bots: dict[str, str | None] | None = None


class TestIn(BaseModel):
    device: str | None = None


class KeyIn(BaseModel):
    profile: str


class CheckIn(BaseModel):
    profile: str
    #: "ssh" or "docker" (a bot on this computer needs no check).
    backend: str = "ssh"
    host: str = ""
    user: str = ""
    port: int = 22
    #: The private key's full path on this server; empty uses ssh's own defaults, as Hermes does.
    key: str = ""


class PresenceIn(BaseModel):
    #: The server's id for the phone (from POST /devices).
    device: str
    #: The app is in front.
    foreground: bool = True
    #: The sessions of the chat or room on screen (live and stored ids both fine).
    sessions: list[str] = []
    ttl: float = 45.0


class RestartIn(BaseModel):
    # Anything else (a unit, a command) is refused: only this dashboard's own unit and Hermes's gateway restart run.
    model_config = {"extra": "forbid"}

    #: "now", or "idle": once no chat is running a turn, or not at all after `idle_timeout_s`.
    when: Literal["now", "idle"]
    idle_timeout_s: int = Field(600, ge=30, le=1800)
    #: Restart the messaging gateway too (first, through Hermes's own gateway restart).
    include_gateway: bool = False


class KanbanAnswerIn(BaseModel):
    choice: Literal["once", "session", "always", "deny"]


class TaildropIn(BaseModel):
    model_config = {"extra": "forbid"}

    #: One of GET /taildrop's targets, by name or Tailscale address.
    target: str = Field(min_length=1, max_length=255)
    #: A file, exactly as for GET /api/fs/download (with its `profile` and `session_id`)...
    path: str | None = Field(None, max_length=4096)
    profile: str | None = Field(None, max_length=64)
    session_id: str | None = Field(None, max_length=256)
    #: ...or a patch's text, sent as `<name>.patch`.
    diff: str | None = Field(None, max_length=16 * 1024 * 1024)
    #: The file name the device receives.
    name: str | None = Field(None, max_length=255)


def _public(row: dict) -> dict:
    """A device without its keys: the app needs to recognise it, never to read the secrets back."""
    return {
        "id": row.get("id"),
        "name": row.get("name", ""),
        "host": urlsplit(row.get("endpoint", "")).hostname,
        "created": row.get("created"),
        "last_ok": row.get("last_ok"),
        "last_error": row.get("last_error"),
    }


@router.get("/status")
def status():
    store_module, _ = _modules()
    store = store_module.DeviceStore()

    return {"devices": [_public(row) for row in store.devices()], "settings": store.settings(), "vapid": store.vapid_public()}


@router.post("/devices")
def add_device(body: DeviceIn):
    store_module, _ = _modules()

    try:
        row = store_module.DeviceStore().upsert(body.endpoint, body.p256dh, body.auth, body.name)
    except ValueError as failure:
        raise HTTPException(status_code=422, detail=str(failure))

    return _public(row)


@router.delete("/devices/{identity}")
def remove_device(identity: str):
    store_module, _ = _modules()

    if not store_module.DeviceStore().remove(identity):
        raise HTTPException(status_code=404, detail="No such device")

    return {"removed": identity}


@router.put("/settings")
def change_settings(body: SettingsIn):
    store_module, _ = _modules()

    try:
        return store_module.DeviceStore().set_settings(body.default, body.bots)
    except ValueError as failure:
        raise HTTPException(status_code=422, detail=str(failure))


@router.put("/presence")
def presence(body: PresenceIn):
    """The phone is in front (repeated every 20 s while it is), showing these sessions: while it is, a finished reply is not
    pushed (Home shows it), nor anything for the chat on screen. Lapses on its own when the phone stops repeating it."""
    store_module, _ = _modules()

    try:
        store_module.DeviceStore().set_presence(body.device, body.sessions, body.foreground, body.ttl)
    except ValueError as failure:
        raise HTTPException(status_code=404 if "unknown" in str(failure) else 422, detail=str(failure))

    return {"ok": True}


@router.post("/test")
def test(body: TestIn):
    """Send a real push to one device (or all) and report what the push service answered."""
    store_module, notify = _modules()
    store = store_module.DeviceStore()
    pusher = notify.Pusher(store, background=False)
    payload = notify.build("test", profile="default", bot="Hermes", body="Notifications are working", key=str(time.time()), url="hermesbot://inbox")

    if body.device is not None and not any(row.get("id") == body.device for row in store.devices()):
        raise HTTPException(status_code=404, detail="No such device")

    results = [
        {"device": r.device, "ok": r.ok, "status": r.status, "error": r.error, "gone": r.gone}
        for r in pusher.deliver("test", payload, only=body.device)
    ]

    return {"results": results}


@router.get("/needs-you")
def needs_you() -> dict:
    """Every prompt waiting on the person across the bots this dashboard serves: `{live, version, requests: [{id, method,
    session_id, session_key, profile, title, created_at, params}], kanban: [...]}`, oldest first. `params` is the prompt as
    Hermes sent it (an approval's command already redacted). `kanban` is the approvals kanban workers wait on: `{id,
    profile, chat, task: {id, title, board, created_by}, command, description, allow_session, allow_permanent,
    created_at}`, where `chat` is the chat to show it in ("" for the bot's main chat). With `live`, the same body is pushed
    as the `/api/ws` event `plugin.hermes-bot-mobile.needs_you.changed` whenever it changes, so the app reads this only when
    it connects."""
    start_needs_you()

    return _needs_you().snapshot()


@router.post("/kanban-approvals/{request_id}")
def answer_kanban_approval(request_id: str, body: KanbanAnswerIn):
    """Answers an approval a kanban worker waits on: `{answered: true, id, choice}`; 404 when it no longer waits (timed out,
    or the worker stopped), 409 when it was answered already, 400 for a choice it doesn't offer. The worker picks the
    answer up within a second."""
    try:
        result = _kanban_module().KanbanApprovals().answer(request_id, body.choice)
    except _kanban_module().Refusal as refusal:
        return _refusal(refusal)

    if start_needs_you():
        _needs_you().poke()

    return result


@router.get("/rooms/{room_id}/activity")
def room_activity(room_id: str) -> dict:
    """What each bot in this room is doing right now: `{members: {member_id: {phase, tool, since}}, now}`.

    `phase` is `tool` (with the tool's name), `thinking`, `writing` or `approval`. Only bots the room says are busy are
    worth showing: a phase stays until the bot's next one."""
    activity = _activity()

    if not activity.ID.match(room_id):
        raise HTTPException(status_code=400, detail="That is not a room id.")

    return {"members": activity.RoomActivity().read(room_id), "now": time.time()}


@router.get("/runs-on/key")
def runs_on_key(profile: str) -> dict:
    """The bot's public SSH line, or `{public: null}` before one is made. (A 404 here means an older plugin.)"""
    keys = _keys()
    profile = _known_bot(profile)

    return {"public": keys.public(profile), "path": str(keys.path(profile))}


@router.post("/runs-on/key")
def make_runs_on_key(body: KeyIn) -> dict:
    """Makes the bot's key pair on this server if it has none: `{public, path, created}`. The private key stays here."""
    keys = _keys()
    profile = _known_bot(body.profile)

    try:
        return keys.make(profile)
    except FileNotFoundError as failure:
        raise HTTPException(status_code=503, detail=str(failure))
    except Exception as failure:  # ssh-keygen failed: say so, never a stack trace
        raise HTTPException(status_code=500, detail=f"Could not make a key: {failure}")


@router.post("/runs-on/check")
def check_runs_on(body: CheckIn) -> dict:
    """Connects the way the bot will: `{ok: true, system}` or `{ok: false, reason, detail}`. `reason` is one of auth,
    too_many_keys, host_key, unknown_host, refused, timeout, unreachable, key_missing, no_ssh, no_docker, docker_down
    or other."""
    runs_on = _runs_on()
    _known_bot(body.profile)

    if body.backend == "docker":
        return runs_on.check_docker()

    if body.backend != "ssh":
        raise HTTPException(status_code=422, detail="Only ssh and docker can be checked.")

    try:
        return runs_on.check_ssh(body.host, body.user, body.port, body.key)
    except runs_on.Invalid as failure:
        raise HTTPException(status_code=422, detail=str(failure))


@router.get("/restart")
def restart_status() -> dict:
    """Whether this dashboard can be restarted from the app, and what a restart would cut short. Read only.

    `{supported, why, unit, scope, pid, started_at, now, busy, gateway: {route, restart, same_unit}, last, retry_after}`:
    `unit` is this process's own systemd unit and `scope` is `user` or `system`; `busy` lists the chats running a turn
    (`[{id, session, title, profile, status}]`, or null when they cannot be seen); `gateway.route` says Hermes serves
    `POST /api/gateway/restart`; `last` is the last request and what became of it (`done` once this dashboard is the
    restarted one)."""
    return _restarter().status()


@router.post("/restart", status_code=202)
def restart(body: RestartIn):
    """Restart this dashboard: `{when: now|idle, idle_timeout_s, include_gateway}` -> 202 `{accepted, id, mode, unit,
    include_gateway}`. 409 when it cannot be restarted from here or a restart is under way, 429 with `retry_after`
    within a minute of the last restart."""
    restart_module = _restart()

    try:
        return _restarter().request(body.when, body.idle_timeout_s, body.include_gateway)
    except restart_module.Refused as refusal:
        return _refusal(refusal)


@router.delete("/restart")
def cancel_restart():
    """Cancel the restart waiting for the chats to finish: `{cancelled: true, id}`; 404 when none waits, 409 once it is
    under way."""
    restart_module = _restart()

    try:
        return _restarter().cancel()
    except restart_module.Refused as refusal:
        return _refusal(refusal)


@router.get("/taildrop")
def taildrop_status(request: Request) -> dict:
    """Whether this server can Taildrop, and to which devices: `{supported, why, targets: [{name, ip, online, detail,
    you}], you: {name, os, ip} | null, setup: {needed: "operator", user, command} | null, max_bytes, sending}`. `you` is
    the device asking (its address as the dashboard saw it, after Hermes's own trusted-proxy handling)."""
    return _sender().status(request.client.host if request.client else None)


@router.post("/taildrop", status_code=202)
async def taildrop_send(body: TaildropIn):
    """Send a file the dashboard would let this person download (`path`, `profile`, `session_id`, checked by Hermes's own
    download check), or a patch (`diff`), to one of the targets: 202 `{id, state, ...}`."""
    taildrop = _taildrop()
    sender = _sender()

    try:
        if (body.path is None) == (body.diff is None):
            raise taildrop.Refused(422, "Send either a file (`path`) or a patch (`diff`).")

        source = await sender.check(body.path, body.profile, body.session_id) if body.path is not None else None

        return await asyncio.to_thread(sender.send, body.target, source=source, diff=body.diff, name=body.name)
    except taildrop.Refused as refusal:
        return _refusal(refusal)


@router.get("/taildrop/{send_id}")
def taildrop_progress(send_id: str):
    """`{id, state: running|done|failed|cancelled, sent_bytes, total_bytes, error, note, setup, ...}`."""
    taildrop = _taildrop()

    try:
        return _sender().progress(send_id)
    except taildrop.Refused as refusal:
        return _refusal(refusal)


@router.delete("/taildrop/{send_id}")
def taildrop_cancel(send_id: str):
    """Stop a running send (its CLI is terminated): `{cancelled: true, id}`."""
    taildrop = _taildrop()

    try:
        return _sender().cancel(send_id)
    except taildrop.Refused as refusal:
        return _refusal(refusal)


# The watch starts with the dashboard, not with the app's first read: a prompt opened before the app connects is pushed
# the same way. Tests load this file without a gateway, where this does nothing.
start_needs_you()
