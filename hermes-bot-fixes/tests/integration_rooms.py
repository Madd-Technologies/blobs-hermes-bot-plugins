"""Through a real `hermes gateway run`: with hermes-bot-fixes, the messaging gateway runs no group-room worker; without it,
it does. The gateway loads its plugins before its own startup code, which is what this catches (0.6.0 missed it).

Not part of the unit tests (it starts a gateway from a Hermes checkout at the version being fixed). Run it with that
Hermes's venv, once without the plugin and once with it:

    ~/.hermes/hermes-agent/venv/bin/python -I hermes-bot-fixes/tests/integration_rooms.py without
    ~/.hermes/hermes-agent/venv/bin/python -I hermes-bot-fixes/tests/integration_rooms.py with

HERMES_AGENT=<checkout> picks another Hermes checkout (default ~/.hermes/hermes-agent). Everything runs in a throwaway
HERMES_HOME with no messaging platforms, so the real `~/.hermes` is never read or written. After CHECK_AFTER seconds
(default 25) it prints whether the room worker runs, then ends the gateway.
"""
import os, runpy, shutil, sys, tempfile, threading, time
from pathlib import Path

WITH_PLUGIN = sys.argv[1] == "with"
HERMES = Path(os.environ.get("HERMES_AGENT") or Path.home() / ".hermes" / "hermes-agent")
PLUGIN = Path(__file__).resolve().parent.parent
home = Path(tempfile.mkdtemp(prefix="hermes-bot-fixes-gateway-"))
os.environ["HERMES_HOME"] = str(home)
os.environ["HERMES_KANBAN_HOME"] = str(home)
(home / "plugins").mkdir()
if WITH_PLUGIN:
    shutil.copytree(PLUGIN, home / "plugins" / "hermes-bot-fixes", ignore=shutil.ignore_patterns("tests", "__pycache__"))
    (home / "config.yaml").write_text("plugins:\n  enabled:\n    - hermes-bot-fixes\n")
else:
    (home / "config.yaml").write_text("plugins:\n  enabled: []\n")
sys.path.insert(0, str(HERMES))

def report():
    time.sleep(float(os.environ.get("CHECK_AFTER", "25")))
    groups = sys.modules.get("tui_gateway.methods_groups")
    service = groups.get_hosted_room_service() if groups else None
    startup = sys.modules.get("gateway.run_startup")
    method = startup.GatewayStartupMixin.__dict__.get("_start_hosted_room_worker_sync") if startup else None
    print(f"\n=== REPORT ({'with' if WITH_PLUGIN else 'without'} plugin): room worker {'RUNNING' if service else 'not running'}; "
          f"start method patched: {bool(method and getattr(method.__func__, '__hermes_bot_fixes__', False))}", flush=True)
    try:
        log = (home / "logs" / "agent.log").read_text() if (home / "logs" / "agent.log").exists() else ""
        for line in log.splitlines():
            if "hermes-bot-fixes" in line and ("rooms" in line or "room worker" in line):
                print("LOG:", line[-170:], flush=True)
    except Exception as error:
        print("log read failed", error, flush=True)
    shutil.rmtree(home, ignore_errors=True)
    os._exit(0)

threading.Thread(target=report, daemon=True).start()
sys.argv = ["hermes", "gateway", "run"]
runpy.run_module("hermes_cli.main", run_name="__main__", alter_sys=True)
