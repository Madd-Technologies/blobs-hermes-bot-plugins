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

#: Where a turn runs when it is one of the person's own chats: the app, Hermes Desktop and the dashboard all talk to the
#: same gateway. Everything else is not a chat the person had (another bot's message runs as `hermes -p <bot> chat`,
#: "cli"; a room member, "bot_room"; a subagent; a routine's run, "cron") or has its own notifications (Telegram...).
CHAT_PLATFORMS = frozenset({"tui"})

#: Turns in a chat that the person did not start, by what woke the bot (the wording Hermes stores them with): a routine's
#: output handed to the bot to sum up, a background process's heartbeat, the answer to a message this bot sent another bot.
NOT_FOR_THE_PERSON = (
    re.compile(r'^\[Cronjob\s+"'),
    re.compile(r"^\[Background process \S+ heartbeat #"),
    re.compile(r"bot_mode_dm\.py\s+--run-delivery|tools/bot_relay\.py"),
    re.compile(r"^Message from (?:agent '|🤖)"),
)

#: Replies that say nothing: a room member passing, a routine told to stay quiet.
SILENT_REPLIES = frozenset({"(pass)", "[silent]", "silent"})


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


def yaml_module():
    """Hermes's YAML reader. Hermes 0.21.6 reads YAML through its own `hermes_yaml` (ruamel) and no longer ships PyYAML,
    so a bare `import yaml` fails there; older Hermes has only PyYAML."""
    try:
        import hermes_yaml

        return hermes_yaml
    except ImportError:
        import yaml

        return yaml


def bot_home(profile: str) -> Path:
    """The named bot's own folder: the Hermes root for `default`, `profiles/<name>` under it for the others. Not the
    calling process's home: a blocked kanban card is pushed from whichever process ran the hook, for its assignee."""
    from hermes_constants import get_default_hermes_root

    root = Path(get_default_hermes_root())

    return root if profile == "default" else root / "profiles" / profile


def bot_name(profile: str) -> str:
    """The bot's own name as the app shows it: its `ui_meta` title, else "Hermes" / the title-cased profile name."""
    try:
        data = yaml_module().safe_load((bot_home(profile) / "profile.yaml").read_text(encoding="utf-8-sig")) or {}
        title = ((data.get("ui_meta") or {}).get("hermes-bots") or {}).get("title")

        if isinstance(title, str) and title.strip():
            return title.strip()
    except Exception:
        pass

    return "Hermes" if profile == "default" else profile.replace("-", " ").replace("_", " ").title()


