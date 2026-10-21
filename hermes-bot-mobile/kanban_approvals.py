"""Approvals for kanban workers, answered from the phone app.

A kanban card is worked by a headless worker: on Hermes 0.21 the dispatcher starts `hermes -p <assignee> chat -q "work kanban
task <id>"`; on 0.22 the assignee's gateway runs the card in a managed worker process of its own. Hermes counts a `-q` run as having nobody to ask (`HERMES_SINGLE_QUERY_SESSION`), so every command that needs an OK
is refused on the spot (`approvals.single_query_mode: deny`), and the worker gives up on its card or blocks it.

In a worker this module asks the person instead, through Hermes's own gateway round-trip:

- The approval gate counts the worker as a session with someone to ask (`_presence`), and a session with no notify
  callback falls back to this relay (`_gateway_notify_cbs`). So the agent thread waits the way it does for a chat: with its
  activity heartbeats, which also keep the card's claim; Hermes keeps "for this session" and "always"; a deny is a hard
  stop and silence is not consent.
- The relay writes the waiting approval to a file under the Hermes root. The dashboard's copy of this plugin reads it,
  pushes it to the app with the rest of Needs you (`needs_you.py`), and writes the person's answer next to it.
- The relay reads the answer and resolves Hermes's queue entry. However the wait ends (an answer, the timeout, the worker
  stopping), the files go.

Where the card shows: in the chat the card was created from, when the bot working it created it there; otherwise (another
bot asked for it, or it came from the dashboard or the CLI) in the working bot's main chat.

`approvals.single_query_mode: approve` still approves without asking, and yolo or `approvals.mode: off` never ask.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("hermes-bot-mobile")

#: Set by Hermes's dispatcher in a kanban worker, and only there.
TASK_ENV = "HERMES_KANBAN_TASK"

#: Under the plugin's folder at the Hermes root: one file per waiting approval, `<request id>.json`, and the person's answer
#: as `<request id>.answer.json`.
FOLDER = "kanban-approvals"

CHOICES = ("once", "session", "always", "deny")

#: How long a worker waits for the person. Nobody watches a background card, so Hermes's usual wait (`approvals.timeout`,
#: 300 s by default) is too short: a longer configured one stands. The card's own runtime limit still applies.
WAIT_S = 30 * 60

#: How often a waiting worker looks for the answer.
POLL_S = 0.5

#: A record this old is left over whatever its process says (no worker waits this long).
STALE_S = 2 * 60 * 60

#: Hermes's persistent session ids (`20260101_110000_0dc184`): a chat the app can open. A routine's run is not one.
SESSION_ID = re.compile(r"^\d{8}_\d{6}_[0-9a-f]+$")

#: What a request id must look like to name a file (Hermes uses `uuid4().hex`).
REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def default_folder() -> Path:
    from hermes_constants import get_default_hermes_root

    return Path(get_default_hermes_root()) / "hermes-bot-mobile" / FOLDER


def in_worker(env=os.environ) -> bool:
    return bool(env.get(TASK_ENV))


def _write(path: Path, data: dict) -> None:
    """Writes `path` whole or not at all, readable by this user only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)

        os.chmod(temp, 0o600)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass

        raise


