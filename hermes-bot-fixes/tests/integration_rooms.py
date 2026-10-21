"""Through a real Hermes checkout: with hermes-bot-fixes loaded by Hermes's own plugin manager, the messaging gateway's
room-worker start does nothing and stops a worker already running there, and the dashboard's start still works.

Not part of the unit tests (it needs a Hermes checkout at the version being fixed). Run it with that Hermes's venv:

    ~/.hermes/hermes-agent/venv/bin/python -I hermes-bot-fixes/tests/integration_rooms.py [path to a Hermes checkout]

HERMES_HOME is a throwaway folder, so the real `~/.hermes` is never read or written.
"""
import os, shutil, sys, tempfile
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else str(Path.home() / ".hermes" / "hermes-agent"))
home = Path(tempfile.mkdtemp(prefix="hermes-bot-fixes-rooms-"))
os.environ["HERMES_HOME"] = str(home)
os.environ["HERMES_KANBAN_HOME"] = str(home)
(home / "plugins").mkdir()
shutil.copytree(PLUGIN, home / "plugins" / "hermes-bot-fixes", ignore=shutil.ignore_patterns("tests", "__pycache__"))
(home / "config.yaml").write_text("plugins:\n  enabled:\n    - hermes-bot-fixes\n")

import gateway.run  # noqa: F401  the messaging gateway's code, as `hermes gateway run` loads it
import tui_gateway.server  # noqa: F401
from tui_gateway import methods_groups
from gateway.run_startup import GatewayStartupMixin
from hermes_cli import plugins

original = GatewayStartupMixin.__dict__["_start_hosted_room_worker_sync"]
# A worker already running in the gateway before the plugin loads, started the gateway's own way.
service = original.__func__()
assert methods_groups.get_hosted_room_service() is not None, "the gateway's own start should run a worker"

plugins.get_plugin_manager().discover_and_load(force=True)
assert GatewayStartupMixin.__dict__["_start_hosted_room_worker_sync"] is not original, "the start method was not replaced"
assert GatewayStartupMixin._start_hosted_room_worker_sync() is None
assert methods_groups.get_hosted_room_service() is None, "the running gateway worker was not stopped"
assert GatewayStartupMixin._start_hosted_room_worker_sync() is None and methods_groups.get_hosted_room_service() is None

# The dashboard starts its worker through methods_groups directly: untouched.
dash = methods_groups.start_hosted_room_service()
assert dash is not None and dash.runtime.status().get("running"), "the dashboard's own start should still work"
methods_groups.stop_hosted_room_service(timeout=5.0)
shutil.rmtree(home, ignore_errors=True)
print("ok: the gateway runs no room worker; the dashboard still does")
