"""Telling people things: a push to the person's phone (through hermes-bot-mobile), and one fixed line to the bot.

**The person:** hermes-bot-mobile 0.1.7+ keeps the registered phones; its `push_api.py` is loaded from the installed plugin
folder as a package of its own. Without it there are no pushes: the cards still show when the app is opened.

**The bot:** Hermes's own Bot Chat delivery lane, the one cron's `deliver: bot-chat` and `message_agent` use, so it needs no
credential or config. If the app holds the bot's chat, the line goes to that live session's mailbox; if nobody holds it,
a headless turn runs it in the chat under the bot's turn lock; if another writer holds it, we try again later. The line
is a fixed template with no words from the other party; the bot reads those with `phone_call_report`.
"""

from __future__ import annotations

import hashlib
import importlib.util
import logging
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger("hermes-phone")

AUTHOR = {"id": "service:hermes-phone", "name": "Phone", "is_bot": True}
TURN_TIMEOUT_S = 600


def _push_api(root: Path):
    """hermes-bot-mobile's push_api, loaded under a private package name so its relative imports work."""
    name = "_hermes_phone_mobile"
    folder = Path(root) / "plugins" / "hermes-bot-mobile"

    if not (folder / "push_api.py").is_file():
        return None
    if name not in sys.modules:
        package = types.ModuleType(name)
        package.__path__ = [str(folder)]  # type: ignore[attr-defined]
        sys.modules[name] = package
    module_name = f"{name}.push_api"
    if module_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(module_name, folder / "push_api.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    return sys.modules[module_name]


def push(root: Path, kind: str, *, profile: str, bot: str, body: str, key: str, url: Optional[str] = None) -> Optional[Dict[str, Any]]:
    try:
        api = _push_api(root)
        if api is None:
            return None
        return api.send(kind, profile=profile, bot=bot, body=body, key=key, url=url)
    except Exception as error:
        log.warning("hermes-phone: push failed (%s)", type(error).__name__)
        return None


def errand_line(errand: Dict[str, Any]) -> str:
    counts: Dict[str, int] = {}
    for call in errand["calls"]:
        status = ((call.get("outcome") or {}).get("status")) or call.get("status") or "done"
        counts[status] = counts.get(status, 0) + 1
    summary = ", ".join(f"{n} {status.replace('_', ' ')}" for status, n in counts.items())
    title = errand["brief"].get("title", "").replace('"', "'")
    return f'[Phone errand finished · "{title}" · {errand["id"]} · {summary}] Read the details with phone_call_report.'


def delivery_id(errand_id: str) -> str:
    return hashlib.sha256(f"hermes-phone:{errand_id}".encode()).hexdigest()


def tell_bot(root: Path, profile: str, line: str, key: str) -> str:
    """'delivered', 'busy' (another writer holds the chat: try later) or 'failed'."""
    home = Path(root) if profile == "default" else Path(root) / "profiles" / profile

    try:
        from tools import bot_live_delivery as live
    except Exception:
        return "failed"

    if not hasattr(live, "find_canonical_owner"):
        return _tell_authority(live, home, line, key)

    try:
        from tools.bot_live_delivery import deliver_to_live_owner, find_canonical_live_owner, find_canonical_owner
    except Exception:
        return "failed"

    try:
        owner = find_canonical_live_owner(home)
        if owner is not None:
            deliver_to_live_owner(home, owner, line, delivery_id=key, author=AUTHOR)
            return "delivered"
        if find_canonical_owner(home):
            return "busy"
    except Exception as error:
        log.warning("hermes-phone: Bot Chat lookup failed (%s)", type(error).__name__)
        return "failed"

    return _headless_turn(Path(root), profile, home, line)


def _tell_authority(live: types.ModuleType, home: Path, line: str, key: str) -> str:
    """Hermes 0.22+: the profile's gateway owns its chats. It admits the delivery into Bot Chat's queue at once, durably,
    behind any turn already there, and makes Bot Chat if the bot has none yet. The delivery id makes a retry safe: the same
    id and message return the receipt already given. A gateway that is not up yet means try later."""
    try:
        owner = live.find_canonical_live_owner(home)
    except ValueError:
        return "busy"
    except Exception as error:
        log.warning("hermes-phone: Bot Chat lookup failed (%s)", type(error).__name__)
        return "failed"

    if owner is None:
        log.warning("hermes-phone: the bot has no Hermes store to deliver to")
        return "failed"

    try:
        receipt = live.deliver_to_live_owner(home, owner, line, delivery_id=key, author=AUTHOR)
    except ValueError:
        # Includes PreDialError: nothing reached the gateway, so the same delivery is safe to send again.
        return "busy"
    except Exception as error:
        log.warning("hermes-phone: delivery to Bot Chat failed (%s)", type(error).__name__)
        return "failed"

    if isinstance(receipt, dict) and receipt.get("admission_id"):
        return "delivered"

    return "busy"


def _child_env(home: Path) -> Dict[str, str]:
    """The environment cron gives a Bot Chat turn for another profile; the plain one on Hermes versions without it."""
    env = dict(os.environ)
    try:
        from agent.delegation_context import delegated_child_subprocess_env
        from tools.environments.local import served_profile_child_env

        env = served_profile_child_env(delegated_child_subprocess_env(os.environ), target_home=home, inherit_credentials=True)
        try:
            from cron.scheduler_worker_env import pin_hermes_tree_on_pythonpath
            import hermes_cli

            pin_hermes_tree_on_pythonpath(env, Path(hermes_cli.__file__).resolve().parents[1])
        except Exception:
            pass
    except Exception:
        env.pop("HERMES_HOME", None)
    return env


def _headless_turn(root: Path, profile: str, home: Path, line: str) -> str:
    try:
        from tools.bot_relay import acquire_turn_lock
    except Exception:
        return "failed"

    argv = [sys.executable, "-m", "hermes_cli.main", "-p", profile if home.parent.name == "profiles" else "default",
            "chat", "--in", "~", "-c", "Bot Chat", "--create-if-missing", "-Q"]
    query = None

    try:
        with acquire_turn_lock(root, profile):
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", prefix="hermes-phone-", delete=False) as out:
                out.write(line)
                query = out.name
            result = subprocess.run([*argv, "--query-file", query], env=_child_env(home), capture_output=True, text=True, timeout=TURN_TIMEOUT_S)
        if result.returncode == 0:
            return "delivered"
        if "SESSION_NOT_OWNED" in (result.stderr or ""):
            return "busy"
        log.warning("hermes-phone: the Bot Chat turn failed (exit %s)", result.returncode)
        return "failed"
    except Exception as error:
        name = type(error).__name__
        log.warning("hermes-phone: the Bot Chat turn failed (%s)", name)
        return "busy" if name == "TurnBusyError" else "failed"
    finally:
        if query:
            Path(query).unlink(missing_ok=True)
