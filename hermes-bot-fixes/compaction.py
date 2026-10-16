"""A fix to what Hermes 0.21.x restates after it compacts a chat.

When a compaction lands in a turn that a background result started (a background task finishing,
`async_delegation_complete`; a background process, `process_complete`), Hermes looks for "the request still in progress"
(`ContextCompressor._find_inflight_user_task`). It skips the result's own row, because the row has a display_kind, and lands
on the person's last message. It judges that message unfinished because the chat does not end on a plain reply, and appends
it again headed "[STILL IN PROGRESS ... Continue it; do not start over.]" (`_reappend_inflight_user_task`). The bot then does
over, itself, the work it had already handed to the background task.

But a plain reply came after that message before the result woke the bot, so the message was answered and the task in
progress is the result. The finder now returns the result's row. When that row survived the compaction there is nothing to
restate (the bot reads it as the newest message), so the restatement is skipped; when it was summarised away, the result is
what gets restated, and its copy keeps its display_kind, so clients show it as a note rather than as the person's words.

Both wraps sit on the ContextCompressor class, which Hermes calls them through, check the methods' shape first, and leave
anything unexpected alone (logged).
"""

from __future__ import annotations

import functools
import inspect
import logging

log = logging.getLogger("hermes-bot-fixes")

#: The rows Hermes stores when a background result wakes a bot for a new turn.
WAKE_KINDS = frozenset({"async_delegation_complete", "process_complete"})

MARK = "__hermes_bot_fixes__"


def _params(fn) -> list[str]:
    try:
        return list(inspect.signature(fn).parameters)
    except (TypeError, ValueError):
        return []


def _text(message: dict) -> str:
    content = message.get("content")

    if isinstance(content, str):
        return content

    if isinstance(content, list):
        return " ".join(part["text"] for part in content if isinstance(part, dict) and isinstance(part.get("text"), str))

    return ""


def wakeup_after_answer(messages: list, request: dict) -> dict | None:
    """The newest background-result row that woke the bot after ``request`` had a plain reply, or None."""
    index = next((i for i, message in enumerate(messages) if message is request), -1)

    if index < 0:
        return None

    answered = False
    wake = None

    for message in messages[index + 1:]:
        if not isinstance(message, dict):
            continue

        role = message.get("role")

        if role == "assistant" and not message.get("tool_calls") and _text(message).strip():
            answered = True
        elif role == "user" and answered and message.get("display_kind") in WAKE_KINDS:
            wake = message

    return wake


def _same_row(a, b: dict) -> bool:
    return a is b or (isinstance(a, dict) and a.get("role") == b.get("role") and a.get("display_kind") == b.get("display_kind") and _text(a) == _text(b))


def patch_compressor(cls) -> bool:
    """Wrap the class's in-flight finder and its restatement. True when both wraps are in place (now or already)."""
    find = cls.__dict__.get("_find_inflight_user_task")
    restate = cls.__dict__.get("_reappend_inflight_user_task")

    if not isinstance(find, classmethod) or not callable(restate):
        log.warning("hermes-bot-fixes: the compaction methods look different in this Hermes; restatement fix not applied")

        return False

    if getattr(find.__func__, MARK, False):
        return True

    if _params(find.__func__) != ["cls", "messages"] or _params(restate)[:3] != ["self", "compressed", "inflight"]:
        log.warning("hermes-bot-fixes: the compaction methods take other arguments in this Hermes; restatement fix not applied")

        return False

    original_find = find.__func__

    @functools.wraps(original_find)
    def find_task(klass, messages):
        request = original_find(klass, messages)

        if not isinstance(request, dict) or not isinstance(messages, list):
            return request

        try:
            wake = wakeup_after_answer(messages, request)
        except Exception:
            log.exception("hermes-bot-fixes: reading the chat for a background result failed; Hermes's choice stands")

            return request

        if wake is None:
            return request

        log.info("hermes-bot-fixes: compaction: the request was answered before a background result woke the bot; the result is the task")

        return wake

    @functools.wraps(restate)
    def restate_task(self, compressed, inflight):
        # The result is still in the chat after the summary: the bot reads it as the newest message, so restate nothing.
        if isinstance(inflight, dict) and inflight.get("display_kind") in WAKE_KINDS and any(_same_row(row, inflight) for row in compressed or []):
            return compressed

        return restate(self, compressed, inflight)

    setattr(find_task, MARK, True)
    setattr(restate_task, MARK, True)
    cls._find_inflight_user_task = classmethod(find_task)
    cls._reappend_inflight_user_task = restate_task

    return True


def apply() -> bool:
    """Apply the fix to the loaded Hermes. Never raises; True when it is in place."""
    try:
        from agent.context_compressor import ContextCompressor
    except Exception as error:  # not a Hermes with this compressor: nothing to fix
        log.info("hermes-bot-fixes: compaction fix not applied (%s)", error)

        return False

    return patch_compressor(ContextCompressor)
