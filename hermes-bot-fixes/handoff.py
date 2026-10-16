"""Unfinished background work goes to a new background worker, never back onto the main chat.

When a background task (`delegate_task`) comes back unfinished, Hermes 0.21.x wakes the bot with a note that ends "act
on the result or re-dispatch if things have changed" (`_format_async_delegation` in
tools/process_registry_notifications.py). A bot reading that may finish the work itself, step after step, in the chat
the person talks to it in. Under that note this adds a hand-off: queue what is left as a kanban card assigned to the bot
itself (a worker with no step cap that survives restarts and retries), or re-dispatch it with delegate_task when the bot
has no kanban tools, then answer in a sentence or two.

`kanban_create` needs the assignee's exact profile name, which a bot is never told (a card for an unknown profile is
never picked up), so the note names it: Hermes formats the note inside the owning chat's profile scope, where
`current_profile_name` reads it.

The wrap sits on the module's `_format_async_delegation`, which `format_process_notification` looks up by name on every
call, so every surface that formats the note (the app's chats, the messaging gateway, the CLI) gets it. It checks the
function's shape first and leaves anything unexpected alone (logged).
"""

from __future__ import annotations

import functools
import inspect
import logging

log = logging.getLogger("hermes-bot-fixes")

MARK = "__hermes_bot_fixes__"

HEADER = "[HAND-OFF — UNFINISHED BACKGROUND WORK]"


def unfinished(result: dict) -> bool:
    """Whether one background task's result leaves work for a new background worker.

    ``result`` is one task's entry: ``status`` is one of completed/success, interrupted, error, failed, timeout,
    stalled, cancelled, rejected, unknown (the server stopped while it ran); ``truncated`` or
    ``exit_reason == "max_iterations"`` means it hit its step cap; ``error`` says why it failed.
    """
    if result.get("truncated") or result.get("exit_reason") == "max_iterations":
        return True

    # It ran out of time, stopped making progress, or the server stopped while it ran: a new worker (no step cap, and it
    # survives restarts) can finish it. A stop the person pressed, an error, a cancel or a refusal to start stays with
    # Hermes's own note.
    return result.get("status") in ("timeout", "stalled", "unknown")


def leftovers(evt: dict) -> list[tuple[int | None, str]]:
    """``(task number, goal)`` for each unfinished task in a finished background result; the number is None for a
    single task. Empty for a fan-out's early failure notice, whose siblings are still running: the batch's own result
    follows, and that is where the hand-off goes."""
    if evt.get("task_failure_notice"):
        return []

    results = evt.get("results")
    goals = evt.get("goals") or []

    if isinstance(results, list) and results:
        found = []

        for result in sorted((r for r in results if isinstance(r, dict)), key=lambda r: r.get("task_index", 0)):
            if not unfinished(result):
                continue

            index = result.get("task_index", 0)
            goal = goals[index] if isinstance(index, int) and 0 <= index < len(goals) else result.get("goal", "")
            found.append((index + 1 if isinstance(index, int) else None, str(goal or "")))

        return found

    # A single task, or a fan-out that failed as a whole before any task reported.
    goal = evt.get("goal") or "; ".join(str(g) for g in goals if g)

    return [(None, str(goal or ""))] if unfinished(evt) else []


def profile_name() -> str | None:
    """The profile the note is being written for, or None. The app's chats format it inside the owning chat's profile
    scope (a bound HERMES_HOME), which ``current_profile_name`` reads before any process-wide setting."""
    try:
        from hermes_cli.profiles import current_profile_name

        name = current_profile_name(None)
    except Exception:
        return None

    return name if name and name != "custom" else None


def handoff(evt: dict, profile: str | None) -> str:
    """The hand-off paragraph for a finished background result, or "" when nothing is left."""
    tasks = leftovers(evt)

    if not tasks:
        return ""

    deleg = str(evt.get("delegation_id") or "unknown")
    numbered = [n for n, _goal in tasks if n is not None]

    if not numbered:
        what, cards, keys = "The background task", "one card", f'idempotency_key "leftover-{deleg}"'
    else:
        names = ", ".join(str(n) for n in numbered)
        what = f"Task {names} of this batch" if len(numbered) == 1 else f"Tasks {names} of this batch"
        cards = "one card" if len(numbered) == 1 else "one card per unfinished task"
        keys = f'idempotency_key "leftover-{deleg}-<task number>"'

    assignee = f'assignee "{profile}" (your own profile)' if profile else "assignee: your own profile name, exactly"

    return "\n".join([
        "",
        HEADER,
        f"{what} did not finish. Do not do the rest yourself in this chat, and do not check it here: hand it to a new "
        "background worker.",
        f"- If you have the kanban_create tool: create {cards}, {assignee}, {keys}. In the body put the original goal, "
        "what is already done (from the result above), exactly what is left, and the transcript path if one is given. "
        'If the work lives in a folder, set workspace_kind "dir" and workspace_path to that absolute path (the default '
        "scratch folder is deleted when the card finishes).",
        "- If you don't have kanban_create: re-dispatch only what is left with delegate_task, with the same detail in "
        "its goal and context.",
        "Then reply in one or two sentences: what finished, and where the rest went (the card id, or that it was "
        "re-dispatched). Only if the result above plainly says nothing is left, say so and queue nothing.",
    ])


def _params(fn) -> list[str]:
    try:
        return list(inspect.signature(fn).parameters)
    except (TypeError, ValueError):
        return []


def patch_notifications(module) -> bool:
    """Wrap the module's async-delegation formatter. True when the wrap is in place (now or already)."""
    original = getattr(module, "_format_async_delegation", None)

    if not callable(original):
        log.warning("hermes-bot-fixes: no background-result formatter in this Hermes; hand-off fix not applied")

        return False

    if getattr(original, MARK, False):
        return True

    if _params(original) != ["evt"]:
        log.warning("hermes-bot-fixes: the background-result formatter takes other arguments in this Hermes; hand-off fix not applied")

        return False

    @functools.wraps(original)
    def format_result(evt):
        text = original(evt)

        if not isinstance(text, str) or not isinstance(evt, dict):
            return text

        try:
            extra = handoff(evt, profile_name())
        except Exception:
            log.exception("hermes-bot-fixes: writing the hand-off failed; Hermes's note stands")

            return text

        if extra:
            log.info("hermes-bot-fixes: background result %s came back unfinished; the note asks for a hand-off", evt.get("delegation_id"))

        return text + "\n" + extra if extra else text

    setattr(format_result, MARK, True)
    module._format_async_delegation = format_result

    return True


def apply() -> bool:
    """Apply the fix to the loaded Hermes. Never raises; True when it is in place."""
    try:
        from tools import process_registry_notifications
    except Exception as error:  # not a Hermes with this formatter: nothing to fix
        log.info("hermes-bot-fixes: hand-off fix not applied (%s)", error)

        return False

    return patch_notifications(process_registry_notifications)
