"""One teammate message as a turn in the recipient's inbox, on Hermes 0.22: the delivery runner's transport for inbox.py.

Hermes 0.22's gateway owns every local session and delivers a teammate's message only into the recipient's main "Bot Chat".
Its bot-to-bot (A2A) door, ``a2a.forward``, is the one gateway call that runs a turn in another session of the recipient: one
session per (agent, tenant, peer, context), created on first use and kept, the turn queued behind anything already running
there, and the reply returned. ``gateway.session_a2a.forward_to_owner`` is its client and starts the recipient's gateway
when none runs. The inbox is not Bot Chat, so the recipient has no message_agent there and cannot start a ping-pong.

The delivery runner (``tools.bot_mode_dm._run_delivery``, stdin mode) starts this with the message on stdin, and what it
prints becomes the sender's completion notification, as the recipient's reply always was.

    inbox_a2a.py --hermes-agent <checkout> --profile-home <recipient home> --recipient <bot> --peer <bot:sender>
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

#: The conversation every teammate message to a bot continues: one inbox per sending bot (the peer).
CONTEXT = "bot-inbox"

#: How long the sender's runner waits for the reply. A timeout never cancels accepted work; the reply is then lost to the
#: sender, as with any other undelivered completion.
TIMEOUT_S = 30 * 60


def reply_of(receipt: dict) -> str:
    """The recipient's answer from the forward's receipt, or a line saying there was none."""
    result = receipt.get("result") if isinstance(receipt, dict) else None
    reply = str((result or {}).get("final_response") or "").strip()

    return reply or "(The bot finished without a reply.)"


def main(argv=None, stdin=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hermes-agent", required=True)
    parser.add_argument("--profile-home", required=True)
    parser.add_argument("--recipient", required=True)
    parser.add_argument("--peer", required=True)
    parser.add_argument("--timeout", type=float, default=TIMEOUT_S)
    args = parser.parse_args(argv)
    text = (stdin or sys.stdin).read()

    if not text.strip():
        print("Nothing to deliver.")

        return 1

    if args.hermes_agent not in sys.path:
        sys.path.insert(0, args.hermes_agent)

    from gateway.session_a2a import forward_to_owner

    try:
        receipt = asyncio.run(forward_to_owner(
            args.profile_home, agent=args.recipient, tenant="", peer=args.peer, context_id=CONTEXT,
            input_id=uuid.uuid4().hex, text=text, timeout=args.timeout,
        ))
    except Exception as error:
        print(f"Delivery to {args.recipient} failed: {type(error).__name__}: {error}")

        return 1

    print(reply_of(receipt))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
