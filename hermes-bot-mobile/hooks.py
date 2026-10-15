"""The Hermes lifecycle hooks that decide when the phone should buzz.

Every callback returns at once and never raises: `pre_tool_call` fails *closed* on a timeout (a slow plugin would
block the bot's tool), and approval hooks run on the thread that is waiting for you. Sending is queued; the
network happens on the pusher's own threads.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

try:
    from . import notify
    from .store import DeviceStore
except ImportError:  # the tests import the modules flat
    import notify  # type: ignore[no-redef]
    from store import DeviceStore  # type: ignore[no-redef]

log = logging.getLogger("hermes-bot-mobile")

#: Hermes's persistent session ids (`20260101_110000_0dc184`): what the app keeps a chat by and opens it with.
SESSION_ID = re.compile(r"^\d{8}_\d{6}_[0-9a-f]+$")

#: A group room's id, as Hermes requires it.
ROOM_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

#: What the app titles a bot's main chat: a push about it needs no chat name, the bot's name says it.
MAIN_CHAT_TITLE = "bot chat"

#: A routine run's session id (`cron_<job id>_<YYYYmmdd>_<HHMMSS>`), which names the job.
CRON_SESSION = re.compile(r"^cron_(.+)_\d{8}_\d{6}$")


def turn_context() -> dict:
    """What Hermes knows about the turn the calling thread works for: `chat`, the persistent id of the chat it belongs to
    (the top-level one, also from a subagent's thread); `source`, its surface (`tui` for the app, `bot_room` for a room
    member); `room`, the group room a member works in. Empty where it cannot tell; never raises."""
    found = {"chat": "", "source": "", "room": ""}

    try:
        from gateway.session_context import get_session_env

        found["chat"] = str(get_session_env("HERMES_SESSION_ID", "") or "")
        found["source"] = str(get_session_env("HERMES_SESSION_SOURCE", "") or "").strip().lower()
    except Exception:
        pass

    try:
        # Private to Hermes, so read defensively: the live session a room member's turn runs in carries its room's task.
        from tui_gateway.server import _current_runtime_session_record

        task = (_current_runtime_session_record.get() or {}).get("_hosted_room_task") or {}
        room = str(task.get("room_id") or "")
        found["room"] = room if ROOM_ID.match(room) else ""
    except Exception:
        pass

    return found


def chat_id(*candidates) -> str:
    """The first of these that is a persistent session id, else ""."""
    for candidate in candidates:
        value = str(candidate or "")

        if SESSION_ID.match(value):
            return value

    return ""


def chat_title(chat: str) -> str:
    """A side chat's title ("Plan the week"), so a push says which chat it is about; "" for the bot's main
    chat or when it can't be read. Read-only, and Hermes's state database is in WAL mode, so a reader never waits."""
    if not chat:
        return ""

    try:
        from hermes_state import SessionDB

        db = SessionDB(read_only=True)

        try:
            title = str(db.get_session_title(chat) or "").strip()
        finally:
            db.close()
    except Exception:
        return ""

    return "" if title.lower() == MAIN_CHAT_TITLE else title


def routine_name(session_id: str) -> str:
    """The name of the routine a cron run's session belongs to, or ""."""
    match = CRON_SESSION.match(str(session_id or ""))

    if not match:
        return ""

    try:
        from cron.jobs import get_job

        return str((get_job(match.group(1)) or {}).get("name") or "").strip()
    except Exception:
        return ""


def current_profile() -> str:
    """The name of the profile the calling thread is working for (`default` for the root home)."""
    from hermes_constants import get_hermes_home, named_profile_home

    home = get_hermes_home()
    named = named_profile_home(home)

    return named.name if named else "default"


def bot_name(profile: str) -> str:
    """The bot's own name as the app shows it: its `ui_meta` title, else "Hermes" / the title-cased profile name."""
    try:
        import yaml
        from hermes_constants import get_hermes_home

        data = yaml.safe_load(Path(get_hermes_home(), "profile.yaml").read_text(encoding="utf-8")) or {}
        title = ((data.get("ui_meta") or {}).get("hermes-bots") or {}).get("title")

        if isinstance(title, str) and title.strip():
            return title.strip()
    except Exception:
        pass

    return "Hermes" if profile == "default" else profile.replace("-", " ").replace("_", " ").title()


def pending_request_id(session_key: str, command: str) -> str:
    """The id Hermes gave this approval, so the same one is never pushed twice (None-safe: returns "" on any doubt)."""
    try:
        from tools.approval import list_gateway_approvals

        queue = list_gateway_approvals(session_key)

        for entry in reversed(queue):
            if entry.get("command") == command:
                return str(entry.get("request_id") or "")

        return str(queue[-1].get("request_id") or "") if queue else ""
    except Exception:
        return ""


