"""Loads hermes-phone through a Hermes checkout's own plugin manager and dashboard, runs a brief from the tool to the approval
route, checks the Hermes functions the call service leans on are there, and starts the call service itself. Nothing is
dialed and nothing leaves this machine: the Twilio keys are fake, and the calling hours are set to leave out this hour, so
the dialer only waits.

Not part of the unit tests (it needs a Hermes checkout). Run it with the Hermes venv:

    ~/.hermes/hermes-agent/venv/bin/python hermes-phone/tests/integration_hermes.py

`HERMES_AGENT=<checkout>` picks another Hermes checkout or source tree (default `~/.hermes/hermes-agent`; it needs 0.21.6 or
later, like the plugin). HERMES_HOME points at a throwaway folder, so the real `~/.hermes` is never read or written.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent
CHECKOUT = Path(os.environ.get("HERMES_AGENT") or Path.home() / ".hermes" / "hermes-agent")
sys.path.insert(0, str(CHECKOUT))
sys.path.insert(1, str(HERE))

home = Path(tempfile.mkdtemp(prefix="hermes-phone-it-"))
os.environ["HERMES_HOME"] = str(home)
(home / "plugins").mkdir()
shutil.copytree(PLUGIN, home / "plugins" / "hermes-phone", ignore=shutil.ignore_patterns("tests", "__pycache__"))
(home / "config.yaml").write_text("plugins:\n  enabled:\n    - hermes-phone\n")
(home / ".env").write_text("TWILIO_ACCOUNT_SID=ACnotreal\nTWILIO_AUTH_TOKEN=not-real\nTWILIO_PHONE_NUMBER=+1 (202) 555-0123\n")
#: Hermes's CLI and the call service run on this interpreter, with the checkout first on the path.
child_env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(CHECKOUT), os.environ.get("PYTHONPATH", "")]).rstrip(os.pathsep)}

validate = subprocess.run([sys.executable, "-m", "hermes_cli.main", "plugins", "validate", str(home / "plugins" / "hermes-phone")],
                          capture_output=True, text=True, env=child_env)
if "invalid choice: 'validate'" in validate.stderr:
    print("validate: this Hermes has no `plugins validate` (it came after 0.21.0); skipped")
else:
    print("validate:", validate.returncode, (validate.stdout + validate.stderr).strip()[-600:])
    assert validate.returncode == 0, "hermes plugins validate failed"

from hermes_cli.plugins import get_plugin_manager  # noqa: E402

manager = get_plugin_manager()
manager.discover_and_load(force=True)
loaded = dict(manager._plugins)
assert "hermes-phone" in loaded, sorted(loaded)
plugin = loaded["hermes-phone"]
assert getattr(plugin, "error", None) in (None, ""), plugin.error
print("tools:", plugin.tools_registered)
assert sorted(plugin.tools_registered) == ["phone_call", "phone_call_report"], plugin.tools_registered

from tools.registry import registry  # noqa: E402

entry = registry.get_entry("phone_call")
assert entry is not None and entry.toolset == "phone", entry

from fastapi.testclient import TestClient  # noqa: E402

from hermes_cli import web_server  # noqa: E402  (mounts plugin API routes on import)

client = TestClient(web_server.app, base_url="http://127.0.0.1")
client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
base = "/api/plugins/hermes-phone"

status = client.get(f"{base}/status").json()
print("status:", status)
assert status["version"] == "0.1.1" and status["line"] == {"set": True, "number": "…23"}, status

settings = client.put(f"{base}/settings", json={"name": "Alex", "never_share_add": [{"label": "Home address", "value": "12 Orchard Lane, Pinecrest"}],
                                                "test_numbers": ["202-555-0101"]}).json()
assert settings["never_share"] == [{"label": "Home address", "hint": "…st"}], settings

from helpers import septic  # noqa: E402

facts = septic()["facts"] + [{"label": "Service address", "value": "12 Orchard Lane, Pinecrest", "only_if_asked": True}]
made = json.loads(entry.handler(septic(facts=facts), session_id="it-session"))
print("phone_call:", made)
assert made["status"] == "waiting_for_ok" and made["flagged_for_their_tick"], made

refused = client.post(f"{base}/errands/{made['errand_id']}/approve", json={})
assert refused.status_code == 409, refused.text
approved = client.post(f"{base}/errands/{made['errand_id']}/approve", json={"ticked": [3]}).json()
assert approved["status"] == "approved" and "token" not in approved["calls"][0], approved

report = json.loads(registry.get_entry("phone_call_report").handler({"errand_id": made["errand_id"]}))
assert report["errands"][0]["status"] == "approved", report

from tools.bot_live_delivery import deliver_to_live_owner, find_canonical_live_owner, find_canonical_owner  # noqa: E402,F401
from tools.bot_relay import acquire_turn_lock  # noqa: E402,F401
from hermes_cli.auth import resolve_xai_oauth_runtime_credentials  # noqa: E402,F401

print("Bot Chat delivery and the sign-in reader import")

with socket.socket() as probe:
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
hour = time.localtime().tm_hour
closed = [0, hour] if hour else [1, 24]  # any window without this hour: the dialer waits instead of calling Twilio
client.put(f"{base}/settings", json={"port": port, "public_url": "https://node.example.ts.net:8443", "hours": closed})
service = subprocess.Popen([sys.executable, str(home / "plugins" / "hermes-phone" / "run.py")],
                           env=child_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
try:
    for _ in range(60):
        try:
            health = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1).read())
            break
        except OSError:
            time.sleep(0.25)
    else:
        raise AssertionError("the call service never answered /health")
    print("service /health:", health)
    for _ in range(40):
        if client.get(f"{base}/status").json()["service"]["running"]:
            break
        time.sleep(0.25)
    else:
        raise AssertionError("the service's heartbeat never reached the app's status")
    errand = client.get(f"{base}/errands/{made['errand_id']}").json()
    print("after the first steps:", errand["status"], [c["status"] for c in errand["calls"]], errand.get("problem"))
    assert "calling hours" in (errand.get("problem") or "") and errand["calls"][0]["status"] == "waiting", errand
finally:
    service.terminate()
    output = service.communicate(timeout=10)[0]
print("service log:", output.strip()[-800:])
assert "not-real" not in output and "/twilio/stream/" not in output, "the service logged a secret or a call path"

shutil.rmtree(home)
print("OK")
