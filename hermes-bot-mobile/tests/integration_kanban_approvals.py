"""A kanban worker's approval answered from the app, through a real Hermes approval gate.

Sets this process up the way Hermes's dispatcher sets up a worker (`HERMES_KANBAN_TASK`, `HERMES_PROFILE`, and the
single-query marker `hermes chat -q` sets), checks that Hermes refuses a flagged command on the spot, then installs the
relay and checks that the same command waits for the person instead: answered "once" it runs, "deny" it doesn't,
"session" covers the next one without asking, and the dashboard's list shows it only while it waits.

Not part of the unit tests (it needs a Hermes 0.21.6 checkout). Run it with the Hermes venv:

    HERMES_AGENT=<checkout> <checkout>/venv/bin/python hermes-bot-mobile/tests/integration_kanban_approvals.py

HERMES_HOME points at a throwaway folder, so the real `~/.hermes` is never read or written. Nothing is executed: only the
approval decision is asked for.
"""

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent
sys.path.insert(0, os.environ.get("HERMES_AGENT") or str(Path.home() / ".hermes" / "hermes-agent"))
home = Path(tempfile.mkdtemp(prefix="hermes-bot-mobile-kanban-"))
# Manual approvals: the default (`smart`) would ask a model first.
(home / "config.yaml").write_text("approvals:\n  mode: manual\n")
os.environ.update({"HERMES_HOME": str(home), "HERMES_KANBAN_TASK": "t_feedc0de", "HERMES_PROFILE": "orion", "HERMES_SINGLE_QUERY_SESSION": "1"})

from tools import approval, approval_context  # noqa: E402

sys.path.insert(0, str(PLUGIN))
import kanban_approvals as ka  # noqa: E402

COMMAND = "rm -rf /tmp/zz-hermes-bot-mobile-kanban-check"


def fail(message):
    raise SystemExit(f"FAIL: {message}")


def ask(command=COMMAND):
    """Asks the gate on another thread, as a worker's tool call would; returns a way to wait for the decision."""
    box = {}
    thread = threading.Thread(target=lambda: box.setdefault("result", approval.check_all_command_guards(command, "local")))
    thread.start()

    def result():
        thread.join(10)

        if thread.is_alive():
            fail("the gate never decided")

        return box["result"]

    return result


def waiting(dashboard, count=1):
    deadline = time.time() + 5

    while time.time() < deadline:
        pending = dashboard.pending()

        if len(pending) >= count:
            return pending

        time.sleep(0.05)

    fail(f"expected {count} waiting approvals, got {dashboard.pending()}")


before = approval.check_all_command_guards(COMMAND, "local")

if before.get("approved") or "single-query" not in str(before.get("message")):
    fail(f"without the relay Hermes should refuse at once: {before}")

print("ok: without the relay, Hermes refuses the worker's command on the spot")

where = {"task": "t_feedc0de", "title": "Clear the build cache", "board": "", "created_by": "orion", "profile": "orion", "chat": "20261020_101500_0dc184"}
relay = ka.WorkerRelay(context=lambda: where)

if not relay.install():
    fail("the relay did not install on this Hermes")

if approval_context._get_approval_timeout() != ka.WAIT_S:
    fail(f"the wait should be {ka.WAIT_S} s, is {approval_context._get_approval_timeout()}")

dashboard = ka.KanbanApprovals()

result = ask()
entry = waiting(dashboard)[0]

if (entry["profile"], entry["chat"], entry["task"]["title"]) != ("orion", "20261020_101500_0dc184", "Clear the build cache") or "rm -rf" not in entry["command"]:
    fail(f"the waiting approval says the wrong thing: {entry}")

dashboard.answer(entry["id"], "once")
decision = result()

if not decision.get("approved"):
    fail(f"answered once, the command should be allowed: {decision}")

if dashboard.pending() or list(dashboard.folder.glob("*.json")):
    fail("the answered approval's files should be gone")

print("ok: it waits for the person, shows in the right chat, and runs once allowed; its files go")

result = ask()
entry = waiting(dashboard)[0]
dashboard.answer(entry["id"], "deny")
decision = result()

if decision.get("approved") or "BLOCKED" not in str(decision.get("message")):
    fail(f"denied, the command must not run: {decision}")

print("ok: denied, it doesn't run")

result = ask()
entry = waiting(dashboard)[0]
dashboard.answer(entry["id"], "session")

if not result().get("approved"):
    fail("allowed for the session, the command should run")

again = approval.check_all_command_guards(COMMAND, "local")

if not again.get("approved") or dashboard.pending():
    fail(f"allowed for the session, the next one should run without asking: {again}")

print("ok: allowed for the session, the next one runs without asking")
print("PASS")
