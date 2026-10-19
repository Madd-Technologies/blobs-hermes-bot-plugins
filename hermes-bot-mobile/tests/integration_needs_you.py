"""Pushes Needs you through a real Hermes gateway: loads `dashboard/plugin_api.py` the way the dashboard does, attaches a
stand-in client connection, opens and answers a real approval request, and checks the client got
`plugin.hermes-bot-mobile.needs_you.changed` both times, plus the route.

Not part of the unit tests (it needs a Hermes 0.21.5+ checkout). Run it with the Hermes venv:

    HERMES_AGENT=<checkout> ~/.hermes/hermes-agent/venv/bin/python hermes-bot-mobile/tests/integration_needs_you.py

HERMES_HOME points at a throwaway folder, so the real `~/.hermes` is never read or written.
"""

import importlib.util
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent
sys.path.insert(0, os.environ.get("HERMES_AGENT") or str(Path.home() / ".hermes" / "hermes-agent"))
os.environ["HERMES_HOME"] = tempfile.mkdtemp(prefix="hermes-bot-mobile-needs-you-")

from tui_gateway import server, server_requests  # noqa: E402

EVENT = "plugin.hermes-bot-mobile.needs_you.changed"


class Client:
    """A connected app: what the gateway writes to it."""

    def __init__(self):
        self.frames = []
        self.arrived = threading.Condition()

    def write(self, frame):
        with self.arrived:
            self.frames.append(frame)
            self.arrived.notify_all()

        return True

    def pushes(self):
        return [frame["params"]["payload"] for frame in self.frames if frame.get("method") == "event" and frame.get("params", {}).get("type") == EVENT]

    def wait(self, count):
        with self.arrived:
            if not self.arrived.wait_for(lambda: len(self.pushes()) >= count, timeout=3):
                raise SystemExit(f"FAIL: expected {count} needs-you pushes, got {self.pushes()} in {self.frames}")

        return self.pushes()[count - 1]


def load_api():
    name = "hermes_dashboard_plugin_hermes-bot-mobile"
    spec = importlib.util.spec_from_file_location(name, PLUGIN / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)

    return module


client = Client()
server.register_live_transport(client)
api = load_api()
assert api._needs_you().live, "the watch did not start on a real gateway"
assert type(server_requests._open).__name__ == "WatchedTable", "the open-requests table was not swapped"

sid = "it-session"
request = server_requests.ServerRequest(
    sid,
    "approval",
    {"request_id": "it-approval", "command": "rm -r <redacted>", "description": "delete a folder", "choices": ["once", "session", "always", "deny"], "allow_session": True, "allow_permanent": True},
)
started = time.monotonic()
server_requests._register(request)
opened = client.wait(1)
latency_ms = (time.monotonic() - started) * 1000
entry = opened["requests"][0]
assert entry["id"] == request.id and entry["method"] == "approval" and entry["session_id"] == sid, opened
assert entry["params"]["description"] == "delete a folder" and "session_id" not in entry["params"], entry
print(f"open pushed in {latency_ms:.0f} ms: {entry['method']} {entry['params']['description']!r}")

route = api.needs_you()
assert route["live"] and [r["id"] for r in route["requests"]] == [request.id], route

server_requests.resolve_response({"jsonrpc": "2.0", "id": request.id, "result": {"choice": "deny"}})
closed = client.wait(2)
assert closed["requests"] == [] and closed["version"] > opened["version"], closed
print("answer pushed: the list is empty again")

# A non-human request (the agent reading the client's terminal) is not something waiting on you.
reading = server_requests.ServerRequest(sid, "terminal.read", {})
with server_requests._lock:
    server_requests._open[reading.id] = reading
time.sleep(0.3)
assert len(client.pushes()) == 2, client.pushes()
print("a terminal.read request pushed nothing")
print("OK")