def kanban_card(board, task_id: str) -> dict | None:
    """A kanban card as it is now (`title`, `status`, `loop`, `reason`), read from Hermes's board, or None."""
    try:
        from hermes_cli import kanban_db as kb

        try:
            from hermes_cli.kanban_db_connect import connect
        except ImportError:  # before 0.21.5 it lived in kanban_db
            connect = kb.connect

        conn = connect(board=board or None)

        try:
            task = kb.get_task(conn, str(task_id))

            if task is None:
                return None

            events = kb.list_events(conn, task.id)
            event = next((event for event in reversed(events) if event.kind in ("blocked", "block_loop_detected")), None)

            return {"title": task.title, "status": task.status, "loop": bool(event and event.kind == "block_loop_detected"), "reason": ((event.payload or {}).get("reason") if event else None) or task.last_failure_error or ""}
        finally:
            conn.close()
    except Exception:
        return None


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
    def __init__(self, store: DeviceStore | None = None, pusher: notify.Pusher | None = None, profile=current_profile, name=bot_name, context=turn_context, title=chat_title, routine=routine_name, card=kanban_card):
        self.store = store or DeviceStore()
        self.pusher = pusher or notify.Pusher(self.store)
        self.dedupe = notify.Dedupe()
        self._profile = profile
        self._name = name
        self._context = context
        self._title = title
        self._routine = routine
        self._card = card

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

    def _emit(self, kind: str, key: str, *, detail: str = "", failed: bool = False, session: str = "", target: dict | None = None, slot: str = "", profile: str = "") -> None:
        """Push one thing for the person: an approval, a question, a routine that broke, a blocked kanban card, or (only for
        a bot set to `all`) the start of a reply. That a turn finished or a routine ran is never pushed: it shows on Home and in the bot's
        routines, and a buzz for it is noise. `slot` is what a newer push of the same kind replaces (a chat's last reply)."""
        profile = profile or self._profile()
        level = self.store.level(profile)

        if level == "off" or (kind == "reply" and level != "all"):
            return

        # The phone is in front with this chat on screen: the card is already in front of the person. A reply is in front
        # of them as soon as the app is: Home shows it.
        seen = self.store.presence()

        if seen["foreground"] and (kind == "reply" or (session and session in seen["sessions"])):
            return

        if not self.store.devices() or not self.dedupe.first(f"{kind}:{profile}:{key}"):
            return

        body = notify.lines(kind, detail=detail, failed=failed)
        target = target or {}
        # Which chat it is about, by its title, so the notification makes sense without opening it.
        about = self._title(target.get("chat", "")) if target.get("chat") else ""
        title = f"{self._name(profile)} · {about}" if about else self._name(profile)
        url = notify.INBOX if target.get("inbox") else notify.link(profile, chat=target.get("chat", ""), room=target.get("room", ""), page=target.get("page", ""))
        self.pusher.send(kind, notify.build(kind, profile=profile, bot=title, body=body, key=slot or key, url=url))

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

    def on_reply(self, **kw) -> None:
        """A bot gave its final answer in one of the person's chats (`post_llm_call`): its first words, like a message."""
        if str(kw.get("platform") or "").lower() not in CHAT_PLATFORMS:
            return

        asked = str(kw.get("user_message") or "")

        if any(pattern.search(asked.strip()) for pattern in NOT_FOR_THE_PERSON):
            return

        answer = str(kw.get("assistant_response") or "").strip()

        if not answer or answer.lower() in SILENT_REPLIES:
            return

        session = str(kw.get("session_id") or "")
        target = self._target(session)

        # A room member's turn, known by its context: the room shows what its members say.
        if target.get("room") or target.get("inbox") or not target.get("chat"):
            return

        chat = target["chat"]
        turn = str(kw.get("turn_id") or kw.get("task_id") or notify.notification_id(answer))
        self._emit("reply", f"{chat}:{turn}", detail=notify.preview(answer), session=chat, target=target, slot=chat)

    def on_session_end(self, **kw) -> None:
        # Only a routine that broke: one that ran fine is no news for the phone.
        if kw.get("platform") != "cron" or kw.get("interrupted") or not kw.get("failed"):
            return

        session = str(kw.get("session_id") or "")
        self._emit("routine", session, detail=self._routine(session), failed=True, target={"page": "routines"})

    def on_kanban_blocked(self, **kw) -> None:
        # A background worker on the kanban board stopped because it needs its person (it runs headless and can't ask).
        # The board says whether the card really waits on them: one waiting on another card moves to todo instead, and one
        # blocked again for the same reason goes to triage for a person.
        task = str(kw.get("task_id") or "")
        card = self._card(kw.get("board"), task) if task else None

        if not card or not (card["status"] == "blocked" or (card["status"] == "triage" and card.get("loop"))):
            return

        what = notify.tidy(notify.redact(card.get("title") or ""), 40)
        why = notify.redact(card.get("reason") or kw.get("reason") or "")
        detail = f"{what} — {why}" if what and why.strip() else what or why
        profile = str(kw.get("assignee") or kw.get("profile_name") or "")
        # The Needs you list, where the app shows the card (its dashboard copies it from the board when it is read).
        self._emit("blocked", f"{kw.get('board') or 'default'}/{task}:{kw.get('run_id')}", detail=notify.tidy(detail), target={"inbox": True}, profile=profile)

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
            "post_llm_call": guard(self.on_reply),
            "on_session_end": guard(self.on_session_end),
            "kanban_task_blocked": guard(self.on_kanban_blocked),
        }
