"""What each bot in a group room is doing right now, for the HermesBot app: running a tool, thinking, writing, or
waiting for your OK.

Hermes runs a room member's turn on a hidden session whose tool, streaming and approval events reach no client, and the
room log only says when the turn started and settled. Hermes 0.21.5 hands those live events to the
`on_room_member_activity` plugin hook instead; this keeps, per room, each member's current phase in one small file under
the plugin's data folder, which the dashboard API reads. The hook can fire in the gateway or the dashboard process, so
the file is the shared state, as with the push store.

It is written only when a member's phase (or tool) changes, never for each streamed word, and it keeps no text, tool
arguments or output: only the phase, the tool's name and when the phase began.
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Any

try:
    from . import store
except ImportError:  # the tests import the modules flat
    import store  # type: ignore[no-redef]

#: Hook event kind -> the member's phase.
KIND_PHASE = {
    "tool.started": "tool",
    "tool.completed": "thinking",
    "reasoning.delta": "thinking",
    "message.delta": "writing",
    "message.interim": "writing",
    "request.opened": "approval",
}
#: Kinds after which the member is no longer doing anything visible.
END_KINDS = {"turn.error"}
#: Room and member ids as Hermes allows them (the room id also names the file, so no `/` or leading `.`).
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
#: A tool's name as shown (`terminal`, `web_search`, `mcp__bank__balance`).
TOOL = re.compile(r"[^A-Za-z0-9_.:-]")
#: A phase nobody has updated for this long is forgotten (a turn that never said it ended).
STALE_AFTER = 6 * 3600.0


class RoomActivity:
    def __init__(self, folder: Path | None = None, clock=time.time):
        #: Worked out on first use (the data folder is the store's), so loading the plugin touches nothing.
        self._folder = Path(folder) if folder else None
        self._clock = clock
        #: (room, member) -> the last (phase, tool, turn) written by this process, to skip writing what is already there.
        self._last: dict[tuple[str, str], tuple[str, str | None, str | None] | None] = {}
        self._lock = threading.Lock()

    def _file(self, room_id: str) -> "store.JsonFile":
        return store.JsonFile((self._folder or store.default_dir()) / "rooms" / f"{room_id}.json", dict)

    def record(self, *, room_id: Any = None, member_id: Any = None, kind: Any = None, payload: Any = None, turn_id: Any = None, **_: Any) -> bool:
        """The `on_room_member_activity` hook. True when the member's phase changed and was saved."""
        room, member = str(room_id or ""), str(member_id or "")

        if not ID.match(room) or not ID.match(member):
            return False

        phase = KIND_PHASE.get(str(kind or ""))
        ended = str(kind or "") in END_KINDS

        if phase is None and not ended:
            return False

        tool = None

        if phase == "tool":
            name = (payload or {}).get("name") if isinstance(payload, dict) else None
            tool = TOOL.sub("", str(name or ""))[:64] or None

        turn = str(turn_id) if turn_id else None
        state = None if ended else (phase, tool, turn)

        with self._lock:
            if self._last.get((room, member), ()) == state:
                return False

            self._last[(room, member)] = state

        now = self._clock()

        def change(members: dict) -> None:
            for key in [key for key, row in members.items() if not isinstance(row, dict) or now - float(row.get("updated") or 0) > STALE_AFTER]:
                members.pop(key, None)

            if state is None:
                members.pop(member, None)
                return

            members[member] = {"phase": phase, "tool": tool, "turn": turn, "since": now, "updated": now}

        self._file(room).update(change)

        return True

    def read(self, room_id: str) -> dict[str, dict]:
        """Each member's current phase in this room: `{member_id: {phase, tool, since}}`, stale ones left out."""
        if not ID.match(room_id):
            return {}

        now = self._clock()

        return {
            member: {"phase": row.get("phase"), "tool": row.get("tool"), "since": row.get("since")}
            for member, row in self._file(room_id).read().items()
            if isinstance(row, dict) and row.get("phase") in KIND_PHASE.values() and now - float(row.get("updated") or 0) <= STALE_AFTER
        }
