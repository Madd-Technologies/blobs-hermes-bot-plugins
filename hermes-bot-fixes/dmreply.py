"""A teammate's message is answered once, in the reply, never also with message_agent back to the bot that sent it.

In Bot Mode a bot that receives "Message from 🤖 <name> (@<handle>): ..." is told by its prompt to "reply concisely via
message_agent to their handle" (tools/bot_mode_probe.py). But message_agent is fire-and-forget, and the delivery runner's
output, which is the recipient's reply for that turn, already goes back to the sender as the completion notification that
wakes it (tools/bot_mode_dm.py: ``_run_local_turn``, ``_wait_live_dm``). So every answer arrives twice: once as that
wake-up and once as a new "Message from" turn. The sender, busy with its own turn, gets both queued and then runs one short
turn after another, and its reply to the second one is itself delivered back, waking the first bot again.

Now a message_agent whose target is the bot whose message started the current turn is not sent: the bot is told that its
reply goes back to that bot by itself, and to put what it wanted to say there. A message to any other bot, and a message
to that bot from any other turn (a wake-up, a routine, the person), is sent as before.

The bot that started the turn is the turn's author (``agent._turn_author``, ``{"id": "bot:<profile>", ...}``), which
Hermes sets for every Bot Mode delivery on this install: through the open chat's mailbox and in the one-shot
``hermes chat`` turn alike. A teammate on another machine or peer gateway (``bot:<origin>/<profile>``) is left alone.

The wrap sits on ``tools.bot_mode_dm.message_agent_tool``, which Hermes looks up by name on every call
(agent/inline_tool_executors.py). It checks the function's shape first and leaves anything unexpected alone (logged).
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
from pathlib import Path

log = logging.getLogger("hermes-bot-fixes")

MARK = "__hermes_bot_fixes__"

#: message_agent_tool's parameters in the Hermes this fixes; anything else is left alone.
PARAMS = ["target", "message", "task_id", "agent"]

#: The bot_mode_dm helpers the target is resolved with, exactly as message_agent resolves it.
HELPERS = ("_agent_home", "_resolve_local_name")


def asker(agent) -> tuple[str, str] | None:
    """``(profile, handle)`` of the bot on this install whose message started the agent's current turn, or None."""
    author = getattr(agent, "_turn_author", None)

    if not isinstance(author, dict) or author.get("is_bot") is not True:
        return None

    ident = str(author.get("id") or "")

    if not ident.startswith("bot:"):
        return None

    profile = ident[len("bot:"):]

    # "bot:<origin>/<profile>" is a bot on another machine or peer gateway.
    if not profile or "/" in profile:
        return None

    return profile, str(author.get("name") or profile)


def target_profile(module, target, agent) -> str | None:
    """The profile on this install that ``target`` names, resolved the way message_agent resolves it, or None when it
    names a peer gateway, an agent on another connection, or nobody."""
    from tools.bot_mode_probe import _hermes_root, _peers, _roster

    raw = str(target or "").strip().lstrip("@")

    # '<peer>/<agent>' and '<handle>@<connection>' go to other machines first.
    if not raw or "/" in raw or "@" in raw:
        return None

    root = _hermes_root(Path(module._agent_home(agent)))

    if raw.lower() in _peers(root):
        return None

    return module._resolve_local_name(raw, [name for name, _home in _roster(root)], root)


def refusal(handle: str) -> str:
    """What the bot reads instead of a delivery acknowledgement."""
    return json.dumps({
        "status": "not_sent",
        "error": (
            f"Not sent: @{handle} sent the message you are answering, and your reply in this chat goes back to "
            f"@{handle} by itself when this turn ends. Sending it with message_agent as well would reach them twice. "
            f"Write what you wanted to tell @{handle} as your reply instead, and do not message @{handle} again in "
            "this turn."
        ),
        "reason": "reply_goes_back",
    })


def _params(fn) -> list[str]:
    try:
        return list(inspect.signature(fn).parameters)
    except (TypeError, ValueError):
        return []


def patch_message_agent(module) -> bool:
    """Wrap the module's message_agent_tool. True when the wrap is in place (now or already)."""
    original = getattr(module, "message_agent_tool", None)

    if not callable(original):
        log.warning("hermes-bot-fixes: no message_agent tool in this Hermes; reply-once fix not applied")

        return False

    if getattr(original, MARK, False):
        return True

    if _params(original) != PARAMS or not all(callable(getattr(module, name, None)) for name in HELPERS):
        log.warning("hermes-bot-fixes: message_agent differs in this Hermes; reply-once fix not applied")

        return False

    @functools.wraps(original)
    def message_agent_tool(target="", message="", task_id=None, agent=None):
        try:
            sender = asker(agent)
            answering = sender is not None and target_profile(module, target, agent) == sender[0]
        except Exception:
            log.exception("hermes-bot-fixes: checking a message_agent target failed; sending it")
            answering = False

        if answering:
            log.info("hermes-bot-fixes: not sending message_agent to %s, whose message this turn answers", sender[0])

            return refusal(sender[1])

        return original(target=target, message=message, task_id=task_id, agent=agent)

    setattr(message_agent_tool, MARK, True)
    module.message_agent_tool = message_agent_tool

    return True


def apply() -> bool:
    """Apply the fix to the loaded Hermes. Never raises; True when it is in place."""
    try:
        from tools import bot_mode_dm
    except Exception as error:  # not a Hermes with Bot Mode: nothing to fix
        log.info("hermes-bot-fixes: reply-once fix not applied (%s)", error)

        return False

    return patch_message_agent(bot_mode_dm)
