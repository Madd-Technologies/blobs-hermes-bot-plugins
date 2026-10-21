"""The dashboard runs every group room; the messaging gateway runs none.

Hermes 0.21.6 starts a group-room worker in two processes: the dashboard (``hermes dashboard``, the server the app talks
to) and the messaging gateway (``hermes gateway run``, ``GatewayStartupMixin._start_post_connect_services``, with no
setting to leave it out). Whichever worker leases a room first runs every member turn in it, so after a restart the
process that came up first owns the rooms. A turn the gateway runs is out of the app's reach: a member's approval waits
in the gateway's own room service (``groups.state`` on the dashboard lists no pending action) and its clarify question in
the gateway's open requests (the dashboard has no session for it), so the app shows neither and the turn waits for the
prompt's timeout while the room looks busy.

The gateway's worker is started, and kept alive every second, through ``GatewayStartupMixin._start_hosted_room_worker_sync``,
which only the messaging gateway calls (the dashboard starts its own through ``methods_groups.start_hosted_room_service``).
This puts a stand-in there that starts nothing and stops a worker already running in the process, without interrupting a
turn it accepted, so the gateway's leases lapse and the dashboard's worker takes the rooms. Group chats on messaging
platforms then fail closed in the gateway, as Hermes does when its worker is down; the app's rooms are unaffected.
``HERMES_BOT_FIXES_GATEWAY_ROOMS=on`` leaves the gateway's worker alone.

The fix is only applied in the messaging gateway: the process started as ``hermes gateway run``, or one where Hermes's
gateway startup code is loaded. The gateway loads its plugins while its own modules are still being imported, before the
startup code that defines that method exists, so the fix waits for it on a small background thread and applies as soon
as it is there; a worker the gateway started first is stopped on its next keep-alive tick. The dashboard never imports
the gateway's code. The fix checks the method's shape first and leaves anything unexpected alone (logged).
"""

from __future__ import annotations

import inspect
import logging
import os
import sys
import threading
import time

log = logging.getLogger("hermes-bot-fixes")

MARK = "__hermes_bot_fixes__"
SWITCH = "HERMES_BOT_FIXES_GATEWAY_ROOMS"
METHOD = "_start_hosted_room_worker_sync"


def enabled() -> bool:
    """False when ``HERMES_BOT_FIXES_GATEWAY_ROOMS=on``: the messaging gateway keeps its own room worker."""
    return os.environ.get(SWITCH, "").strip().lower() not in {"on", "1", "true", "yes"}


def stand_down():
    """Runs in the messaging gateway in place of starting its room worker: starts none, and stops one already running in this
    process (started before the fix was applied), letting a turn it accepted finish. Returns no service."""
    groups = sys.modules.get("tui_gateway.methods_groups")

    if groups is not None and groups.get_hosted_room_service() is not None and groups.stop_hosted_room_service(timeout=5.0):
        log.info("hermes-bot-fixes: the messaging gateway's room worker stopped; the dashboard runs the rooms")

    return None


setattr(stand_down, MARK, True)


def patch_startup(startup) -> bool:
    """Put the stand-in on ``startup.GatewayStartupMixin``. True when it is in place (now or already)."""
    owner = getattr(startup, "GatewayStartupMixin", None)
    current = inspect.getattr_static(owner, METHOD, None) if owner is not None else None

    if isinstance(current, staticmethod) and getattr(current.__func__, MARK, False):
        return True

    if not isinstance(current, staticmethod) or inspect.signature(current.__func__).parameters:
        log.warning("hermes-bot-fixes: gateway rooms fix not applied (%s is not the one it expects)", METHOD)

        return False

    setattr(owner, METHOD, staticmethod(stand_down))

    return True


#: How long the gateway's startup code is waited for after the plugin loads, and how often it is looked for.
WAIT_SECONDS = 600.0
LOOK_EVERY = 0.2


def gateway_process(argv=None) -> bool:
    """Whether this process is the messaging gateway: started as ``hermes gateway run`` (with any flags between)."""
    words = [word for word in (sys.argv if argv is None else argv)[1:] if not word.startswith("-")]

    return any(word == "gateway" and words[index + 1:index + 2] == ["run"] for index, word in enumerate(words))


def loaded_startup():
    """Hermes's gateway startup module once its mixin is defined (a module still being imported has none yet), or None."""
    startup = sys.modules.get("gateway.run_startup")

    return startup if startup is not None and hasattr(startup, "GatewayStartupMixin") else None


def patch_when_loaded(wait: float = WAIT_SECONDS, every: float = LOOK_EVERY, sleep=time.sleep, clock=time.monotonic) -> bool:
    """Wait for the gateway's startup code and apply the fix to it. True once applied; False if it never came."""
    deadline = clock() + wait

    while True:
        startup = loaded_startup()

        if startup is not None:
            try:
                applied = patch_startup(startup)
            except Exception:
                log.exception("hermes-bot-fixes: gateway rooms fix failed")

                return False

            if applied:
                log.info("hermes-bot-fixes: gateway rooms fix applied; the dashboard runs the rooms")

            return applied

        if clock() >= deadline:
            log.warning("hermes-bot-fixes: gateway rooms fix not applied (the gateway's startup code never loaded)")

            return False

        sleep(every)


def apply() -> bool | str:
    """Apply the fix in the messaging gateway. Never raises. True when it is in place, "waiting" while it waits for the
    gateway's startup code to load, False where it does not apply (the dashboard) or is switched off."""
    if not enabled():
        log.info("hermes-bot-fixes: gateway rooms fix switched off (%s=on)", SWITCH)

        return False

    if loaded_startup() is not None:
        return patch_when_loaded(wait=0)

    # The gateway imports its startup code after loading plugins; anywhere else there is nothing to keep out of rooms.
    if not gateway_process() and "gateway.run" not in sys.modules:
        return False

    threading.Thread(target=patch_when_loaded, name="hermes-bot-fixes-gateway-rooms", daemon=True).start()

    return "waiting"
