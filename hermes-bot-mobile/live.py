"""Each group-room member's turn as it streams, for the app to show it the way it shows a bot's own chat.

Hermes hands a room member's live events (its reply's text as it streams, its tool steps, an approval it waits on) to the
`on_room_member_activity` hook and to no client: the durable room log only gets the finished reply. `activity.py` keeps
the member's phase from the same hook; this keeps the turn itself: the text so far (its last `MAX_TEXT` characters) and
the tool steps, per room and member, until the next turn or a quiet `FORGET_AFTER`.

In Hermes 0.22 the messaging gateway runs room members' turns, so the hook fires in that process, while the app is
connected to the dashboard. The turn goes to one small file per room (`rooms/<room_id>.live.json` in the plugin's data
folder), written at most every `WRITE_EVERY` seconds while a member writes; `relay.py`, in the dashboard, sends it on to
the apps watching that room. Approvals are only named (Hermes redacts them before the hook); nothing else is kept.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

try:
    from . import store
except ImportError:  # the tests import the modules flat
    import store  # type: ignore[no-redef]

log = logging.getLogger("hermes-bot-mobile")

ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
TOOL = re.compile(r"[^A-Za-z0-9_.:-]")
#: The text kept per turn: its end, which is what the app shows while it streams (the finished reply comes from the log).
MAX_TEXT = 8_000
MAX_STEPS = 40
#: How often a room's file is written while its members stream: a few times a second reads as live.
WRITE_EVERY = 0.25
#: A member nobody has heard from for this long is dropped (a turn that ended without the hook saying so).
FORGET_AFTER = 15 * 60.0

PHASE = {
    "tool.started": "tool",
    "tool.completed": "thinking",
    "reasoning.delta": "thinking",
    "message.delta": "writing",
    "message.interim": "writing",
    "request.opened": "approval",
    "turn.error": "error",
}


def live_path(folder: Path, room_id: str) -> Path:
    return folder / "rooms" / f"{room_id}.live.json"


class RoomLive:
    def __init__(self, folder: Path | None = None, clock: Callable[[], float] = time.time, start_thread: bool = True):
        self._folder = Path(folder) if folder else None
        self._clock = clock
        self._lock = threading.Lock()
        #: room -> member -> the turn so far.
        self._rooms: dict[str, dict[str, dict[str, Any]]] = {}
        self._dirty: set[str] = set()
        self._written: dict[str, float] = {}
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._start_thread = start_thread

    def _dir(self) -> Path:
        return self._folder or store.default_dir()

    def record(self, *, room_id: Any = None, member_id: Any = None, kind: Any = None, payload: Any = None, turn_id: Any = None, **_: Any) -> bool:
        """The hook. Never raises: a broken live view must not cost the member's turn."""
        try:
            room, member, kind = str(room_id or ""), str(member_id or ""), str(kind or "")

            if not ID.match(room) or not ID.match(member) or kind not in PHASE:
                return False

            body = payload if isinstance(payload, dict) else {}
            now = self._clock()

            with self._lock:
                members = self._rooms.setdefault(room, {})
                turn = str(turn_id or "")
                entry = members.get(member)

                if entry is None or entry["turn"] != turn:
                    entry = members[member] = {"turn": turn, "phase": PHASE[kind], "tool": None, "text": "", "cut": False, "steps": [], "started": now, "updated": now}

                self._apply(entry, kind, body)
                entry["updated"] = now
                self._dirty.add(room)

            self._ensure_thread()
            self._wake.set()

            return True
        except Exception:
            log.exception("hermes-bot-mobile: keeping a room member's live turn failed")

            return False

    @staticmethod
    def _apply(entry: dict[str, Any], kind: str, body: dict[str, Any]) -> None:
        entry["phase"] = PHASE[kind]

        if kind == "message.delta":
            entry["text"] += str(body.get("text") or "")
        elif kind == "message.interim":
            # An interim message is a separate message mid-turn; one already streamed came as deltas.
            if not body.get("already_streamed"):
                text = str(body.get("text") or "")
                entry["text"] += ("\n\n" if entry["text"] and text else "") + text
        elif kind == "tool.started":
            name = TOOL.sub("", str(body.get("name") or ""))[:64] or "tool"
            entry["tool"] = name
            entry["steps"].append({"id": str(body.get("tool_id") or "")[:64], "name": name, "done": False})
            del entry["steps"][:-MAX_STEPS]
        elif kind == "tool.completed":
            entry["tool"] = None
            tool_id = str(body.get("tool_id") or "")[:64]

            for step in reversed(entry["steps"]):
                if not step["done"] and (not tool_id or step["id"] == tool_id):
                    step["done"] = True

                    if isinstance(body.get("duration_s"), (int, float)):
                        step["seconds"] = round(float(body["duration_s"]), 1)

                    break

        if len(entry["text"]) > MAX_TEXT:
            entry["text"] = entry["text"][-MAX_TEXT:]
            entry["cut"] = True

    def snapshot(self, room_id: str) -> dict[str, Any]:
        """The room's members as written to its file (also what the tests read)."""
        now = self._clock()

        with self._lock:
            members = self._rooms.get(room_id, {})

            for member in [key for key, value in members.items() if now - value["updated"] > FORGET_AFTER]:
                del members[member]

            return {member: {key: value for key, value in entry.items()} | {"steps": [dict(step) for step in entry["steps"]]} for member, entry in members.items()}

    def flush(self, force: bool = False) -> None:
        """Write every changed room whose last write is at least `WRITE_EVERY` ago (all of them with `force`)."""
        now = self._clock()

        with self._lock:
            due = [room for room in self._dirty if force or now - self._written.get(room, 0.0) >= WRITE_EVERY]

            for room in due:
                self._dirty.discard(room)
                self._written[room] = now

        for room in due:
            members = self.snapshot(room)

            try:
                store.JsonFile(live_path(self._dir(), room), dict).update(lambda data, members=members: (data.clear(), data.update({"members": members, "at": now})))
            except Exception:
                log.exception("hermes-bot-mobile: writing a room's live turn failed")

    def _ensure_thread(self) -> None:
        if not self._start_thread or (self._thread is not None and self._thread.is_alive()):
            return

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return

            self._thread = threading.Thread(target=self._run, name="hermes-bot-mobile-live", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            self._wake.wait(timeout=5.0)
            self._wake.clear()

            try:
                self.flush()
            except Exception:
                log.exception("hermes-bot-mobile: the live room writer failed")

            # Rooms still waiting on their next write: come back when it is due.
            with self._lock:
                pending = bool(self._dirty)

            if pending:
                time.sleep(WRITE_EVERY / 2)
                self._wake.set()
