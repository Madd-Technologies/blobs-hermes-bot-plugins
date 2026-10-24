"""Room changes pushed to the phone app the moment they happen, so the app stops asking every room on a timer.

Runs in the dashboard (the process the app is connected to), on one small thread, and sends two events over `/api/ws`
with Hermes's `broadcast_plugin_event`:

- `plugin.hermes-bot-mobile.rooms.changed` `{rooms: {room_id: {latest_seq, revision, updated_at}}}`: whenever any room's
  log or settings moved. The room store (`shared-state.db`) is asked SQLite's `data_version` every `ROOMS_EVERY` seconds,
  which costs nothing while nothing was written, and the rooms are listed only when it moved. A turn starting and
  settling are both log events, so "who is replying" changes with them too: the app reads a room in full only then.
- `plugin.hermes-bot-mobile.room.live` `{room_id, members, at}`: the members' turns as they stream (`live.py`), for the rooms
  an app has on screen (`watch.py`). Their files are looked at (a stat) every `LIVE_EVERY` seconds.

Nothing is sent while nothing changes; a process without Hermes's gateway (tests, `hermes plugins validate`) sends nothing.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

try:
    from . import live, store, watch
except ImportError:  # the tests import the modules flat
    import live  # type: ignore[no-redef]
    import store  # type: ignore[no-redef]
    import watch  # type: ignore[no-redef]

log = logging.getLogger("hermes-bot-mobile")

PLUGIN_ID = "hermes-bot-mobile"
LIVE_EVERY = 0.2
ROOMS_EVERY = 1.0


def _hermes_broadcast(event: str, payload: dict) -> None:
    from hermes_cli.plugin_events import broadcast_plugin_event

    broadcast_plugin_event(PLUGIN_ID, event, payload)


def _hermes_rooms_db() -> Optional[Path]:
    from gateway.hosted_rooms import default_db_path

    return Path(default_db_path())


def _hermes_list_rooms(db_path: Path) -> list[dict]:
    from gateway.hosted_rooms import list_rooms

    return list(list_rooms(db_path, include_disbanded=False))


class RoomRelay:
    def __init__(
        self,
        folder: Optional[Path] = None,
        broadcast: Callable[[str, dict], None] = _hermes_broadcast,
        rooms_db: Callable[[], Optional[Path]] = _hermes_rooms_db,
        list_rooms: Callable[[Path], list[dict]] = _hermes_list_rooms,
        watched: Callable[[str], set] = watch.watched,
        clock: Callable[[], float] = time.time,
    ):
        self._folder = Path(folder) if folder else None
        self._broadcast = broadcast
        self._rooms_db = rooms_db
        self._list_rooms = list_rooms
        self._watched = watched
        self._clock = clock
        #: room -> the live file's (mtime_ns, size) last sent, or None once an empty room was sent.
        self._live_seen: dict[str, Optional[tuple[int, int]]] = {}
        self._conn: Optional[sqlite3.Connection] = None
        self._data_version: Optional[int] = None
        self._rooms_sent: Optional[dict[str, dict]] = None
        self._next_rooms = 0.0
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def _dir(self) -> Path:
        return self._folder or store.default_dir()

    # ── live text ────────────────────────────────────────────────────────────────────────────────

    def tick_live(self) -> int:
        """Send the live turns of watched rooms whose file changed. Returns how many events went out."""
        sent = 0
        rooms = {room for room in self._watched("rooms") if live.ID.match(room)}

        for room in list(self._live_seen):
            if room not in rooms:
                del self._live_seen[room]

        for room in rooms:
            path = live.live_path(self._dir(), room)

            try:
                stat = path.stat()
                mark: Optional[tuple[int, int]] = (stat.st_mtime_ns, stat.st_size)
            except FileNotFoundError:
                mark = None

            if room in self._live_seen and self._live_seen[room] == mark:
                continue

            members: dict[str, Any] = {}
            at = self._clock()

            if mark is not None:
                try:
                    data = json.loads(path.read_text(encoding="utf-8") or "{}")
                    members = data.get("members") or {}
                    at = float(data.get("at") or at)
                except (OSError, ValueError):
                    continue

            # A room first seen with nothing live needs no event: the app shows nothing either way.
            if room not in self._live_seen and not members:
                self._live_seen[room] = mark
                continue

            self._live_seen[room] = mark
            self._broadcast("room.live", {"room_id": room, "members": members, "at": at})
            sent += 1

        return sent

    # ── room list ────────────────────────────────────────────────────────────────────────────────

    def _moved(self, db_path: Path) -> bool:
        """Whether anything wrote the room store since the last look (SQLite's data_version, on one kept connection)."""
        try:
            if self._conn is None:
                self._conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False, timeout=1.0)

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

    def tick_rooms(self) -> bool:
        """Send `rooms.changed` when a room's log or settings moved. True when it went out."""
        db_path = self._rooms_db()

        if db_path is None or not Path(db_path).exists() or not self._moved(Path(db_path)):
            return False

        rooms = {
            str(row.get("room_id")): {"latest_seq": int(row.get("latest_seq") or 0), "revision": int(row.get("revision") or 0), "updated_at": row.get("updated_at")}
            for row in self._list_rooms(Path(db_path))
            if row.get("room_id")
        }

        if rooms == self._rooms_sent:
            return False

        self._rooms_sent = rooms
        self._broadcast("rooms.changed", {"rooms": rooms})

        return True

    # ── thread ───────────────────────────────────────────────────────────────────────────────────

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return False

        self._thread = threading.Thread(target=self._run, name="hermes-bot-mobile-relay", daemon=True)
        self._thread.start()

        return True

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(LIVE_EVERY):
            try:
                self.tick_live()
            except Exception:
                log.exception("hermes-bot-mobile: relaying live room turns failed")

            if self._clock() >= self._next_rooms:
                self._next_rooms = self._clock() + ROOMS_EVERY

                try:
                    self.tick_rooms()
                except Exception:
                    log.exception("hermes-bot-mobile: relaying room changes failed")
                    self._close()
