"""Serves a chat's history light through Hermes's own readers, against a real Hermes checkout.

Not part of the unit tests (it needs a Hermes checkout). Run it with the Hermes venv:

    ~/.hermes/hermes-agent/venv/bin/python hermes-bot-mobile/tests/integration_history.py

`HERMES_AGENT=<checkout>` picks another Hermes checkout (default `~/.hermes/hermes-agent`). It points HERMES_HOME at a
throwaway folder and writes its own chats there, so the real `~/.hermes` is never read or written.
"""

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, os.environ.get("HERMES_AGENT") or str(Path.home() / ".hermes" / "hermes-agent"))

home = Path(tempfile.mkdtemp(prefix="hermes-bot-mobile-history-"))
os.environ["HERMES_HOME"] = str(home)

from hermes_state import SessionDB  # noqa: E402

CHAT, OTHER = "20261101_090000_a1b2c3", "20261101_090500_d4e5f6"
db = SessionDB(home / "state.db")
db.create_session(CHAT, "tui")
db.create_session(OTHER, "tui")
outputs: dict[int, str] = {}

for turn in range(4):
    db.append_message(CHAT, "user", f"question {turn}", api_content=f"[context the model saw]\nquestion {turn}")

    for step in range(3):
        call = f"call_{turn}_{step}"
        db.append_message(CHAT, "assistant", "", tool_calls=[{"id": call, "type": "function", "function": {"name": "terminal", "arguments": json.dumps({"command": f"ls /srv/{step}"})}}], reasoning_details=[{"type": "reasoning.encrypted", "data": "x" * 6000}])
        output = "\n".join(f"line {n}" for n in range(800)) + f"\nend {turn}.{step}"
        outputs[db.append_message(CHAT, "tool", json.dumps({"output": output, "exit_code": 0}), tool_call_id=call, tool_name="terminal")] = output

    db.append_message(CHAT, "assistant", f"answer {turn}", reasoning="thinking it over")

foreign = db.append_message(OTHER, "user", "a different chat")
db.close()

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from hermes_cli.web_routers import sessions as hermes_sessions  # noqa: E402

spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_hermes-bot-mobile-history", PLUGIN / "dashboard" / "plugin_api.py")
api = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = api
spec.loader.exec_module(api)

app = FastAPI()
app.include_router(api.router, prefix="/api/plugins/hermes-bot-mobile")
client = TestClient(app)
base = "/api/plugins/hermes-bot-mobile"

assert "history" in api.FEATURES, api.FEATURES

# What the app reads today, for comparison: Hermes's own route, the same rows whole.
whole = asyncio.run(hermes_sessions.get_session_messages(CHAT, profile=None, limit=500, offset=0, order="latest", include_compacted=True, inline_images=True))
whole_size = len(json.dumps(whole["messages"]))

first = client.get(f"{base}/history/{CHAT}", params={"turns": 2})
assert first.status_code == 200, first.text
page = first.json()
rows = page["messages"]
assert rows[0]["content"] == "question 2" and rows[-1]["content"] == "answer 3", [row.get("content") for row in rows]
assert page["pagination"]["turns"] == 2 and page["pagination"]["has_older"] is True, page["pagination"]
assert all(key not in row for row in rows for key in ("api_content", "reasoning", "reasoning_details")), "never-drawn fields dropped"

tools = [row for row in rows if row["role"] == "tool"]
assert all(row.get("omitted") for row in tools), "every long output is marked as cut"
assert json.loads(tools[-1]["content"])["output"].endswith("end 3.2"), "the last line survives"
assert json.loads(tools[-1]["content"])["exit_code"] == 0

slim_size = len(json.dumps(rows))
half_whole = whole_size / 2  # two of the four turns
print(f"two turns: {slim_size} characters light, about {int(half_whole)} whole")
assert slim_size < half_whole / 4, (slim_size, half_whole)

older = client.get(f"{base}/history/{CHAT}", params={"turns": 2, "offset": page["pagination"]["returned"]}).json()
assert older["messages"][0]["content"] == "question 0" and older["messages"][-1]["content"] == "answer 1", [row.get("content") for row in older["messages"]]
assert older["pagination"]["has_older"] is False, older["pagination"]

wanted = [tools[-1]["id"], tools[0]["id"], foreign]
full = client.get(f"{base}/history/{CHAT}/rows", params={"ids": ",".join(str(row_id) for row_id in wanted)})
assert full.status_code == 200, full.text
by_id = {row["id"]: row for row in full.json()["messages"]}
assert set(by_id) == {tools[-1]["id"], tools[0]["id"]}, f"another chat's row is never handed back: {sorted(by_id)}"
assert json.loads(by_id[tools[-1]["id"]]["content"])["output"] == outputs[tools[-1]["id"]], "the whole output comes back"
assert "api_content" not in by_id[tools[0]["id"]]

missing = client.get(f"{base}/history/20261101_000000_000000")
assert missing.status_code == 404, missing.status_code

print("light history: ok")