def question_text(args) -> str:
    """The first question a `clarify` call asks (it may carry one `question` or a list under `questions`)."""
    if not isinstance(args, dict):
        return ""

    if isinstance(args.get("question"), str):
        return args["question"]

    questions = args.get("questions")

    if isinstance(questions, list) and questions:
        first = questions[0]

        if isinstance(first, dict):
            return str(first.get("question") or first.get("prompt") or "")

        return str(first)

    return ""


class PushHooks:
    def __init__(self, store: DeviceStore | None = None, pusher: notify.Pusher | None = None, profile=current_profile, name=bot_name, context=turn_context, title=chat_title, routine=routine_name):
        self.store = store or DeviceStore()
        self.pusher = pusher or notify.Pusher(self.store)
        self.dedupe = notify.Dedupe()
        self._profile = profile
        self._name = name
        self._context = context
        self._title = title
        self._routine = routine

    # -- helpers -----------------------------------------------------------------------------------------------

    def _target(self, *ids) -> dict:
        """Where a push about the calling turn should land: its group room for a room member (Needs you when the room
        cannot be told: a member's own session is not a chat the app opens), else the chat it belongs to."""
        context = self._context()

        if context["room"]:
            return {"room": context["room"]}

        if context["source"] == "bot_room":
            return {"inbox": True}

        return {"chat": chat_id(context["chat"], *ids)}

    def _emit(self, kind: str, key: str, *, detail: str = "", failed: bool = False, session: str = "", target: dict | None = None) -> None:
        """Push one thing that needs the person: an approval, a question, or a routine that broke. Finished replies and
        routines that ran are never pushed: they show on Home and in the bot's routines, and a buzz for them is noise."""
        profile = self._profile()

        if self.store.level(profile) == "off":
            return

        # The phone is in front with this chat on screen: the card is already in front of the person.
        seen = self.store.presence()

        if seen["foreground"] and session and session in seen["sessions"]:
            return

        if not self.store.devices() or not self.dedupe.first(f"{kind}:{profile}:{key}"):
            return

        body = notify.lines(kind, detail=detail, failed=failed)
        target = target or {}
        # Which chat it is about, by its title, so the notification makes sense without opening it.
        about = self._title(target.get("chat", "")) if target.get("chat") else ""
        title = f"{self._name(profile)} · {about}" if about else self._name(profile)
        url = notify.INBOX if target.get("inbox") else notify.link(profile, chat=target.get("chat", ""), room=target.get("room", ""), page=target.get("page", ""))
        self.pusher.send(kind, notify.build(kind, profile=profile, bot=title, body=body, key=key, url=url))

    # -- hooks -------------------------------------------------------------------------------------------------

    def on_approval(self, **kw) -> None:
        # Only an approval waiting on a person who is away from the terminal: the gateway path (the app, Desktop). A
        # `coalesced` one repeats an approval already waiting, which puts nothing new in front of the person.
        if kw.get("surface") != "gateway" or kw.get("coalesced"):
            return

        session_key = str(kw.get("session_key") or "")
        command = str(kw.get("command") or "")
        key = pending_request_id(session_key, command) or f"{session_key}:{kw.get('pattern_key')}"
        # In the dashboard the session key is the chat's persistent id; in the messaging gateway it is not, and then
        # the chat comes from the turn's context.
        target = self._target(session_key, kw.get("session_id"))
        self._emit("approval", key, detail=notify.describe(kw.get("description", ""), command), session=target.get("chat") or session_key, target=target)

    def on_tool(self, **kw) -> None:
        if kw.get("tool_name") != "clarify":
            return

        key = str(kw.get("tool_call_id") or kw.get("session_id") or "")
        target = self._target(kw.get("session_id"))
        self._emit("question", key, detail=notify.tidy(question_text(kw.get("args"))), session=target.get("chat") or str(kw.get("session_id") or ""), target=target)

    def on_session_end(self, **kw) -> None:
        # Only a routine that broke: one that ran fine is no news for the phone.
        if kw.get("platform") != "cron" or kw.get("interrupted") or not kw.get("failed"):
            return

        session = str(kw.get("session_id") or "")
        self._emit("routine", session, detail=self._routine(session), failed=True, target={"page": "routines"})

    def callbacks(self) -> dict:
        """Hook name -> callback, each wrapped so that nothing it does can reach the bot."""
        def guard(callback):
            def run(**kw):
                try:
                    callback(**kw)
                except Exception:
                    log.exception("hermes-bot-mobile hook failed")

            return run

        return {
            "pre_approval_request": guard(self.on_approval),
            "pre_tool_call": guard(self.on_tool),
            "on_session_end": guard(self.on_session_end),
        }
