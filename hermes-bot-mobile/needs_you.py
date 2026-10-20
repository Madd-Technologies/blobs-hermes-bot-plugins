"""What the person must answer right now, across every bot this dashboard serves, and a push to the phone app the
moment that changes.

Hermes 0.21.5 keeps every prompt it has put to a client (an approval, a question, sudo, a secret, a vault prompt) in one
table in this process, `tui_gateway.server_requests._open`, shared by every session and profile the dashboard serves: the
app's own chats, other clients' chats and the members of group rooms. Hermes has no event for a prompt opening or closing
(`sessions.changed` follows state.db, which a prompt never touches), so the app used to ask about every busy chat on a
timer. Here the table is swapped for one that says when it changes, and a small thread sends
`plugin.hermes-bot-mobile.needs_you.changed`, with the whole list, to every app connected over `/api/ws`. Nothing polls.

Every write to the table is inside `server_requests.py`, through the module's own name for it, and the other modules
import the module rather than the table, so the swap is seen everywhere. Prompts the messaging gateway asks (Telegram and
the like) live in that process, and with `dashboard.turn_isolation` on, a turn's prompts live in its compute-host child:
neither is seen here, and the app keeps its slow check for those servers.

A kanban worker's approvals wait in another process altogether: its copy of this plugin writes them to files
(`kanban_approvals.py`), and the watch looks at that folder every second, so they go out in the same event under `kanban`.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

log = logging.getLogger("hermes-bot-mobile")

PLUGIN_ID = "hermes-bot-mobile"
EVENT = "needs_you.changed"

#: The prompts a person answers. The others in the table (`terminal.read`, `preview.read`, `window.read`, `preview.act`,
#: `tour`) are the agent asking the client for something, not asking you.
HUMAN = ("approval", "clarify", "sudo", "secret", "vault.unlock_prompt", "vault.save_login", "vault.code")

#: Changes that land this close together go out as one event: a batch of questions, or a prompt answered as the next opens.
COALESCE_S = 0.05

#: How often the watch looks at the kanban workers' approvals, which have no event of their own.
KANBAN_POLL_S = 1.0


class WatchedTable(dict):
    """The open-requests table, calling `changed` after every write. It runs under Hermes's own lock, so it must only
    flag the change: `NeedsYou` does the work on its own thread."""

    def __init__(self, items=(), changed: Callable[[], None] = lambda: None):
        super().__init__(items)
        self.changed = changed

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.changed()

    def __delitem__(self, key):
        super().__delitem__(key)
        self.changed()

    def pop(self, key, *default):
        had = key in self
        value = super().pop(key, *default)

        if had:
            self.changed()

        return value

    def popitem(self):
        item = super().popitem()
        self.changed()

        return item

    def clear(self):
        had = bool(self)
        super().clear()

        if had:
            self.changed()

    def update(self, *args, **kwargs):
        super().update(*args, **kwargs)
        self.changed()

    def setdefault(self, key, default=None):
        had = key in self
        value = super().setdefault(key, default)

        if not had:
            self.changed()

        return value


def _hermes_requests():
    from tui_gateway import server_requests

    return server_requests


def _hermes_broadcast(payload: dict) -> None:
    from hermes_cli.plugin_events import broadcast_plugin_event

    broadcast_plugin_event(PLUGIN_ID, EVENT, payload)


def _hermes_session(sid: str) -> dict:
    """The bot a live session belongs to, its stored id and its title, from the dashboard's own session table. A group
    room member's hidden session is titled `Group: <room_id>`: the app shows those prompts on the room, not the bot."""
    try:
        from tui_gateway import server
    except Exception:
        return {"profile": "", "session_key": "", "title": ""}

    session = server._sessions.get(sid) or {}
    key = str(getattr(session.get("agent"), "session_id", None) or session.get("session_key") or "")
    profile = title = ""

    try:
        from hermes_constants import profile_name_for_home

        profile = profile_name_for_home(session.get("profile_home")) or ""
    except Exception:
        pass

    try:
        title = server._session_live_title(session, key) if key else str(session.get("pending_title") or "")
    except Exception:
        pass

    return {"profile": profile, "session_key": key, "title": title}


