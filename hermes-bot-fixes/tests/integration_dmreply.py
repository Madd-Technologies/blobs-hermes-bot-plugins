"""Sends message_agent through Hermes's own tool dispatch and name resolution, before and after the reply-once fix, with no
model call and no delivery (the delivery step is replaced by a recorder).

Not part of the unit tests (it needs a Hermes checkout at the version being fixed). Run it with a Hermes venv:

    ~/.hermes/hermes-agent/venv/bin/python hermes-bot-fixes/tests/integration_dmreply.py <path to a Hermes checkout>

It points HERMES_HOME at a throwaway folder with three Bot Mode bots, so the real `~/.hermes` is never read or written.
The turn's author is set the way Hermes sets it for a teammate's message (``agent._turn_author``).
"""

import json
import os
import sys
import tempfile
import textwrap
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else str(Path.home() / ".hermes" / "hermes-agent"))
sys.path.insert(0, str(PLUGIN))
root = Path(tempfile.mkdtemp(prefix="hermes-bot-fixes-dmreply-"))
os.environ["HERMES_HOME"] = str(root)
for name in ("HERMES_PROFILE", "HERMES_PROFILE_NAME", "HERMES_TURN_AUTHOR"):
    os.environ.pop(name, None)

for profile, title in (("orion", "Night Sky"), ("juniper", "Juniper"), ("harbor", "Harbor")):
    home = root / "profiles" / profile
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("model:\n  default: none\n", encoding="utf-8")
    (home / "profile.yaml").write_text(textwrap.dedent(f"""\
        description: throwaway teammate
        display_name: {title}
        ui_meta:
          hermes-bots:
            shape: cloud
        """), encoding="utf-8")

from agent.inline_tool_executors import INLINE_TOOL_EXECUTORS, InlineToolContext  # noqa: E402
from tools import bot_mode_dm  # noqa: E402

import dmreply  # noqa: E402

delivered = []


def record_delivery(argv, content, label, **kw):
    delivered.append(label)

    return json.dumps({"status": "queued", "to": label})


bot_mode_dm._start_delivery = record_delivery


class SessionDB:
    def __init__(self, home):
        self.db_path = str(home / "state.db")

    def get_session_title(self, _sid):
        return "Bot Chat"


class Agent:
    """Juniper's Bot Chat, in a turn that ``author`` started."""

    def __init__(self, author):
        self._session_db = SessionDB(root / "profiles" / "juniper")
        self.session_id = "bot-chat"
        self._session_title_hint = None
        self._bot_mode_protocol = True
        self._turn_author = author
        self.tools, self.valid_tool_names = [], set()


ORION = {"id": "bot:orion", "name": "orion", "is_bot": True}
DEFAULT = {"id": "bot:default", "name": "hermes", "is_bot": True}
ELSEWHERE = {"id": "bot:spark/orion", "name": "orion", "is_bot": True}


def send(target, author):
    """What message_agent returns, through the dispatch Hermes runs for a model's tool call."""
    before = len(delivered)
    result = json.loads(INLINE_TOOL_EXECUTORS["message_agent"](Agent(author), {"target": target, "message": "Done: it exited 0."}, InlineToolContext("t1")))

    return result, delivered[before:]


result, sent = send("orion", ORION)
assert sent == ["@orion"], (result, sent)
print("before: answering Orion's message, message_agent to orion is sent:", result["status"])

assert dmreply.apply(), "fix not applied"
assert dmreply.apply(), "applying twice"

for target in ("orion", "@orion", "ORION", "Night Sky"):
    result, sent = send(target, ORION)
    assert sent == [] and result["status"] == "not_sent" and "@orion" in result["error"], (target, result, sent)
print("after: answering Orion's message, message_agent to orion, @orion, ORION or its name Night Sky is not sent")
print("       the bot reads:", result["error"][:120], "...")

result, sent = send("hermes", DEFAULT)
assert sent == [] and "@hermes" in result["error"], (result, sent)
print("after: answering the default bot's message, message_agent to @hermes is not sent")

result, sent = send("harbor", ORION)
assert sent == ["@harbor"], (result, sent)
print("unchanged: answering Orion's message, message_agent to harbor is sent")

for author, why in ((None, "a wake-up, a routine or the person"), ({"id": "user", "name": "Alex", "is_bot": False}, "a person"), (ELSEWHERE, "a bot on another machine")):
    result, sent = send("orion", author)
    assert sent == ["@orion"], (author, result, sent)
    print(f"unchanged: in a turn started by {why}, message_agent to orion is sent")

result, sent = send("juniper", ORION)
assert sent == [] and "yourself" in result["error"], (result, sent)
print("unchanged: Hermes's own refusals still apply (messaging yourself)")
print("OK")
