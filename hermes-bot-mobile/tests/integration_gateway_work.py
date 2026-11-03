"""Checks the gateway half of "restart from the app" against a Hermes checkout (0.22+), with no gateway and nothing
restarted. Not part of the unit tests. Run it with a Hermes venv:

    ~/.hermes/hermes-agent/venv/bin/python hermes-bot-mobile/tests/integration_gateway_work.py <path to a Hermes checkout>

It points HERMES_HOME at a throwaway folder and stands in for the gateway's control socket only (the one call that asks
a running gateway what it is doing), so Hermes's own restart guard words the gateway's work, exactly as
`hermes gateway restart` would print it when it refuses. Then it checks that the plugin finds Hermes's restart spawner,
that the spawner takes `interrupt` (what "Restart now" passes), and where the spawner's output lands.
"""

import os
import sys
import tempfile
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else str(Path.home() / ".hermes" / "hermes-agent"))
sys.path.insert(0, str(PLUGIN))
home = Path(tempfile.mkdtemp(prefix="hermes-bot-mobile-gateway-"))
os.environ["HERMES_HOME"] = str(home)

import hermes_cli.web_server  # noqa: E402,F401  (what the dashboard has loaded)
from gateway import control_socket  # noqa: E402

import restart  # noqa: E402

system = restart.System()
answers = {}
real_query = control_socket.query_gateway_control


def query(asked_home, verb, *, params=None, timeout=None):
    assert verb == "active-work", verb

    return answers.get(Path(asked_home))


control_socket.query_gateway_control = query

try:
    # No gateway answers: nothing to wait for, and the restart itself decides.
    assert system.gateway_work() == [], system.gateway_work()

    answers[home] = {"active_agents": 2, "active_work": [
        {"kind": "cron", "job_id": "0123456789ab", "pid": 4100, "elapsed_s": 10},
        {"kind": "chat", "session": "agent:main:local:dm:abc", "pid": 4100, "elapsed_s": 508, "model": "demo-model", "current_tool": "teammate_wait"},
    ]}
    work = system.gateway_work()
    assert work == [
        "cron job 0123456789ab in-process pid 4100, running 10s",
        "chat turn agent:main:local:dm:abc pid 4100, running 8m28s [model demo-model, tool teammate_wait]",
    ], work

    answers[home] = {"active_agents": 0, "active_work": []}
    assert system.gateway_work() == [], system.gateway_work()
finally:
    control_socket.query_gateway_control = real_query

spawner = system.gateway_spawner()
assert spawner is not None, "this Hermes has no _spawn_gateway_restart"
assert restart.accepts_interrupt(spawner), "this Hermes's gateway restart takes no interrupt"
assert system.gateway_log() == home / "logs" / "gateway-restart.log", system.gateway_log()
print("gateway work:", work)
print("ok")
