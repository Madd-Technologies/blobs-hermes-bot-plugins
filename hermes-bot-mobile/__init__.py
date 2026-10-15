"""hermes-bot-mobile: the HermesBot phone app's companion on this server.

Standalone plugin, formerly hermes-push (it carries that plugin's registered phones over, see `store.carry_over`):

- Pushes: it listens to the agent's own lifecycle hooks (an approval is waiting, a question was asked, a routine
  broke) and sends a small encrypted Web Push message to every phone registered through the dashboard API
  in `dashboard/plugin_api.py`. The phone's UnifiedPush distributor (for example ntfy) delivers it.
- Room activity: it keeps what each bot in a group room is doing right now (running a tool, thinking, writing, waiting
  for your OK) from Hermes's `on_room_member_activity` hook, for the app to show; see `activity.py`.

Nothing here holds the bot up: see `hooks.py`.
"""

from __future__ import annotations

import logging

log = logging.getLogger("hermes-bot-mobile")


def register(ctx) -> None:
    from .activity import RoomActivity
    from .hooks import PushHooks

    hooks = PushHooks()

    for name, callback in hooks.callbacks().items():
        ctx.register_hook(name, callback)

    watched = list(hooks.callbacks())

    # Live room activity needs Hermes 0.21.5+ (the hook); older ones keep the pushes and the room shows "working".
    try:
        ctx.register_hook("on_room_member_activity", RoomActivity().record)
        watched.append("on_room_member_activity")
    except Exception as error:
        log.info("hermes-bot-mobile: no live room activity on this Hermes (%s)", error)

    log.info("hermes-bot-mobile: watching %s", ", ".join(watched))