def _read(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

    return data if isinstance(data, dict) else None


def _remove(*paths: Path) -> None:
    for path in paths:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            log.debug("hermes-bot-mobile: could not remove %s", path, exc_info=True)


# -- where the card goes ----------------------------------------------------------------------------------------------


def read_task(task_id: str, board: str = "") -> Optional[dict]:
    """The card's title, who created it, the chat it was created from (`session_id`) and who works it, from Hermes's
    board; None when it can't be read."""
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

            return {
                "title": str(task.title or ""),
                "created_by": str(task.created_by or ""),
                "session_id": str(getattr(task, "session_id", "") or ""),
                "assignee": str(task.assignee or ""),
            }
        finally:
            conn.close()
    except Exception:
        log.debug("hermes-bot-mobile: could not read kanban card %s", task_id, exc_info=True)

        return None


def where_to_ask(task: Optional[dict], profile: str) -> str:
    """The chat a worker's approval shows in: the chat the card was created from when the bot working it created it there,
    else "" (that bot's main chat). Another bot's chat is never the place: that bot can't answer for this one."""
    chat = str((task or {}).get("session_id") or "")

    if task and task.get("created_by") == profile and SESSION_ID.match(chat):
        return chat

    return ""


def own_profile(env=os.environ) -> str:
    """The profile this worker runs as: the dispatcher sets `HERMES_PROFILE` to the card's assignee."""
    named = str(env.get("HERMES_PROFILE") or "").strip()

    if named:
        return named

    try:
        from hermes_constants import get_hermes_home, named_profile_home

        home = named_profile_home(get_hermes_home())

        return home.name if home else "default"
    except Exception:
        return "default"


_CONTEXT: Optional[dict] = None
_CONTEXT_LOCK = threading.Lock()


def worker_context(env=os.environ, read: Callable[[str, str], Optional[dict]] = read_task) -> Optional[dict]:
    """What this worker's approvals are about and where they go, read from the board once per worker (a card's creator and
    origin never change): `task`, `board`, `run`, `title`, `created_by`, `profile` (who works it) and `chat`. None outside a
    worker."""
    global _CONTEXT

    task_id = str(env.get(TASK_ENV) or "")

    if not task_id:
        return None

    with _CONTEXT_LOCK:
        if _CONTEXT is not None and _CONTEXT["task"] == task_id:
            return _CONTEXT

        board = str(env.get("HERMES_KANBAN_BOARD") or "")
        profile = own_profile(env)
        task = read(task_id, board)
        context = {
            "task": task_id,
            "board": board,
            "run": str(env.get("HERMES_KANBAN_RUN_ID") or ""),
            "title": str((task or {}).get("title") or ""),
            "created_by": str((task or {}).get("created_by") or ""),
            "profile": profile,
            "chat": where_to_ask(task, profile),
        }

        # A board that could not be read is tried again at the next approval.
        if task is not None:
            _CONTEXT = context

        return context


# -- in the worker ----------------------------------------------------------------------------------------------------


class RelayCallbacks(dict):
    """Hermes's session -> notify-callback table, with the relay added for a worker's approvals.

    - Hermes 0.21: a worker is a `hermes chat -q` run that no gateway registers a callback for, so the relay is the only way the
      approval reaches anyone.
    - Hermes 0.22: the gateway runs a card in a managed worker that registers its own callback, which mirrors the approval onto
      the card's session in the gateway, where no app looks. The relay is called after it, so the approval also reaches the app;
      an answer from either side resolves the same wait. A relay failure leaves the gateway's copy standing.
    """

    def __init__(self, items=(), fallback: Optional[Callable[[str], Callable[[dict], None]]] = None):
        super().__init__(items)
        self.fallback = fallback

    def get(self, key, default=None):
        found = super().get(key)

        if self.fallback is None:
            return found if found is not None else default

        relay = self.fallback(key)

        if found is None:
            return relay

        def both(data, _gateway=found, _relay=relay):
            _gateway(data)

            try:
                _relay(data)
            except Exception:
                log.warning("hermes-bot-mobile: a kanban approval could not be put in the app; it waits in the gateway", exc_info=True)

        return both


class WorkerRelay:
    """Puts a kanban worker's approvals in front of the person and hands their answer back to Hermes."""

    def __init__(
        self,
        folder: Optional[Path] = None,
        context: Callable[[], Optional[dict]] = worker_context,
        approval=None,
        approval_context=None,
        env=os.environ,
        clock: Callable[[], float] = time.time,
        pid: Optional[int] = None,
        poll_s: float = POLL_S,
        wait_s: float = WAIT_S,
    ):
        self._folder = folder
        self._context = context
        self._approval = approval
        self._approval_context = approval_context
        self._env = env
        self._clock = clock
        self._pid = pid or os.getpid()
        self._poll_s = poll_s
        self._wait_s = wait_s
        self._lock = threading.Lock()
        #: request id -> the session key Hermes queued it under.
        self._waiting: dict[str, str] = {}
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.installed = False

    @property
    def folder(self) -> Path:
        if self._folder is None:
            self._folder = default_folder()

        return self._folder

    def install(self) -> bool:
        """Takes over this worker's approvals. Only in a kanban worker, and only on a Hermes with the parts it relies on;
        False leaves Hermes as it was (the worker's commands are refused, as before)."""
        if not in_worker(self._env):
            return False

        try:
            approval = self._approval or importlib.import_module("tools.approval")
            context = self._approval_context or importlib.import_module("tools.approval_context")
            presence = approval._presence
            callbacks = approval._gateway_notify_cbs
            timeout = context._get_approval_timeout
            single_query_mode = context._get_single_query_approval_mode

            for name in ("resolve_gateway_approval", "register_gateway_settle", "_lock"):
                getattr(approval, name)
        except Exception as error:
            log.info("hermes-bot-mobile: kanban approvals can't be relayed on this Hermes, its workers' commands stay refused (%s)", error)

            return False

        self._approval = approval

        with approval._lock:
            if isinstance(callbacks, RelayCallbacks):
                callbacks.fallback = self.callback_for
            else:
                approval._gateway_notify_cbs = RelayCallbacks(callbacks, self.callback_for)

        if not getattr(presence, "hermes_bot_mobile", False):

            def asks(approval_callback=None, _presence=presence):
                callback, is_cli, is_gateway, is_ask = _presence(approval_callback)

                # Nobody at a terminal and no gateway: a worker. Ask the person, unless this Hermes is set to approve
                # single-query runs without asking.
                if not (is_cli or is_gateway or is_ask) and in_worker(self._env):
                    try:
                        approve = single_query_mode() == "approve"
                    except Exception:
                        approve = False

                    if not approve:
                        is_ask = True

                return callback, is_cli, is_gateway, is_ask

            asks.hermes_bot_mobile = True
            approval._presence = asks

        if not getattr(timeout, "hermes_bot_mobile", False):

            def longer(_timeout=timeout):
                return max(int(_timeout()), int(self._wait_s))

            longer.hermes_bot_mobile = True
            context._get_approval_timeout = longer

        self.installed = True
        log.info("hermes-bot-mobile: this kanban worker's approvals go to the app")

        return True

    def callback_for(self, session_key: str) -> Callable[[dict], None]:
        return lambda data: self.notify(session_key, data)

    def notify(self, session_key: str, data: dict) -> None:
        """Hermes's notify callback, on the agent thread, with the approval already in its queue: write it down for the
        dashboard and watch for the answer. Raising makes Hermes refuse the command, so nothing runs unasked."""
        request_id = str(data.get("request_id") or "")

        if not REQUEST_ID.match(request_id):
            raise ValueError("an approval without a request id the relay can use")

        context = self._context() or {}
        record = {
            "v": 1,
            "id": request_id,
            "pid": self._pid,
            "created_at": self._clock(),
            "profile": str(context.get("profile") or ""),
            "chat": str(context.get("chat") or ""),
            "task": {
                "id": str(context.get("task") or ""),
                "title": str(context.get("title") or ""),
                "board": str(context.get("board") or ""),
                "created_by": str(context.get("created_by") or ""),
            },
            # Hermes passes the command already redacted for showing.
            "command": str(data.get("command") or ""),
            "description": str(data.get("description") or ""),
            "allow_session": data.get("allow_session") is not False,
            "allow_permanent": data.get("allow_permanent") is not False,
        }
        _write(self.folder / f"{request_id}.json", record)

        with self._lock:
            self._waiting[request_id] = session_key

        # However the wait ends, the record goes with it.
        if not self._approval.register_gateway_settle(session_key, request_id, lambda _reason, rid=request_id: self.settle(rid)):
            self.settle(request_id)

            return

        self._start()
        self._wake.set()

    def settle(self, request_id: str) -> None:
        with self._lock:
            self._waiting.pop(request_id, None)

        _remove(self.folder / f"{request_id}.json", self.folder / f"{request_id}.answer.json")

    def check(self) -> int:
        """Hands every answer the person gave back to Hermes; returns how many."""
        with self._lock:
            waiting = dict(self._waiting)

        handed = 0

        for request_id, session_key in waiting.items():
            answer_path = self.folder / f"{request_id}.answer.json"
            answer = _read(answer_path)

            if answer is None:
                continue

            choice = answer.get("choice")

            if choice not in CHOICES:
                # Not an answer: wait for a real one.
                _remove(answer_path)

                continue

            if self._approval.resolve_gateway_approval(session_key, choice, request_id=request_id):
                handed += 1
            else:
                # Hermes no longer waits for it (it timed out just now): nothing to hand back.
                self.settle(request_id)

        return handed

    def _start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return

            self._thread = threading.Thread(target=self._run, name="hermes-bot-mobile-kanban-approvals", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            with self._lock:
                idle = not self._waiting

            if idle:
                self._wake.wait()
                self._wake.clear()

            try:
                self.check()
            except Exception:
                log.debug("hermes-bot-mobile: kanban approval check failed", exc_info=True)

            time.sleep(self._poll_s)


# -- in the dashboard -------------------------------------------------------------------------------------------------


def pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError):
        return False

    return True


class Refusal(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.extra: dict = {}


def allowed(record: dict) -> tuple[str, ...]:
    return tuple(choice for choice in CHOICES if (choice != "session" or record.get("allow_session") is not False) and (choice != "always" or record.get("allow_permanent") is not False))


def public(record: dict) -> dict:
    """What the app gets of a waiting approval: nothing of the worker's process or session."""
    task = record.get("task") if isinstance(record.get("task"), dict) else {}

    return {
        "id": str(record.get("id") or ""),
        "profile": str(record.get("profile") or ""),
        "chat": str(record.get("chat") or ""),
        "task": {key: str(task.get(key) or "") for key in ("id", "title", "board", "created_by")},
        "command": str(record.get("command") or ""),
        "description": str(record.get("description") or ""),
        "allow_session": record.get("allow_session") is not False,
        "allow_permanent": record.get("allow_permanent") is not False,
        "created_at": record.get("created_at") if isinstance(record.get("created_at"), (int, float)) else 0,
    }


class KanbanApprovals:
    """The dashboard's side: the approvals kanban workers wait on, and the person's answers to them."""

    def __init__(self, folder: Optional[Path] = None, alive: Callable[[int], bool] = pid_alive, clock: Callable[[], float] = time.time):
        self._folder = folder
        self._alive = alive
        self._clock = clock

    @property
    def folder(self) -> Path:
        if self._folder is None:
            self._folder = default_folder()

        return self._folder

    def signature(self) -> tuple:
        """Changes whenever an approval opens, is answered or closes: a cheap look at the folder."""
        try:
            return tuple(sorted((entry.name, entry.stat().st_mtime_ns) for entry in os.scandir(self.folder) if entry.name.endswith(".json")))
        except OSError:
            return ()

    def _records(self) -> list[tuple[dict, bool]]:
        """Every live record, with whether it was answered; a record whose worker is gone is cleared away."""
        try:
            names = [entry.name for entry in os.scandir(self.folder) if entry.name.endswith(".json") and not entry.name.endswith(".answer.json")]
        except OSError:
            return []

        found = []

        for name in names:
            request_id = name[: -len(".json")]
            path = self.folder / name
            record = _read(path)
            answer = self.folder / f"{request_id}.answer.json"

            if record is None or not REQUEST_ID.match(request_id) or record.get("id") != request_id:
                continue

            created = record.get("created_at") if isinstance(record.get("created_at"), (int, float)) else 0

            if not self._alive(record.get("pid")) or self._clock() - created > STALE_S:
                _remove(path, answer)

                continue

            found.append((record, answer.exists()))

        return sorted(found, key=lambda item: item[0].get("created_at") or 0)

    def pending(self) -> list[dict]:
        """What waits on the person, oldest first. An answered one is off the list while its worker takes the answer."""
        return [public(record) for record, answered in self._records() if not answered]

    def answer(self, request_id: str, choice: str) -> dict:
        if not REQUEST_ID.match(str(request_id or "")):
            raise Refusal(404, "That request is no longer waiting.")

        found = next(((record, answered) for record, answered in self._records() if record.get("id") == request_id), None)

        if found is None:
            # Timed out, or its worker stopped.
            raise Refusal(404, "That request is no longer waiting.")

        record, answered = found

        if answered:
            raise Refusal(409, "That request was already answered.")

        if choice not in allowed(record):
            raise Refusal(400, f"This request can't be answered with {choice!r}.")

        _write(self.folder / f"{request_id}.answer.json", {"choice": choice, "at": self._clock()})

        return {"answered": True, "id": request_id, "choice": choice}