class NeedsYou:
    """Watches the prompts waiting on the person and broadcasts the list whenever it changes."""

    def __init__(
        self,
        requests: Optional[Callable[[], Any]] = None,
        broadcast: Optional[Callable[[dict], None]] = None,
        session_of: Optional[Callable[[str], dict]] = None,
        coalesce_s: float = COALESCE_S,
        kanban=None,
        kanban_poll_s: float = KANBAN_POLL_S,
    ):
        self._requests = requests or _hermes_requests
        self._broadcast = broadcast or _hermes_broadcast
        self._session_of = session_of or _hermes_session
        self._coalesce_s = coalesce_s
        #: The kanban workers' approvals (`kanban_approvals.KanbanApprovals`), or None.
        self._kanban = kanban
        self._kanban_poll_s = kanban_poll_s
        self._kanban_seen: tuple = ()
        self._dirty = threading.Event()
        self._lock = threading.Lock()
        self._module = None
        self._version = 0
        self._last: tuple = ((), ())
        self._thread: Optional[threading.Thread] = None

    @property
    def live(self) -> bool:
        """Whether changes are being pushed (False on a Hermes without the table, or before `install`)."""
        return self._module is not None

    def install(self) -> bool:
        """Starts watching. Safe to call more than once and from any thread; False when this Hermes has no table."""
        with self._lock:
            if self._module is not None:
                return True

            try:
                module = self._requests()
                table = module._open
                hermes_lock = module._lock
            except Exception as error:
                log.info("hermes-bot-mobile: no open-requests table on this Hermes, Needs you stays on the app's check (%s)", error)

                return False

            with hermes_lock:
                if isinstance(table, WatchedTable):
                    # Already swapped in this process (the dashboard imported this file twice): share it.
                    table.changed = self._dirty.set
                else:
                    module._open = WatchedTable(table, changed=self._dirty.set)

            self._module = module
            self._thread = threading.Thread(target=self._run, name="hermes-bot-mobile-needs-you", daemon=True)
            self._thread.start()

        self._dirty.set()

        return True

    def snapshot(self) -> dict:
        """What waits on the person now, read fresh. `version` counts the changes pushed so far by this process."""
        return self._body(self.live, self._read(), self._read_kanban())

    def poke(self) -> None:
        """Something changed that the watch can't see at once (an answer to a kanban approval): look now."""
        self._dirty.set()

    def publish(self) -> bool:
        """Pushes the list if it changed since the last push. Only the watch thread calls it, so a read for the route
        never hides a change from it. True when something was sent."""
        if self._kanban is not None:
            # Read before the list, so a change landing in between is seen on the next look.
            self._kanban_seen = self._kanban.signature()

        requests = self._read()
        kanban = self._read_kanban()
        # A batch of questions answered one by one changes only its locked answers: that is a change too.
        key = (tuple((entry["id"], tuple(sorted((entry["params"].get("answers") or {}).items()))) for entry in requests), tuple(entry["id"] for entry in kanban))

        if key == self._last:
            return False

        self._last = key
        self._version += 1
        self._broadcast(self._body(True, requests, kanban))

        return True

    def _body(self, live: bool, requests: list[dict], kanban: list[dict]) -> dict:
        body = {"live": live, "version": self._version, "requests": requests}

        if self._kanban is not None:
            body["kanban"] = kanban

        return body

    def _read_kanban(self) -> list[dict]:
        if self._kanban is None:
            return []

        try:
            return self._kanban.pending()
        except Exception:
            log.debug("hermes-bot-mobile: could not read the kanban approvals", exc_info=True)

            return []

    def _read(self) -> list[dict]:
        module = self._module

        if module is None:
            return []

        with module._lock:
            waiting = [request for request in module._open.values() if request.method in HUMAN]

        entries = []

        for request in sorted(waiting, key=lambda r: r.created_at):
            params = dict(request.snapshot().get("params") or {})
            params.pop("session_id", None)
            session = self._session_of(request.sid)
            entries.append(
                {
                    "id": request.id,
                    "method": request.method,
                    "session_id": request.sid,
                    "session_key": session.get("session_key", ""),
                    "profile": session.get("profile", ""),
                    "title": session.get("title", ""),
                    "created_at": request.created_at,
                    "params": params,
                }
            )

        return entries

    def _run(self) -> None:
        while True:
            if self._dirty.wait(self._kanban_poll_s if self._kanban is not None else None):
                time.sleep(self._coalesce_s)
                self._dirty.clear()
            elif self._kanban.signature() == self._kanban_seen:
                continue

            try:
                self.publish()
            except Exception:
                # A broadcast that fails (no app connected, a peer gone) must not end the watch.
                log.debug("hermes-bot-mobile: needs-you broadcast failed", exc_info=True)
