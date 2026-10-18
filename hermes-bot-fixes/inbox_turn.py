"""Runs one teammate's message as a turn in the recipient bot's inbox chat (see inbox.py).

Hermes's delivery runner (``tools/bot_mode_dm.py --run-delivery``) starts this in place of ``hermes``, with
``--query-file <message>`` appended:

    <python> inbox_turn.py --hermes-agent <Hermes checkout> --profile-home <bot's home> \\
        <hermes cli> -p <bot> chat --in ~ -c "Bot Inbox" --create-if-missing -Q --query-file <message>

It does for that turn what the runner does for a main-chat turn: it holds the bot's turn lock
(``tools.bot_relay.acquire_turn_lock``, the lock every delivery, relay and room turn into that bot takes), so messages
from several bots queue instead of colliding on the inbox, and gives the turn the bot's own environment
(``delivery_env`` for its home: its secrets, never the sender's, and the sender's turn author). The hermes turn's stdout,
stderr and exit code pass straight through: its stdout is the reply the runner sends back to the sender. When the lock
stays taken past ``bot_mode.turn_wait_seconds``, it prints the runner's own target_busy refusal and exits 1.

A single-query turn (``-Q``) follows ``approvals.single_query_mode`` for dangerous commands (deny by default), and
nothing in it can wait for a person.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def split(args: list[str]) -> tuple[str, str, list[str]] | None:
    """``(Hermes checkout, bot's home, hermes argv)``, or None when the arguments are not this runner's."""
    if len(args) < 7 or args[0] != "--hermes-agent" or args[2] != "--profile-home":
        return None

    agent_dir, profile_home, argv = args[1], args[3], args[4:]

    if len(argv) < 3 or argv[1] != "-p" or not argv[2]:
        return None

    return agent_dir, profile_home, argv


def main(args: list[str]) -> int:
    parsed = split(args)

    if parsed is None:
        print(json.dumps({"error": "Delivery failed: the bot inbox runner was started with arguments it does not "
                                   "understand, so the message was NOT delivered.", "reason": "unknown"}))

        return 2

    agent_dir, profile_home, argv = parsed

    # Started by path with the sender's interpreter, like the delivery runner: boot the same way before any Hermes
    # import (it activates Hermes's dependencies when they are not on this interpreter's path). This plugin's folder,
    # sys.path[0] for a script, goes first, so none of its modules can stand in for one of Hermes's.
    here = Path(__file__).resolve().parent
    sys.path[:] = [agent_dir, *(entry for entry in sys.path if Path(entry or ".").resolve() != here)]

    import hermes_bootstrap  # noqa: F401
    from agent.turn_author import turn_author_from_env
    from tools.bot_failure_reasons import delivery_failure_reason
    from tools.bot_mode_dm import _default_home
    from tools.bot_mode_probe import _hermes_root
    from tools.bot_relay import RESUME_UNANSWERED_TURN_ENV, TurnBusyError, acquire_turn_lock, delivery_env

    # The runner built our environment without the bot's home (that is what kept the message out of the main chat);
    # build the bot's own now, as the runner would have. The turn's author and the retry marker ride along.
    env = delivery_env(turn_author_from_env(), profile_home)

    if os.environ.get(RESUME_UNANSWERED_TURN_ENV):
        env[RESUME_UNANSWERED_TURN_ENV] = os.environ[RESUME_UNANSWERED_TURN_ENV]

    try:
        with acquire_turn_lock(_hermes_root(Path(_default_home())), argv[2]):
            return subprocess.run(argv, stdin=subprocess.DEVNULL, env=env, check=False).returncode
    except TurnBusyError as error:
        # The same refusal the runner prints when its own lock wait runs out.
        print(json.dumps({"error": str(error), "reason": delivery_failure_reason(error)}))

        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
