"""Loads the plugin through Hermes's own plugin manager and drives real kanban cards through Hermes's own board code.

Not part of the unit tests (it needs a Hermes checkout). Run it with a Hermes venv, from anywhere:

    <hermes venv>/bin/python hermes-kanban/tests/integration_kanban.py [path to a Hermes checkout]

It points HERMES_HOME at a throwaway folder, so the real `~/.hermes` is never read or written. No model runs: the desk turn is
stood in for; the live check is a card on a throwaway server.
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else str(Path.home() / ".hermes" / "hermes-agent"))

home = Path(tempfile.mkdtemp(prefix="hermes-kanban-it-"))
os.environ["HERMES_HOME"] = str(home)
os.environ.pop("HERMES_KANBAN_BOARD", None)
os.environ.pop("HERMES_KANBAN_DB", None)
(home / "plugins").mkdir()
for bot in ("orion", "juniper"):
    (home / "profiles" / bot).mkdir(parents=True)
shutil.copytree(PLUGIN, home / "plugins" / "hermes-kanban", ignore=shutil.ignore_patterns("tests", "__pycache__"))
(home / "config.yaml").write_text("plugins:\n  enabled:\n    - hermes-kanban\n")

from hermes_cli.plugins import get_plugin_manager  # noqa: E402

manager = get_plugin_manager()
manager.discover_and_load(force=True)
plugin = manager._plugins.get("hermes-kanban")
assert plugin is not None, "Hermes did not load the plugin"
assert getattr(plugin, "error", None) in (None, ""), plugin.error
for hook in ("kanban_task_claimed", "kanban_task_blocked", "kanban_task_completed"):
    assert manager._hooks.get(hook), f"{hook} not registered"

from hermes_cli import kanban_db as kb  # noqa: E402
from hermes_cli import kanban_db_connect  # noqa: E402

data = home / "hermes-kanban"
requests = data / "requests"


def pending():
    return [json.loads(p.read_text()) for p in sorted(requests.glob("*.json"))] if requests.is_dir() else []


def clear():
    for path in requests.glob("*.json"):
        path.unlink()


conn = kanban_db_connect.connect()

# Files: a done parent card with a file, and a child card with its own; claiming the child copies both into its workspace.
parent = kb.create_task(conn, title="Research the venue", assignee="juniper", created_by="orion")
kb.store_attachment_bytes(conn, parent, "venue-notes.md", b"# Notes\n", uploaded_by="orion")
assert kb.complete_task(conn, parent, summary="Notes attached.")
child = kb.create_task(conn, title="Draft the welcome email", assignee="juniper", created_by="orion", parents=[parent])
kb.store_attachment_bytes(conn, child, "brief.pdf", b"%PDF-1.4 brief", uploaded_by="orion")
claimed = kb.claim_task(conn, child)
assert claimed is not None, "the child card could not be claimed"
from hermes_cli import kanban_db_workspace  # noqa: E402

workspace = Path(kanban_db_workspace.resolve_workspace(kb.get_task(conn, child)))
own = workspace / ".kanban-files" / child / "brief.pdf"
inherited = workspace / ".kanban-files" / child / f"from-{parent}" / "venue-notes.md"
print("staged:", sorted(str(p.relative_to(workspace)) for p in (workspace / ".kanban-files").rglob("*") if p.is_file()))
assert own.read_bytes() == b"%PDF-1.4 brief"
assert inherited.read_bytes() == b"# Notes\n"
notes = [c for c in kb.list_comments(conn, child) if c.author == "hermes-kanban"]
assert len(notes) == 1 and f".kanban-files/{child}/from-{parent}/venue-notes.md" in notes[0].body, notes
context = kb.build_worker_context(conn, child)
assert f".kanban-files/{child}/brief.pdf" in context, "the worker's context does not list the copied files"
print("worker context mentions the copies: yes")

# A first block wakes the bot that created the card.
assert kb.block_task(conn, child, reason="Which tone should the email use?", kind="needs_input")
[wake] = pending()
print("first block:", wake["action"], "->", wake["target"])
assert (wake["action"], wake["target"], wake["task_id"]) == ("wake", "orion", child)
assert "Which tone should the email use?" in wake["message"]

# The desk runner: a stood-in turn, run once, logged.
sys.path.insert(0, str(home / "plugins"))
import importlib  # noqa: E402

desk = importlib.import_module("hermes-kanban.desk")
turns = []
runner = desk.Desk(turn=lambda root, target, message, input_id: turns.append(target) or "Answered on the card.")
[(path, request)] = runner.due()
assert runner.handle(path, request) == "woke", "the desk did not run the wake"
assert turns == ["orion"] and pending() == []

# Someone answers and unblocks it; it blocks again the same way: Hermes moves it to triage and nobody is woken again.
assert kb.unblock_task(conn, child)
claimed = kb.claim_task(conn, child)
assert kb.block_task(conn, child, reason="Which tone, again?", kind="needs_input")
assert kb.get_task(conn, child).status == "triage", kb.get_task(conn, child).status
print("second block of the same kind:", kb.get_task(conn, child).status, "requests:", len(pending()))
assert pending() == []

# A dependency wait fires the hook before it is saved; it is never handed to a bot.
open_parent = kb.create_task(conn, title="Book the venue", assignee="juniper", created_by="orion")
waiting = kb.create_task(conn, title="Send invites", assignee="juniper", created_by="orion")
kb.claim_task(conn, waiting)
with conn:  # Hermes refuses to link a running card; a worker that finds a new prerequisite ends up like this
    conn.execute("INSERT INTO task_links(parent_id, child_id) VALUES (?, ?)", (open_parent, waiting))
assert kb.block_task(conn, waiting, reason="Needs the venue first", kind="dependency")
print("dependency wait:", kb.get_task(conn, waiting).status, "requests:", len(pending()))
assert pending() == []

# A transient block asks for one retry later; the runner unblocks it when the time comes.
flaky = kb.create_task(conn, title="Fetch the menu", assignee="juniper", created_by="orion")
kb.claim_task(conn, flaky)
assert kb.block_task(conn, flaky, reason="The site timed out", kind="transient")
[retry] = pending()
assert retry["action"] == "retry"
runner._clock = lambda: retry["not_before"]
[(path, request)] = runner.due()
assert runner.handle(path, request) == "retried"
print("transient block:", kb.get_task(conn, flaky).status, "after the retry")
assert kb.get_task(conn, flaky).status in ("ready", "todo")

# A card a person made and works has no bot to wake.
manual = kb.create_task(conn, title="Call the caterer", assignee="alex", created_by="alex")
kb.claim_task(conn, manual)
kb.block_task(conn, manual, reason="Waiting for a call back", kind="needs_input")
assert pending() == []

conn.close()
print("OK")
shutil.rmtree(home, ignore_errors=True)
