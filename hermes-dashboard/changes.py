"""Dashboard changes pushed to the phone app the moment they happen, so the app stops asking on a timer.

Runs in the dashboard (the process the app is connected to), on one small thread, and sends
`plugin.hermes-dashboard.changed` `{versions: {bot: version}, summary?}` over `/api/ws` with Hermes's
`broadcast_plugin_event` whenever a bot's dashboard version moved; `summary` (each bot's needs-you count, panels and last
change, what Home draws) comes along when the store was written. The app reloads a dashboard only when its version moved.

- Records and panels are written by bots' tools, script feeds and the person, in any process: the store is asked SQLite's
  `data_version` every `EVERY` seconds, which costs nothing while nothing was written.
- Live panels read sources outside the store. They are read again (at their ttl, or for a file only once it changed) only
  for dashboards an app has on screen, as hermes-bot-mobile's watch says (`sys.modules["hermes_mobile_watch"]`); others keep
  their last read until someone looks.
- Background work follows the kanban board here too (`kanban.sync`, throttled), not only when the app asks.

A process without Hermes's gateway (tests, `hermes plugins validate`) sends nothing.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("hermes-dashboard")

PLUGIN_ID = "hermes-dashboard"
EVENT = "changed"
EVERY = 1.0
#: The summary is the costliest part (it works out every needs-you rule): at most this often.
SUMMARY_EVERY = 2.0
WATCH_REGISTRY = "hermes_mobile_watch"


def _hermes_broadcast(payload: dict) -> None:
    from hermes_cli.plugin_events import broadcast_plugin_event

    broadcast_plugin_event(PLUGIN_ID, EVENT, payload)


def watched_dashboards() -> set:
    """Dashboards an app has on screen, from hermes-bot-mobile when it is installed (none otherwise)."""
    registry = sys.modules.get(WATCH_REGISTRY)

    try:
        return set(registry.watched("dashboards")) if registry is not None else set()
    except Exception:
        return set()


class DashboardChanges:
    def __init__(
        self,
        open_store: Callable[[], Any],
        broadcast: Callable[[dict], None] = _hermes_broadcast,
        watched: Callable[[], set] = watched_dashboards,
        mirror: Optional[Callable[[Any], None]] = None,
        clock: Callable[[], float] = time.time,
    ):
        self._open_store = open_store
        self._broadcast = broadcast
        self._watched = watched
        self._mirror = mirror
        self._clock = clock
        self._conn: Optional[sqlite3.Connection] = None
        self._data_version: Optional[int] = None
        self._sent: dict[str, str] = {}
        self._summary_sent: Optional[dict] = None
        self._summary_due = False
        self._summary_at = 0.0
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def _moved(self, path: Path) -> bool:
        try:
            if self._conn is None:
                self._conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False, timeout=1.0)

            version = int(self._conn.execute("PRAGMA data_version").fetchone()[0])
        except sqlite3.Error:
            self._close()

            return True

        moved = version != self._data_version
        self._data_version = version

        return moved

    def _close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

            self._conn = None
            self._data_version = None

    def tick(self) -> Optional[dict]:
        """One look. Returns the event it sent, or None."""
        store = self._open_store()

        if self._mirror is not None:
            try:
                self._mirror(store)
            except Exception:
                log.debug("hermes-dashboard: kanban mirror failed", exc_info=True)

        path = Path(store.path)

        if not path.exists():
            return None

        moved = self._moved(path)
        watched = self._watched()

        if moved:
            self._summary_due = True
            # Every dashboard the store has, plus those on screen (a live panel's own rows can move without a write).
            with store._db() as conn:
                bots = {row[0] for row in conn.execute("SELECT DISTINCT dashboard FROM views")}
        else:
            bots = set()

        bots |= watched
        versions = {}

        for bot in sorted(bots):
            try:
                versions[bot] = store.version(bot, read_live=bot in watched)
            except Exception:
                log.debug("hermes-dashboard: no version for %s", bot, exc_info=True)

        changed = {bot: version for bot, version in versions.items() if self._sent.get(bot) != version}
        payload: dict = {}

        if changed:
            self._sent.update(changed)
            payload["versions"] = changed

        if self._summary_due and self._clock() - self._summary_at >= SUMMARY_EVERY:
            self._summary_due = False
            self._summary_at = self._clock()
            summary = store.summary()

            if summary != self._summary_sent:
                self._summary_sent = summary
                payload["summary"] = summary

        if not payload:
            return None

        payload["now"] = int(self._clock())
        self._broadcast(payload)

        return payload

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return False

        self._thread = threading.Thread(target=self._run, name="hermes-dashboard-changes", daemon=True)
        self._thread.start()

        return True

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(EVERY):
            try:
                self.tick()
            except Exception:
                log.exception("hermes-dashboard: pushing dashboard changes failed")
                self._close()
