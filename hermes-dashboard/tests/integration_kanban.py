"""Mirrors a real Hermes kanban board into a bot's Background work, then answers a blocked card from its record.

Not part of the unit tests (it needs a Hermes checkout). Run it with a Hermes venv:

    ~/.hermes/hermes-agent/venv/bin/python hermes-dashboard/tests/integration_kanban.py <path to a Hermes checkout>

HERMES_HOME and the kanban home point at a throwaway folder, so the real `~/.hermes` and its board are never read or
written. The cards go through Hermes's own `hermes_cli.kanban_db`, as a bot's and a worker's tools would move them.
"""

import os
import sys
import tempfile
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else str(Path.home() / ".hermes" / "hermes-agent"))
sys.path.append(str(PLUGIN))  # after Hermes: the plugin's flat `tools.py` must not hide Hermes's `tools` package
root = Path(tempfile.mkdtemp(prefix="hermes-dashboard-kanban-"))
os.environ["HERMES_HOME"] = str(root)
os.environ["HERMES_KANBAN_HOME"] = str(root)

from hermes_cli import kanban_db as kb  # noqa: E402
from hermes_cli.kanban_db_connect import connect  # noqa: E402

import kanban as K  # noqa: E402
import store as S  # noqa: E402

BOT = "zz-kanban"
served = lambda profile: profile == BOT  # noqa: E731
store = S.Store(root / "hermes-dashboard")
board = K.HermesBoard()

conn = connect()
rule = kb.create_task(conn, title="Write the weekly report", assignee=BOT, created_by=BOT)
panel = kb.create_task(conn, title="Build the report panel", assignee=BOT, created_by=BOT)
other = kb.create_task(conn, title="Someone else's task", assignee="zz-other", created_by="zz-other")
assert kb.block_task(conn, rule, reason="Which week should the report cover?"), "expected the card blocked"
print("board:", {task.id: task.status for task in kb.list_tasks(conn)})

K.mirror(store, board, served)
records = {record["values"]["card"]: record for record in store.query(S.Actor.bot(BOT), K.COLLECTION, {"limit": 50})["records"]}
assert records[rule]["values"]["status"] == "Blocked" and records[rule]["values"]["why"] == "Which week should the report cover?", records[rule]
assert records[panel]["values"]["status"] in ("Queued", "Running"), records[panel]
assert other not in records
(flagged,) = store.needs_you_records(BOT)
assert flagged["row"]["id"] == records[rule]["id"]
print("mirrored: the blocked card needs you, with why:", records[rule]["values"]["why"])

result = K.answer(store, records[rule]["id"], "Last week.", board, served)
assert result["posted"] and result["status"] != "blocked", result
last = kb.list_comments(conn, rule)[-1]
assert (last.author, last.body) == (K.AUTHOR, "Last week."), last
assert kb.get_task(conn, rule).status != "blocked"
assert store.needs_you_records(BOT) == []
print("answered: the note is a comment on the card from", repr(last.author), "and the card is", kb.get_task(conn, rule).status)

assert kb.complete_task(conn, panel, summary="Panel built: a red dot on overdue tasks.")
K.mirror(store, board, served)
done = store.get_record(S.Actor.person(), records[panel]["id"])["record"]
assert done["values"]["status"] == "Done" and done["closed_at"] and done["values"]["latest"].startswith("Panel built"), done
print("finished: the done card's record closed with its summary:", done["values"]["latest"][:40], "...")

# Blocked again for the same reason, Hermes sends a card to triage for a person: still needs you, and the answer releases it.
loop = kb.create_task(conn, title="Send the report", assignee=BOT, created_by=BOT)
assert kb.block_task(conn, loop, reason="No address to send it to")
assert kb.unblock_task(conn, loop)
assert kb.block_task(conn, loop, reason="No address to send it to")
assert kb.get_task(conn, loop).status == "triage", kb.get_task(conn, loop).status
planned = kb.create_task(conn, title="Plan the week", assignee=BOT, created_by=BOT, triage=True)
K.mirror(store, board, served)
by_card = {record["values"]["card"]: record for record in store.query(S.Actor.bot(BOT), K.COLLECTION, {"limit": 50})["records"]}
assert by_card[loop]["values"]["status"] == "Blocked" and by_card[loop]["values"]["why"].startswith("Blocked again"), by_card[loop]
assert by_card[planned]["values"]["status"] == "Queued", by_card[planned]
print("looped: a card triaged after blocking twice needs you:", by_card[loop]["values"]["why"])
result = K.answer(store, by_card[loop]["id"], "Use the team list.", board, served)
assert result["posted"] and result["status"] in ("todo", "ready"), result
K.answer(store, by_card[planned]["id"], "Mondays first.", board, served)
assert kb.get_task(conn, planned).status == "triage", "a planner's triage card is not the person's to release"
print("looped: the answer released it to", result["status"], "; a planner's triage card got the note and stayed in triage")

assert K.mirror(store, board, served) == 0
print("unchanged: a second pass writes nothing")
conn.close()
print("OK")
