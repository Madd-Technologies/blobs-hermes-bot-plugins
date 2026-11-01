"""A group room keeps answering after its policy checkpoint trims old messages.

Hermes 0.22's room worker plans each turn from a checkpoint of the room, not the whole room log
(`gateway/hosted_room_policy_checkpoint.py`). The checkpoint keeps a thread's newest 24 messages
(`MAX_THREAD_TRANSCRIPT_EVENTS`) with the turn outcome tied to each kept member message, and deletes every row older than
the 24th. When that cut falls between a person's message and the reply to it, the reply and its `turn.settled` stay while
the message they answer goes, and the planner (`hosted_room_discussion._derive_member_watermarks`) refuses the whole room:
"turn.settled references no matching user discussion". The worker keeps that error to itself (in memory, never logged),
so the room just goes quiet for good from the message that took its thread past 24. Reproduced on Hermes 0.22.4: a
two-bot room stops on its 13th message (`tests/integration_roomtrim.py`).

Here a planner that hits that error plans again without the trimmed conversations' leftovers: member messages and turn
outcomes whose person's message is no longer in the checkpoint. Nothing is lost by it: what each member has seen is kept
apart (the checkpoint's watermarks, which the planner merges in), and every conversation still whole plans as before. Only
that one error is handled; any other is raised as before.
"""

from __future__ import annotations

import functools
import inspect
import logging

log = logging.getLogger("hermes-bot-fixes")

MARK = "__hermes_bot_fixes__"

#: The planner's words when a kept outcome answers a message the checkpoint trimmed.
TRIMMED = "turn.settled references no matching user discussion"

#: What a conversation leaves behind after its person's message: the replies and the turns' outcomes.
LEFTOVERS = frozenset({"message.member", "turn.settled", "turn.failed", "turn.cancelled", "turn.deferred"})

_noted = 0


def without_trimmed(events: list) -> list:
    """The events without leftovers of conversations whose person's message is not among them (oldest first, as given)."""
    present = {event.event_id for event in events if event.kind == "message.user"}

    return [event for event in events if event.kind not in LEFTOVERS or str((event.payload or {}).get("discussion_event_id") or "") in present]


def patch_watermarks(module) -> bool:
    """Wrap the module's `_derive_member_watermarks`. True when the wrap is in place (now or already)."""
    original = getattr(module, "_derive_member_watermarks", None)
    error_type = getattr(module, "DiscussionValidationError", None)

    if not callable(original) or not isinstance(error_type, type):
        log.warning("hermes-bot-fixes: no room watermark planner in this Hermes; room trim fix not applied")

        return False

    if getattr(original, MARK, False):
        return True

    if list(inspect.signature(original).parameters) != ["events"]:
        log.warning("hermes-bot-fixes: the room watermark planner differs in this Hermes; room trim fix not applied")

        return False

    @functools.wraps(original)
    def _derive_member_watermarks(events):
        try:
            return original(events)
        except error_type as error:
            if str(error) != TRIMMED:
                raise

            kept = without_trimmed(list(events))

            if len(kept) == len(events):
                raise

            global _noted

            # Once per process loudly: the same room is planned every few seconds until its next trim moves the cut.
            (log.info if _noted == 0 else log.debug)(
                "hermes-bot-fixes: a room's checkpoint trimmed a conversation part-way; planning without its %d leftover events", len(events) - len(kept)
            )
            _noted += 1

            return original(kept)

    setattr(_derive_member_watermarks, MARK, True)
    module._derive_member_watermarks = _derive_member_watermarks

    return True


def apply() -> bool:
    """Apply the fix to the loaded Hermes. Never raises; True when it is in place."""
    try:
        from gateway import hosted_room_discussion
    except Exception as error:  # a Hermes without group rooms: nothing to fix
        log.info("hermes-bot-fixes: room trim fix not applied (%s)", error)

        return False

    try:
        return patch_watermarks(hosted_room_discussion)
    except Exception:
        log.exception("hermes-bot-fixes: applying the room trim fix failed")

        return False
