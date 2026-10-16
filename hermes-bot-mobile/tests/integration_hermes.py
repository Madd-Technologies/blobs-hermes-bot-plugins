"""Loads the plugin through Hermes's own plugin manager and pushes a real approval to a local listener.

Not part of the unit tests (it needs a Hermes checkout). Run it with the Hermes venv:

    ~/.hermes/hermes-agent/venv/bin/python hermes-bot-mobile/tests/integration_hermes.py

`HERMES_AGENT=<checkout>` picks another Hermes checkout (default `~/.hermes/hermes-agent`). It points HERMES_HOME (and the
kanban board) at a throwaway folder, so the real `~/.hermes` is never read or written. With a Hermes that has the
kanban_task_blocked hook (0.21.5+), it also blocks a real kanban card and checks the push.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, os.environ.get("HERMES_AGENT") or str(Path.home() / ".hermes" / "hermes-agent"))

home = Path(tempfile.mkdtemp(prefix="hermes-bot-mobile-it-"))
os.environ["HERMES_HOME"] = str(home)
os.environ["HERMES_KANBAN_HOME"] = str(home)

(home / "plugins").mkdir()
shutil.copytree(PLUGIN, home / "plugins" / "hermes-bot-mobile", ignore=shutil.ignore_patterns("tests", "__pycache__"))
(home / "config.yaml").write_text("plugins:\n  enabled:\n    - hermes-bot-mobile\n")

from helpers import Phone  # noqa: E402

received = []
arrived = threading.Event()


class Listener(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        received.append((self.path, dict(self.headers), body))
        self.send_response(201)
        self.end_headers()
        arrived.set()

    def log_message(self, *args):
        pass


server = HTTPServer(("127.0.0.1", 0), Listener)
threading.Thread(target=server.serve_forever, daemon=True).start()

phone = Phone(f"http://127.0.0.1:{server.server_port}/upIntegration")

import importlib  # noqa: E402

from hermes_cli.plugins import get_plugin_manager  # noqa: E402

manager = get_plugin_manager()
manager.discover_and_load(force=True)

loaded = {name: plugin for name, plugin in manager._plugins.items()}
print("loaded plugins:", sorted(loaded))
assert "hermes-bot-mobile" in loaded, "Hermes did not load the plugin"
assert getattr(loaded["hermes-bot-mobile"], "error", None) in (None, ""), loaded["hermes-bot-mobile"].error
assert manager._hooks.get("pre_approval_request"), "approval hook not registered"

# Register the phone with the very store the plugin uses.
from hermes_constants import get_default_hermes_root  # noqa: E402

store_module = sys.modules[[m for m in sys.modules if m.endswith(".store") and "hermes_bot_mobile" in m.replace("-", "_")][0]]
store_module.DeviceStore().upsert(phone.endpoint, phone.p256dh, phone.auth, "Integration phone")
print("devices file:", get_default_hermes_root() / "hermes-bot-mobile" / "devices.json")

from hermes_cli.lifecycle import invoke_hook  # noqa: E402

invoke_hook(
    "pre_approval_request",
    command="rm -rf /tmp/secret-build-dir",
    description="recursive delete",
    pattern_key="rm",
    pattern_keys=["rm"],
    session_key="it-session",
    surface="gateway",
)

assert arrived.wait(10), "no push reached the listener"
path, headers, body = received[0]
message = json.loads(phone.read(body))
print("push headers:", {k: v for k, v in headers.items() if k in ("Content-Encoding", "Urgency", "TTL")})
print("push message:", message)
assert message["body"] == "Needs your OK: recursive delete"
assert message["url"] == "hermesbot://bot/default"
assert "secret-build-dir" not in json.dumps(message)

# A routine that ran fine is never pushed (0.1.2): nothing more reaches the listener.
arrived.clear()
invoke_hook("on_session_end", session_id="cron_0a1b2c3d4e5f_20260101_090000", platform="cron", completed=True, failed=False, interrupted=False)
assert not arrived.wait(2), "a routine that ran was pushed"

# A bot's answer in one of the person's chats (0.1.3): the start of it, like a message.
assert manager._hooks.get("post_llm_call"), "reply hook not registered"
chat = "20261015_090000_aa11bb"
invoke_hook("post_llm_call", session_id=chat, task_id="t", turn_id="turn-1", user_message="Any news on the quote?",
            assistant_response="**The quote came back** at $640. They can come on Tuesday or Thursday morning.",
            conversation_history=[], model="m", platform="tui")
assert arrived.wait(10), "no reply push reached the listener"
message = json.loads(phone.read(received[-1][2]))
print("reply push:", message)
assert message["body"] == "The quote came back at $640. They can come on Tuesday or Thursday morning."
assert message["type"] == "Replies"
assert message["url"] == f"hermesbot://bot/default?chat={chat}"

# Another bot's message to this one runs as `hermes -p <bot> chat` ("cli"): not the person's chat, never pushed.
arrived.clear()
invoke_hook("post_llm_call", session_id=chat, task_id="t", turn_id="turn-2", user_message="hi", assistant_response="hello",
            conversation_history=[], model="m", platform="cli")
assert not arrived.wait(2), "another bot's turn was pushed"

if manager._hooks.get("kanban_task_blocked"):
    # A worker blocks its card on the person, through Hermes's own board: the hook fires after the write and pushes.
    from hermes_cli import kanban_db as kb  # noqa: E402

    try:
        from hermes_cli.kanban_db_connect import connect  # noqa: E402
    except ImportError:  # before 0.21.5
        connect = kb.connect

    conn = connect()
    task = kb.create_task(conn, title="Write the weekly report", assignee="default", created_by="default")
    waiting = kb.create_task(conn, title="Send it", assignee="default", created_by="default", parents=[task])
    arrived.clear()
    received.clear()
    assert kb.block_task(conn, task, reason="Which week should the report cover?")
    assert arrived.wait(10), "no push for the blocked card"
    message = json.loads(phone.read(received[0][2]))
    print("blocked card push:", message)
    assert message["body"].startswith("Needs you: Write the weekly report — Which week"), message
    assert message["type"] == "Questions" and message["url"] == "hermesbot://inbox"

    # A card that only waits on another card moves to todo, and is not the person's business.
    arrived.clear()
    kb.block_task(conn, waiting, reason="waiting on the report", kind="dependency")
    assert kb.get_task(conn, waiting).status == "todo"
    assert not arrived.wait(2), "a dependency wait was pushed"
    conn.close()
    print("kanban: a card blocked on the person is pushed; a dependency wait is not")
else:
    print("kanban: this Hermes has no kanban_task_blocked hook; skipped")

print("OK")

server.shutdown()
shutil.rmtree(home, ignore_errors=True)
