"""The phone app's side of hermes-bot-mobile: register a device, change how loud each bot is, send a test, and read
what each bot in a group room is doing right now.

Mounted by `hermes serve` under `/api/plugins/hermes-bot-mobile/`, behind the dashboard's own sign-in, so only someone
who can already use the server can reach it. Hermes imports this file on its own, apart from the plugin package,
so it loads the package's modules by path and shares state with the hooks through the files in `store.py`.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

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


class PresenceIn(BaseModel):
    #: The server's id for the phone (from POST /devices).
    device: str
    #: The app is in front.
    foreground: bool = True
    #: The sessions of the chat or room on screen (live and stored ids both fine).
    sessions: list[str] = []
    ttl: float = 45.0


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


@router.get("/rooms/{room_id}/activity")
def room_activity(room_id: str) -> dict:
    """What each bot in this room is doing right now: `{members: {member_id: {phase, tool, since}}, now}`.

    `phase` is `tool` (with the tool's name), `thinking`, `writing` or `approval`. Only bots the room says are busy are
    worth showing: a phase stays until the bot's next one."""
    activity = _activity()

    if not activity.ID.match(room_id):
        raise HTTPException(status_code=400, detail="That is not a room id.")

    return {"members": activity.RoomActivity().read(room_id), "now": time.time()}
