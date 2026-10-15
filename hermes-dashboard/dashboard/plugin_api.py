"""The phone's side of hermes-dashboard: each bot's dashboard computed, a record with its history, and the person's actions
on a record (change fields, add a note, close or reopen). The person never creates records or views; bots do.

Mounted by Hermes under `/api/plugins/hermes-dashboard/`, behind the dashboard's own sign-in. Hermes imports this file on
its own, apart from the plugin package, so it loads the package by path and shares the database with the bots' tools.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

_PACKAGE = "_hermes_dashboard_package"


def _package():
    if _PACKAGE not in sys.modules:
        root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location(_PACKAGE, root / "__init__.py", submodule_search_locations=[str(root)])
        module = importlib.util.module_from_spec(spec)
        module.__package__ = _PACKAGE
        module.__path__ = [str(root)]
        sys.modules[_PACKAGE] = module
        spec.loader.exec_module(module)

    return importlib.import_module(f"{_PACKAGE}.store"), importlib.import_module(f"{_PACKAGE}.tools")


def _store():
    S, T = _package()

    return S, T.open_store()


def _version() -> str:
    try:
        import yaml

        manifest = yaml.safe_load((Path(__file__).resolve().parent.parent / "plugin.yaml").read_text(encoding="utf-8")) or {}
        return str(manifest.get("version") or "")
    except Exception:
        return ""


def _guard(call):
    S, store = _store()

    try:
        return call(S, store)
    except S.NotFound as error:
        raise HTTPException(status_code=404, detail=str(error)) from None
    except S.DashboardError as error:
        raise HTTPException(status_code=400, detail=str(error)) from None


router = APIRouter()


@router.get("/status")
async def status() -> dict:
    return _guard(lambda S, store: {"version": _version(), "schema": S.SCHEMA_VERSION, "bots": len(store.summary())})


@router.get("/bots")
async def bots() -> dict:
    return _guard(lambda S, store: {"bots": store.summary(), "now": int(time.time())})


@router.get("/bots/{profile}")
async def bot(profile: str, since: str | None = None, fresh: bool = False) -> dict:
    """A bot's dashboard. With `since` (the version it last answered) and nothing changed: `{unchanged, version}`, and
    no view is computed. `fresh` reads live sources now instead of from their short cache (the app's pull to refresh).
    `installed` says whether that bot has the plugin (None when it can't be told)."""

    def answer(S, store):
        if since and not fresh:
            version = store.version(profile)

            if version == since:
                return {"unchanged": True, "version": version, "now": int(time.time())}

        board = store.dashboard(profile, fresh=fresh)
        # Whether the bot can keep a dashboard at all: without the plugin in its own profile it has no dashboard_* tools.
        board["installed"] = S.L.plugin_installed(store.root, profile)

        return board

    return _guard(answer)


@router.get("/bots/{profile}/needs-you")
async def bot_needs_you(profile: str) -> dict:
    return _guard(lambda S, store: {"profile": profile, "records": store.needs_you_records(profile), "now": int(time.time())})


@router.get("/records/{record_id}")
async def record(record_id: str) -> dict:
    return _guard(lambda S, store: {**store.get_record(S.Actor.person(), record_id), "now": int(time.time())})


class Values(BaseModel):
    values: dict


@router.patch("/records/{record_id}")
async def change(record_id: str, body: Values) -> dict:
    return _guard(lambda S, store: store.update(S.Actor.person(), record_id, body.values))


class Note(BaseModel):
    text: str


@router.post("/records/{record_id}/notes")
async def note(record_id: str, body: Note) -> dict:
    return _guard(lambda S, store: store.add_note(S.Actor.person(), record_id, body.text))


class Mute(BaseModel):
    until: int | None = None


@router.post("/records/{record_id}/mute")
async def mute(record_id: str, body: Mute) -> dict:
    """Mutes a record until `until` (epoch seconds), or unmutes it when `until` is null."""
    return _guard(lambda S, store: store.mute(S.Actor.person(), record_id, body.until))


class Press(BaseModel):
    session: str | None = None


@router.post("/records/{record_id}/actions/{action_id}")
async def press(record_id: str, action_id: str, body: Press) -> dict:
    """A button on a record: answers the message for the bot, which the app sends in the side chat `session`."""
    return _guard(lambda S, store: store.start_action(S.Actor.person(), record_id, action_id, body.session))


@router.post("/records/{record_id}/close")
async def close(record_id: str) -> dict:
    return _guard(lambda S, store: store.set_closed(S.Actor.person(), record_id, True))


@router.post("/records/{record_id}/reopen")
async def reopen(record_id: str) -> dict:
    return _guard(lambda S, store: store.set_closed(S.Actor.person(), record_id, False))
