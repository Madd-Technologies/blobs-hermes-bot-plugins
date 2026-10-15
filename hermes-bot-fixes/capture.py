"""Two fixes to how Hermes's computer_use picks and times its screenshots on a Linux Bot Screen.

- A capture that names no window picks the frontmost one. Xfce's panels sit in the dock layer above every app, so on a
  Bot Screen the default capture was the 28 px top bar (or the dock) instead of what the bot was working in. Panels are
  now left out of that choice, the same way Hermes already leaves out desktop and GNOME helper windows.
- The screenshot that follows an action (`capture_after`) was taken the moment the action returned, so it could show the
  screen before the click took effect. It now waits a short settle first.

Both wrap Hermes functions that its own code looks up by name at call time, so nothing in Hermes is edited, and both
check the function is the one they expect before touching it (anything else is left alone and logged).
"""

from __future__ import annotations

import functools
import inspect
import logging
import os
import sys
import time

log = logging.getLogger("hermes-bot-fixes")

#: Window names that are desktop chrome, never what a bot works in.
PANEL_NAMES = ("xfce4-panel", "xfce4-notifyd", "plank", "tint2", "lxpanel", "polybar")

#: How long the screen gets to settle before the screenshot after an action (HERMES_BOT_FIXES_SETTLE_SECONDS overrides).
SETTLE_SECONDS = 0.75
SETTLE_MAX = 3.0

MARK = "__hermes_bot_fixes__"


def is_panel(window: dict) -> bool:
    text = f"{window.get('app_name', '')} {window.get('title', '')}".lower()

    return any(name in text for name in PANEL_NAMES)


def settle_seconds() -> float:
    raw = os.environ.get("HERMES_BOT_FIXES_SETTLE_SECONDS", "").strip()

    try:
        value = float(raw) if raw else SETTLE_SECONDS
    except ValueError:
        value = SETTLE_SECONDS

    return max(0.0, min(value, SETTLE_MAX))


def _params(fn) -> list[str]:
    try:
        return list(inspect.signature(fn).parameters)
    except (TypeError, ValueError):
        return []


def patch_target_choice(module) -> bool:
    """Wrap ``_select_capture_target(windows, *, app_requested, exact_target)`` so an unqualified capture never lands on
    a panel. True when the wrap is in place (now or already)."""
    original = getattr(module, "_select_capture_target", None)

    if original is None:
        return False

    if getattr(original, MARK, False):
        return True

    if _params(original)[:1] != ["windows"] or "app_requested" not in _params(original):
        log.warning("hermes-bot-fixes: _select_capture_target looks different in this Hermes; panel fix not applied")

        return False

    @functools.wraps(original)
    def choose(windows, *, app_requested, exact_target=False):
        if not app_requested and not exact_target and sys.platform == "linux":
            apps = [w for w in windows if not is_panel(w)]

            if apps:
                windows = apps

        return original(windows, app_requested=app_requested, exact_target=exact_target)

    setattr(choose, MARK, True)
    module._select_capture_target = choose

    return True


def patch_settle(module, sleep=time.sleep) -> bool:
    """Wrap ``_maybe_follow_capture(backend, res, do_capture, ...)`` so the follow-up screenshot waits for the screen to
    settle. True when the wrap is in place (now or already)."""
    original = getattr(module, "_maybe_follow_capture", None)

    if original is None:
        return False

    if getattr(original, MARK, False):
        return True

    if _params(original)[:3] != ["backend", "res", "do_capture"]:
        log.warning("hermes-bot-fixes: _maybe_follow_capture looks different in this Hermes; settle fix not applied")

        return False

    @functools.wraps(original)
    def follow(backend, res, do_capture, *args, **kwargs):
        if do_capture and getattr(res, "ok", False):
            pause = settle_seconds()

            if pause:
                sleep(pause)

        return original(backend, res, do_capture, *args, **kwargs)

    setattr(follow, MARK, True)
    module._maybe_follow_capture = follow

    return True


def apply() -> dict:
    """Apply both fixes to the loaded Hermes modules. Never raises; returns what was applied."""
    applied = {"panels": False, "settle": False}

    try:
        import importlib

        applied["panels"] = patch_target_choice(importlib.import_module("tools.computer_use.cua_backend_capture"))
        applied["settle"] = patch_settle(importlib.import_module("tools.computer_use.tool"))
    except Exception as error:  # computer_use not installed or importable here: nothing to fix
        log.info("hermes-bot-fixes: computer_use fixes not applied (%s)", error)

    return applied
