"""What the phone app has on screen right now, so this server watches only that.

The app tells `PUT /api/plugins/hermes-bot-mobile/watch` which dashboards, records, rooms and phone errands are on its
screen whenever that changes, and repeats it while it stays in front; a client that stops repeating lapses after its `ttl`.
Other code asks `watched("rooms")` for the union over every app connected: the live room relay (`relay.py`) reads the live
text only of rooms someone looks at, and hermes-dashboard re-reads live panel sources only for dashboards someone looks at.

The plugin is loaded twice in the dashboard process (once for its hooks, once for its routes), and other plugins read the
same set, so the state lives in one module object kept in `sys.modules` under `REGISTRY`, created by whoever comes first.
"""

from __future__ import annotations

import re
import sys
import threading
import time
import types
from typing import Any, Iterable

#: The shared module's name. hermes-dashboard reads `watched("dashboards")` from it when this plugin is installed.
REGISTRY = "hermes_mobile_watch"
KINDS = ("dashboards", "records", "rooms", "errands")
#: Ids as Hermes and the plugins allow them (bot names, record ids, room ids, errand ids).
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
CLIENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
#: A screen shows a handful of things; more than this is a client bug, not a screen.
MAX_ITEMS = 50
MAX_CLIENTS = 32
DEFAULT_TTL = 90.0
MAX_TTL = 600.0

_create_lock = threading.Lock()


def registry() -> types.ModuleType:
    """The one shared state object in this process (made on first use)."""
    module = sys.modules.get(REGISTRY)

    if module is None:
        with _create_lock:
            module = sys.modules.get(REGISTRY)

            if module is None:
                module = types.ModuleType(REGISTRY)
                module.lock = threading.Lock()
                module.clients = {}
                module.watched = watched
                sys.modules[REGISTRY] = module

    return module


def _ids(values: Any) -> list[str]:
    if values is None:
        return []

    if not isinstance(values, (list, tuple)):
        raise ValueError("Each kind is a list of ids.")

    ids = [str(value) for value in values]

    if len(ids) > MAX_ITEMS:
        raise ValueError(f"At most {MAX_ITEMS} ids per kind.")

    bad = [value for value in ids if not ID.match(value)]

    if bad:
        raise ValueError(f"Not an id: {bad[0][:40]}")

    return sorted(set(ids))


def put(client: str, watch: dict, ttl: float | None = None, now: float | None = None) -> dict:
    """Replace what `client` watches. An empty watch (nothing on screen) removes the client. Returns what is kept."""
    if not isinstance(client, str) or not CLIENT.match(client):
        raise ValueError("That is not a client id.")

    kept = {kind: _ids((watch or {}).get(kind)) for kind in KINDS}
    seconds = min(max(float(ttl if ttl is not None else DEFAULT_TTL), 5.0), MAX_TTL)
    now = time.time() if now is None else now
    state = registry()

    with state.lock:
        _expire(state, now)

        if not any(kept.values()):
            state.clients.pop(client, None)
        else:
            if client not in state.clients and len(state.clients) >= MAX_CLIENTS:
                raise ValueError("Too many clients are watching.")

            state.clients[client] = {"watch": kept, "until": now + seconds}

    return kept


def _expire(state: types.ModuleType, now: float) -> None:
    for client in [key for key, value in state.clients.items() if value["until"] <= now]:
        del state.clients[client]


def watched(kind: str, now: float | None = None) -> set[str]:
    """Everything of `kind` some app has on screen now."""
    if kind not in KINDS:
        return set()

    state = registry()
    now = time.time() if now is None else now
    found: set[str] = set()

    with state.lock:
        _expire(state, now)

        for value in state.clients.values():
            found.update(value["watch"].get(kind, ()))

    return found


def anyone(now: float | None = None) -> bool:
    """Whether any app has anything on screen (the relay idles otherwise)."""
    state = registry()
    now = time.time() if now is None else now

    with state.lock:
        _expire(state, now)

        return bool(state.clients)


def clear() -> None:
    """Forget every client (tests)."""
    state = registry()

    with state.lock:
        state.clients.clear()


def ids_in(values: Iterable[Any]) -> list[str]:
    """`values` that are valid ids (a helper for callers filtering what they were handed)."""
    return [str(value) for value in values if ID.match(str(value))]
