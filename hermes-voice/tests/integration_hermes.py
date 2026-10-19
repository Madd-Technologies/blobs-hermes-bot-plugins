"""Loads hermes-voice through a Hermes checkout's own plugin manager and dashboard, and makes a call secret with Hermes's real
sign-in reader (a fake grant in a throwaway home; xAI is replaced by a fake, so nothing leaves this machine).

Not part of the unit tests (it needs a Hermes checkout). Run it with the Hermes venv:

    ~/.hermes/hermes-agent/venv/bin/python hermes-voice/tests/integration_hermes.py

`HERMES_AGENT=<checkout>` picks another Hermes checkout (default `~/.hermes/hermes-agent`). HERMES_HOME points at a throwaway
folder, so the real `~/.hermes` is never read or written.
"""

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent
CHECKOUT = Path(os.environ.get("HERMES_AGENT") or Path.home() / ".hermes" / "hermes-agent")
sys.path.insert(0, str(CHECKOUT))

home = Path(tempfile.mkdtemp(prefix="hermes-voice-it-"))
os.environ["HERMES_HOME"] = str(home)
for name in ("XAI_API_KEY", "HERMES_XAI_BASE_URL", "XAI_BASE_URL"):
    os.environ.pop(name, None)

(home / "plugins").mkdir()
(home / "profiles" / "harbor").mkdir(parents=True)
shutil.copytree(PLUGIN, home / "plugins" / "hermes-voice", ignore=shutil.ignore_patterns("tests", "__pycache__"))
(home / "config.yaml").write_text("plugins:\n  enabled:\n    - hermes-voice\n")


def b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


# A grant that is not expiring, so Hermes's reader never asks xAI to refresh it.
ACCESS = f"{b64({'alg': 'none'})}.{b64({'exp': int(time.time()) + 6 * 3600, 'sub': 'it'})}.sig"
(home / "auth.json").write_text(json.dumps({"version": 1, "providers": {"xai-oauth": {"tokens": {
    "access_token": ACCESS, "refresh_token": "refresh-not-real", "token_type": "Bearer"}}}}))

validate = subprocess.run([str(CHECKOUT / "venv" / "bin" / "hermes"), "plugins", "validate", str(home / "plugins" / "hermes-voice")],
                          capture_output=True, text=True, env={**os.environ})
if "invalid choice: 'validate'" in validate.stderr:
    print("validate: this Hermes has no `plugins validate` (it came after 0.21.0); skipped")
else:
    print("validate:", validate.returncode, (validate.stdout + validate.stderr).strip()[-600:])
    assert validate.returncode == 0, "hermes plugins validate failed"

from hermes_cli.plugins import get_plugin_manager  # noqa: E402

manager = get_plugin_manager()
manager.discover_and_load(force=True)
loaded = dict(manager._plugins)
print("loaded plugins:", sorted(loaded))
assert "hermes-voice" in loaded, "Hermes did not load the plugin"
assert getattr(loaded["hermes-voice"], "error", None) in (None, ""), loaded["hermes-voice"].error

from fastapi.testclient import TestClient  # noqa: E402

from hermes_cli import web_server  # noqa: E402  (mounts plugin API routes on import)

paths = {getattr(route, "path", "") for route in web_server.app.routes}
assert "/api/plugins/hermes-voice/bot" in paths, sorted(p for p in paths if "plugins" in p)

api = next(m for m in list(sys.modules.values()) if str(getattr(m, "__file__", "")).endswith("hermes-voice/dashboard/plugin_api.py"))
asked = []


def fake_xai(url, headers, payload, timeout):
    asked.append((url, headers["Authorization"], payload))
    return 200, {"value": "client-secret-not-real", "expires_at": int(time.time()) + 60}


api.poster = fake_xai
client = TestClient(web_server.app, base_url="http://127.0.0.1")
client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN

status = client.get("/api/plugins/hermes-voice/status")
print("status:", status.status_code, status.json())
assert status.json()["version"] == "0.1.0"

bot = client.get("/api/plugins/hermes-voice/bot", params={"profile": "default"}).json()
print("default:", bot)
assert (bot["account"], bot["ready"], bot["available"]) == ("sign-in", True, {"sign_in": True, "key": False}), bot

harbor = client.get("/api/plugins/hermes-voice/bot", params={"profile": "harbor"}).json()
print("harbor (no sign-in of its own, so the root one):", harbor)
assert harbor["available"]["sign_in"] is True, harbor

changed = client.put("/api/plugins/hermes-voice/bot", params={"profile": "harbor"}, json={"voice": "rex"}).json()
assert changed["voice"] == "rex", changed
assert json.loads((home / "hermes-voice" / "bots.json").read_text()) == {"bots": {"harbor": {"voice": "rex"}}}

call = client.post("/api/plugins/hermes-voice/session", params={"profile": "harbor"}, json={"name": "Harbor"})
print("session:", call.status_code, {k: v for k, v in call.json().items() if k not in ("session", "client_secret")})
assert call.status_code == 200, call.text
assert asked and asked[0][1] == f"Bearer {ACCESS}", "the call was not made with Hermes's sign-in"
assert ACCESS not in call.text and "refresh-not-real" not in call.text
assert call.json()["session"]["voice"] == "rex"

unknown = client.get("/api/plugins/hermes-voice/bot", params={"profile": "juniper"})
assert unknown.status_code == 404, unknown.text

print("auth.json untouched:", json.loads((home / "auth.json").read_text())["providers"]["xai-oauth"]["tokens"]["access_token"] == ACCESS)
shutil.rmtree(home)
print("OK")
