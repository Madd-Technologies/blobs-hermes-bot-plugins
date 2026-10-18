"""A message from another bot runs in the recipient's inbox chat ("Bot Inbox"), never in its main chat.

Hermes 0.21.x runs every message_agent from a teammate on this install as a turn in the recipient's main "Bot Chat": into
the open chat's mailbox when the dashboard holds it live, otherwise as a one-shot ``hermes -p <bot> chat -c "Bot Chat"``.
Either way the main chat shows the bot working, a person's own message waits behind the teammate's turn or is mixed into
it, and the whole exchange sits in the chat the person talks to the bot in.

Now a local teammate delivery runs one turn in the recipient's "Bot Inbox" session instead, created on first use
(``chat -c "Bot Inbox" --create-if-missing``). The recipient answers in its reply there; the reply goes back to the
sender as the usual completion wake-up in the chat that sent it. The inbox is not "Bot Chat", so the recipient has no
message_agent there and cannot start a ping-pong.

The wrap sits on ``tools.bot_mode_dm._start_delivery``, which message_agent looks up by name on every call. For a local
teammate delivery only (``[<hermes cli>, "-p", <bot>, *BOT_CHAT_TURN_ARGS]``, not on stdin) it passes on:

- ``profile_home=None``, so the sender does not admit the message into the main chat's live mailbox;
- an argv that starts with this interpreter and ``inbox_turn.py``. The delivery runner, which no plugin can reach, only
  admits a delivery into a live mailbox or takes the bot's turn lock when argv[0] is ``hermes``
  (``_local_delivery_home``, ``_delivery_lock``), so it runs our argv as it is, through ``_run_local_turn``: the
  ``--query-file`` it appends, its one policy-gated retry, its busy handling and the reply on stdout are all Hermes's.
  ``inbox_turn.py`` takes the bot's turn lock and gives the turn the recipient's own environment, as the runner would.

Peers, other connections and relayed deliveries are left alone, and so are routines and Kanban (they never pass through
here). ``HERMES_BOT_FIXES_BOT_INBOX=off`` switches the inbox off; Hermes's own delivery then runs as it always did. It
checks the function's shape first and leaves anything unexpected alone (logged).
"""

from __future__ import annotations

import functools
import inspect
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger("hermes-bot-fixes")

MARK = "__hermes_bot_fixes__"

#: The inbox session's title, in every bot that other bots message.
INBOX_TITLE = "Bot Inbox"

#: The switch: any of these turns the inbox off (read on every delivery; the process reads its .env when it starts).
SWITCH = "HERMES_BOT_FIXES_BOT_INBOX"
OFF = ("0", "off", "false", "no")

#: The runner the delivery runs instead of ``hermes``.
TURN = Path(__file__).resolve().with_name("inbox_turn.py")

#: _start_delivery's parameters in the Hermes this fixes; anything else is left alone.
PARAMS = ["argv", "content", "label", "stdin_file", "task_id", "agent", "profile_home", "author"]

#: One turn in the main chat (``tools.bot_relay.BOT_CHAT_TURN_ARGS``), and the same turn in the inbox.
BOT_CHAT_TURN_ARGS = ("chat", "--in", "~", "-c", "Bot Chat", "--create-if-missing", "-Q")
INBOX_TURN_ARGS = ("chat", "--in", "~", "-c", INBOX_TITLE, "--create-if-missing", "-Q")

#: The bot_mode_dm helpers inbox_turn.py and the runner check rely on.
HELPERS = ("_default_home", "_local_delivery_home")

#: The bot_relay helpers inbox_turn.py runs the turn with.
RELAY = ("acquire_turn_lock", "delivery_env", "TurnBusyError", "RESUME_UNANSWERED_TURN_ENV")


def enabled(environ=os.environ) -> bool:
    """Whether teammate messages go to the inbox (on unless the switch says off)."""
    return environ.get(SWITCH, "").strip().lower() not in OFF


def teammate(argv, stdin_file) -> str | None:
    """The bot a delivery runs a main-chat turn for when it is a local teammate delivery, else None."""
    if stdin_file or not isinstance(argv, (list, tuple)) or len(argv) != 3 + len(BOT_CHAT_TURN_ARGS):
        return None

    # argv[0] may be an absolute path, and carries .exe on Windows (the same test as Hermes's own).
    cli = str(argv[0]).rsplit("\\", 1)[-1].rsplit("/", 1)[-1]

    if cli not in ("hermes", "hermes.exe") or argv[1] != "-p" or tuple(argv[3:]) != BOT_CHAT_TURN_ARGS:
        return None

    return str(argv[2]) or None


def inbox_argv(argv, agent_dir, profile_home) -> list[str]:
    """The delivery argv that runs the same turn in the bot's inbox, through inbox_turn.py."""
    return [
        sys.executable, str(TURN), "--hermes-agent", str(agent_dir), "--profile-home", str(profile_home),
        str(argv[0]), "-p", str(argv[2]), *INBOX_TURN_ARGS,
    ]


def _params(fn) -> list[str]:
    try:
        return list(inspect.signature(fn).parameters)
    except (TypeError, ValueError):
        return []


def patch_start_delivery(module, relay) -> bool:
    """Wrap the module's _start_delivery. True when the wrap is in place (now or already)."""
    original = getattr(module, "_start_delivery", None)

    if not callable(original):
        log.warning("hermes-bot-fixes: no message delivery in this Hermes; bot inbox not applied")

        return False

    if getattr(original, MARK, False):
        return True

    if (
        _params(original) != PARAMS
        or tuple(getattr(relay, "BOT_CHAT_TURN_ARGS", ())) != BOT_CHAT_TURN_ARGS
        or not all(callable(getattr(module, name, None)) for name in HELPERS)
        or not all(getattr(relay, name, None) is not None for name in RELAY)
        or not TURN.is_file()
    ):
        log.warning("hermes-bot-fixes: message delivery differs in this Hermes; bot inbox not applied")

        return False

    agent_dir = Path(module.__file__).resolve().parent.parent

    @functools.wraps(original)
    def start_delivery(argv, content, label, *, stdin_file, task_id, agent, profile_home=None, author=None):
        routed = None

        try:
            name = teammate(argv, stdin_file) if profile_home is not None and enabled() else None

            if name:
                routed = inbox_argv(argv, agent_dir, profile_home)

                # The runner must see a transport, not a hermes turn it would hand to the main chat's mailbox.
                if module._local_delivery_home(routed) is not None:
                    log.warning("hermes-bot-fixes: this Hermes would still send %s's message to its main chat; bot inbox not used", name)
                    routed = None
        except Exception:
            log.exception("hermes-bot-fixes: routing a teammate's message to the inbox failed; delivering it as Hermes does")
            routed = None

        if routed is None:
            return original(argv, content, label, stdin_file=stdin_file, task_id=task_id, agent=agent, profile_home=profile_home, author=author)

        log.info("hermes-bot-fixes: a message for %s runs in its inbox, not its main chat", name)

        return original(routed, content, label, stdin_file=False, task_id=task_id, agent=agent, profile_home=None, author=author)

    setattr(start_delivery, MARK, True)
    module._start_delivery = start_delivery

    return True


def apply() -> bool:
    """Apply the fix to the loaded Hermes. Never raises; True when it is in place."""
    try:
        from tools import bot_mode_dm, bot_relay
    except Exception as error:  # not a Hermes with Bot Mode: nothing to fix
        log.info("hermes-bot-fixes: bot inbox not applied (%s)", error)

        return False

    return patch_start_delivery(bot_mode_dm, bot_relay)
