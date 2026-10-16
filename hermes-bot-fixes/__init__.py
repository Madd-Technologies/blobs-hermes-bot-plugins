"""hermes-bot-fixes: local fixes for Hermes 0.21.x on a host that runs Bot Screens.

- computer_use screenshots that name no window skip the desktop's panels, and the screenshot after an action waits a
  moment for the screen to settle (`capture.py`).
- A guard repairs SQLite files whose header a stray TLS record overwrote, checking a copy first (`dbguard.py`).
- After a compaction, a request the bot already answered before a background result woke it is not handed back to it as
  unfinished work; the result is (`compaction.py`).
- A background task that comes back unfinished is handed to a new background worker (a kanban card for the bot itself, or
  delegate_task), not finished by the bot in the chat the person talks to it in (`handoff.py`).

Install it for the default profile only: the fixes apply to the whole process, so every bot it runs is covered.
Nothing here edits Hermes's own files; each fix checks that the code it wraps is the one it expects.
"""

from __future__ import annotations

import logging

log = logging.getLogger("hermes-bot-fixes")


def register(ctx) -> None:
    from . import capture, compaction, dbguard, handoff

    applied = capture.apply()
    restatement = compaction.apply()
    handing_off = handoff.apply()

    def on_tool(**kw):
        # computer_use may only be importable once a turn first uses it; apply what is still missing then.
        try:
            if kw.get("tool_name") == "computer_use" and not all(applied.values()):
                applied.update({key: value for key, value in capture.apply().items() if value})
        except Exception:
            log.exception("hermes-bot-fixes: applying the computer_use fixes failed")

    ctx.register_hook("pre_tool_call", on_tool)
    started = dbguard.start()
    log.info("hermes-bot-fixes: panels %s, settle %s, compaction %s, hand-off %s, database guard %s", applied["panels"], applied["settle"], restatement, handing_off, "started" if started else "already running")
