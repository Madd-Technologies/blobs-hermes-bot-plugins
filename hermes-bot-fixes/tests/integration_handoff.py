"""Shows Hermes's note for an unfinished background task, then the same note with the hand-off, with no model call.

Not part of the unit tests (it needs a Hermes checkout at the version being fixed). Run it with a Hermes venv:

    ~/.hermes/hermes-agent/venv/bin/python hermes-bot-fixes/tests/integration_handoff.py <path to a Hermes checkout>

It points HERMES_HOME at a throwaway folder, so the real `~/.hermes` is never read or written. The note is formatted the
way the app's chats format it: inside the owning chat's profile scope (a bound HERMES_HOME override).
"""

import os
import sys
import tempfile
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else str(Path.home() / ".hermes" / "hermes-agent"))
sys.path.insert(0, str(PLUGIN))
root = Path(tempfile.mkdtemp(prefix="hermes-bot-fixes-handoff-"))
os.environ["HERMES_HOME"] = str(root)
for name in ("HERMES_PROFILE", "HERMES_PROFILE_NAME"):
    os.environ.pop(name, None)
(root / "profiles" / "zz-handoff").mkdir(parents=True)

import hermes_constants  # noqa: E402
from tools import process_registry_notifications as prn  # noqa: E402

import handoff  # noqa: E402

# What delegate_tool reports for a background task that ran out of steps (exit_reason max_iterations).
truncated = {
    "type": "async_delegation", "delegation_id": "deleg_1", "session_key": "", "goal": "Add a status column to the weekly report",
    "context": "Red when a task is overdue.", "role": "leaf", "model": "m", "status": "completed",
    "summary": "Added the column; the overdue rule is not written yet.", "api_calls": 250, "duration_seconds": 900,
    "exit_reason": "max_iterations", "dispatched_at": 0, "completed_at": 900,
}
finished = {**truncated, "delegation_id": "deleg_2", "exit_reason": "completed", "api_calls": 40, "summary": "Done."}
fan_out = {
    "type": "async_delegation", "delegation_id": "deleg_3", "is_batch": True, "goals": ["Write the rule", "Write the column"],
    "status": "completed", "dispatched_at": 0, "completed_at": 900,
    "results": [
        {"task_index": 0, "status": "completed", "exit_reason": "completed", "summary": "Rule written."},
        {"task_index": 1, "status": "completed", "exit_reason": "max_iterations", "truncated": True, "summary": "Half a column."},
    ],
}


def in_profile(evt, profile):
    token = hermes_constants.set_hermes_home_override(str(root / "profiles" / profile))
    try:
        return prn.format_process_notification(evt)
    finally:
        hermes_constants.reset_hermes_home_override(token)


before = prn.format_process_notification(truncated)
assert "re-dispatch" in before and handoff.HEADER not in before, "expected Hermes's own note (is this the Hermes being fixed?)"
print("before: Hermes's note leaves it to the bot:", before.splitlines()[1][:70], "...")

assert handoff.apply(), "fix not applied"

after = in_profile(truncated, "zz-handoff")
assert after.startswith(before), "expected Hermes's note kept as it was"
assert handoff.HEADER in after and 'assignee "zz-handoff"' in after and 'idempotency_key "leftover-deleg_1"' in after, after
print("after: the note ends with the hand-off, naming the chat's own profile:", 'assignee "zz-handoff"')

title = prn.async_delegation_display_text(truncated)
assert title == "Subagent Task Incomplete: Add a status column to the weekly report", title
print("unchanged: the chat's title for it:", title)

assert handoff.HEADER not in in_profile(finished, "zz-handoff"), "expected no hand-off for a finished task"
print("unchanged: a finished task gets no hand-off")

batch = in_profile(fan_out, "zz-handoff")
assert "Task 2 of this batch did not finish" in batch and "leftover-deleg_3-<task number>" in batch, batch
print("after: a fan-out names only its unfinished task (task 2)")

# Callers that imported format_process_notification by name before the fix still get it (the formatter is looked up by
# name on every call).
from tools.process_registry_notifications import format_process_notification as imported_early  # noqa: E402

assert handoff.HEADER in imported_early(truncated)
print("after: callers holding format_process_notification get the hand-off too")
print("OK")
