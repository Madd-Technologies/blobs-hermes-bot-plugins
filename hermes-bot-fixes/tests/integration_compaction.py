"""Shows the compaction restatement bug on Hermes's real ContextCompressor, then the fix, with no model call.

Not part of the unit tests (it needs a Hermes checkout at the version being fixed). Run it with a Hermes venv:

    ~/.hermes/hermes-agent/venv/bin/python hermes-bot-fixes/tests/integration_compaction.py <path to a Hermes checkout>

It points HERMES_HOME at a throwaway folder, so the real `~/.hermes` is never read or written. The chat is the case being
fixed: the bot handed a request to a background task and said so, the task ran out of steps, its result woke the bot, and a
compaction landed in that turn.
"""

import os
import sys
import tempfile
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else str(Path.home() / ".hermes" / "hermes-agent"))
sys.path.insert(0, str(PLUGIN))
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="hermes-bot-fixes-compaction-")

from agent import context_compressor as cc  # noqa: E402

import compaction  # noqa: E402

HEADER = cc._INFLIGHT_TASK_REPLAY_HEADER
ask = {"role": "user", "content": "Add a status column to the weekly report; red when a task is overdue."}
wake = {
    "role": "user",
    "display_kind": "async_delegation_complete",
    "content": "[ASYNC DELEGATION BATCH COMPLETE — deleg_1]\n⚠ TASK 1/1 TRUNCATED: hit max_iterations — work may be incomplete",
}
call = lambda n, name: {"role": "assistant", "content": "", "tool_calls": [{"id": f"c{n}", "type": "function", "function": {"name": name, "arguments": "{}"}}]}  # noqa: E731
result = lambda n: {"role": "tool", "tool_call_id": f"c{n}", "content": "ok"}  # noqa: E731
messages = [
    {"role": "system", "content": "You are Concierge."},
    ask,
    call(1, "delegate_task"), result(1),
    {"role": "assistant", "content": "It is running in the background with the rule you chose."},
    wake,
    call(2, "read_file"), result(2),
]
carrier = {"role": "user", "content": f"{cc.SUMMARY_PREFIX}\nThe person asked for a status column; a background task is building it.\n{cc._SUMMARY_END_MARKER}", cc.COMPRESSED_SUMMARY_METADATA_KEY: True}


def compressor():
    engine = object.__new__(cc.ContextCompressor)
    engine.quiet_mode = True

    return engine


def restated(rows):
    return [row for row in rows if HEADER in str(row.get("content"))]


# Before: Hermes picks the answered request and restates it.
picked = cc.ContextCompressor._find_inflight_user_task(messages)
assert picked is ask, "expected Hermes to pick the answered request (is this the Hermes being fixed?)"
before = compressor()._reappend_inflight_user_task([messages[0], dict(carrier), dict(wake), call(2, "read_file"), result(2)], picked)
assert restated(before) and ask["content"] in str(restated(before)[-1]["content"]), "expected the request restated"
print("before: picks the answered request, restates it:", str(restated(before)[-1]["content"])[:60], "...")

assert compaction.apply(), "fix not applied"

# After: the result is the task; nothing is restated while it is still in the chat.
picked = cc.ContextCompressor._find_inflight_user_task(messages)
assert picked is wake, "expected the background result as the task"
kept = [messages[0], dict(carrier), dict(wake), call(2, "read_file"), result(2)]
after = compressor()._reappend_inflight_user_task(list(kept), picked)
assert not restated(after) and len(after) == len(kept), "expected nothing restated while the result is in the chat"
print("after: picks the background result; with it still in the chat, restates nothing")

# After: when the result was summarised away, the result (not the request) is restated, keeping its display_kind.
gone = compressor()._reappend_inflight_user_task([messages[0], dict(carrier), {"role": "assistant", "content": "Reading the result."}, call(2, "read_file"), result(2)], picked)
copy = restated(gone)[-1]
assert "ASYNC DELEGATION BATCH COMPLETE" in str(copy["content"]) and ask["content"] not in str(copy["content"]), "expected the result restated"
assert copy.get("display_kind") == "async_delegation_complete", "expected the copy to keep its display_kind"
print("after: with the result summarised away, restates the result as a note row (display_kind kept)")

# A request still being worked on is untouched.
working = [messages[0], ask, call(3, "read_file"), result(3)]
assert cc.ContextCompressor._find_inflight_user_task(working) is ask
print("unchanged: a request still being worked on stays the task")
print("OK")
