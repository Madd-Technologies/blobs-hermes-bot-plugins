"""hermes-bot-mobile: the HermesBot phone app's companion on this server.

Standalone plugin, formerly hermes-push (it carries that plugin's registered phones over, see `store.carry_over`):

- Pushes: it listens to the agent's own lifecycle hooks (an approval is waiting, a question was asked, a routine
  broke, a background worker blocked its kanban card on the person, and for a bot set to `all` a reply in one of the
  person's chats) and sends a small encrypted Web Push message
  to every phone registered through the dashboard API in `dashboard/plugin_api.py`. The phone's UnifiedPush distributor
  (for example ntfy) delivers it.
- Room activity: it keeps what each bot in a group room is doing right now (running a tool, thinking, writing, waiting
  for your OK) from Hermes's `on_room_member_activity` hook, for the app to show; see `activity.py`.
- Kanban approvals: in a kanban worker, which runs headless and would refuse every command that needs an OK, it asks the
  person through the app instead, in the chat the card came from; see `kanban_approvals.py`.

Nothing here holds the bot up: see `hooks.py`.
"""

from __future__ import annotations

import logging

log = logging.getLogger("hermes-bot-mobile")


def register(ctx) -> None:
    from .activity import RoomActivity
    from .hooks import PushHooks

    hooks = PushHooks()
    watched = []

    for name, callback in hooks.callbacks().items():
        # One hook this Hermes doesn't have (kanban_task_blocked needs 0.21.5+) must not cost the others.
        try:
            ctx.register_hook(name, callback)
            watched.append(name)
        except Exception as error:
            log.info("hermes-bot-mobile: no %s hook on this Hermes (%s)", name, error)

    # Live room activity needs Hermes 0.21.5+ (the hook); older ones keep the pushes and the room shows "working". One
    # callback keeps both the member's phase (activity.py) and its turn as it streams (live.py).
    try:
        from .live import RoomLive

        phases, turns = RoomActivity(), RoomLive()

        def on_room_member_activity(**kwargs):
            phases.record(**kwargs)
            turns.record(**kwargs)

        ctx.register_hook("on_room_member_activity", on_room_member_activity)
        watched.append("on_room_member_activity")
    except Exception as error:
        log.info("hermes-bot-mobile: no live room activity on this Hermes (%s)", error)

    # Only in a kanban worker (the dispatcher's `hermes chat -q`): anywhere else it does nothing.
    try:
        from .kanban_approvals import WorkerRelay

        WorkerRelay().install()
    except Exception as error:
        log.info("hermes-bot-mobile: kanban approvals not relayed (%s)", error)

    log.info("hermes-bot-mobile: watching %s", ", ".join(watched))
