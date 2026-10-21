"""The desk runner: carries out what the supervisor asked for, from the messaging gateway.

The supervisor (``supervisor.py``) runs inside a card's worker, which exits right after the card blocks, so it only writes a
request file under ``<root>/hermes-kanban/requests/``. One runner per server (a lock file decides which process) reads them
every few seconds, from a thread in the gateway: the process that runs the kanban dispatcher, so it is up whenever cards run.

- **wake**: one turn for the bot named in the request, in its "Kanban desk" chat, with the desk message. On Hermes 0.22 that is
  the gateway's bot-to-bot door (``a2a.forward``: one session per sender and context, created on first use and kept, the turn
  queued behind anything already running there); the request carries a fixed input id, so a wake retried after a restart
  converges on the same turn. On Hermes 0.21 it is a one-shot ``hermes -p <bot> chat -c "Kanban desk" -Q`` under the bot's
  turn lock (``tools.bot_relay.acquire_turn_lock``, the lock deliveries and relays take) with that bot's own environment
  (``delivery_env``). A one-shot turn follows ``approvals.single_query_mode`` for dangerous commands and cannot wait for a
  person; the desk's work (kanban tools, comments) needs neither.
- **retry**: unblocks a card that blocked as transient, once, when its time comes.

Before either, the card is read again: if it is no longer blocked (someone answered it meanwhile) the request is dropped. What
each turn replied goes to ``desk.log`` (one JSON line per request) and to the ``kanban_card_woke`` hook.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

try:
    from . import board as B
except ImportError:  # the tests import the modules flat
    import board as B  # type: ignore[no-redef]

try:
    import fcntl
except ImportError:  # Windows: no gateway-side runner there
    fcntl = None  # type: ignore[assignment]

log = logging.getLogger("hermes-kanban")

DESK_TITLE = "Kanban desk"
#: Who the desk's messages are from, and the conversation they continue, on Hermes 0.22's bot-to-bot door.
PEER = "Kanban desk"
CONTEXT = "kanban-desk"

EVERY_S = 5.0
TURN_TIMEOUT_S = 30 * 60
#: How long a 0.21 desk turn waits for the bot's turn lock before it is tried again on a later look.
LOCK_WAIT_S = 30.0
#: A request older than this is dropped, as is one tried this many times (a turn the gateway's restart cut off is tried again).
EXPIRE_S = 24 * 3600
MAX_TRIES = 2
MAX_RUNNING = 2


def enabled() -> bool:
    return os.environ.get("HERMES_KANBAN_DESK", "on").strip().lower() not in ("off", "0", "false", "no")


def through_gateway() -> bool:
    """Whether this Hermes has the gateway's bot-to-bot door (0.22+)."""
    try:
        return importlib.util.find_spec("gateway.session_a2a") is not None
    except Exception:
        return False


def reply_of(receipt: Any) -> str:
    result = receipt.get("result") if isinstance(receipt, dict) else None
    reply = str((result or {}).get("final_response") or "").strip() if isinstance(result, dict) else ""

    return reply or "(The bot finished without a reply.)"


class Busy(Exception):
    """The bot is in another delivery turn; the request waits for a later look."""


def turn_through_gateway(root: Path, target: str, message: str, input_id: str, timeout: float = TURN_TIMEOUT_S) -> str:
    import asyncio

    from gateway.session_a2a import forward_to_owner

    receipt = asyncio.run(forward_to_owner(
        str(B.profile_home(target, root)), agent=target, tenant="", peer=PEER, context_id=CONTEXT,
        input_id=input_id, text=message, timeout=timeout,
    ))

    return reply_of(receipt)


def turn_one_shot(root: Path, target: str, message: str, input_id: str, timeout: float = TURN_TIMEOUT_S) -> str:
    from tools import bot_relay

    # The launcher Hermes's own deliveries use. A throwaway test home has no dependency generation for it to pick, so a test
    # can name the venv's hermes instead.
    cli = getattr(bot_relay, "_hermes_cli", None)
    argv0 = os.environ.get("HERMES_KANBAN_HERMES_CLI") or (cli() if callable(cli) else shutil.which("hermes") or "hermes")
    env = bot_relay.delivery_env(None, B.profile_home(target, root))
    folder = B.data_dir(root) / "tmp"
    folder.mkdir(parents=True, exist_ok=True)
    query = folder / f"desk-{input_id}.md"
    query.write_text(message, encoding="utf-8")
    argv = [argv0, "-p", target, "chat", "--in", "~", "-c", DESK_TITLE, "--create-if-missing", "-Q", "--query-file", str(query)]

    try:
        with bot_relay.acquire_turn_lock(root, target, timeout_seconds=LOCK_WAIT_S):
            done = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env, timeout=timeout, check=False)
    except bot_relay.TurnBusyError as error:
        raise Busy(str(error)) from error
    finally:
        query.unlink(missing_ok=True)

    if done.returncode != 0:
        tail = (done.stderr or done.stdout or "").strip().splitlines()[-1:] or [""]
        raise RuntimeError(f"the desk turn exited with {done.returncode}: {tail[0][:300]}")

    return done.stdout.strip() or "(The bot finished without a reply.)"


class Desk:
    def __init__(self, root: Optional[Path] = None, clock: Callable[[], float] = time.time, emit: Optional[Callable[..., Any]] = None,
                 open_board=None, board_api=B, turn: Optional[Callable[..., str]] = None):
        self._root = root
        self._clock = clock
        self._emit = emit or (lambda *args, **kw: None)
        self._open_board = open_board or B.board
        self._api = board_api
        self._turn = turn
        self._running: set[str] = set()
        self._guard = threading.Lock()
        self._lock_handle = None

    @property
    def root(self) -> Path:
        return Path(self._root or B.hermes_root())

    @property
    def folder(self) -> Path:
        return B.data_dir(self.root) / "requests"

    def turn(self, target: str, message: str, input_id: str) -> str:
        if self._turn is not None:
            return self._turn(self.root, target, message, input_id)

        if through_gateway():
            return turn_through_gateway(self.root, target, message, input_id)

        return turn_one_shot(self.root, target, message, input_id)

    # ── the requests ───────────────────────────────────────────────────────────────────────────────────────────────

    def due(self) -> list[tuple[Path, dict]]:
        """The requests whose time has come, oldest first. Unreadable ones are dropped."""
        found = []

        for path in sorted(self.folder.glob("*.json")) if self.folder.is_dir() else []:
            try:
                request = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                path.unlink(missing_ok=True)
                continue

            if not isinstance(request, dict) or request.get("action") not in ("wake", "retry") or not request.get("task_id"):
                path.unlink(missing_ok=True)
                continue

            if float(request.get("not_before") or 0) <= self._clock():
                found.append((path, request))

        return found

    def handle(self, path: Path, request: dict) -> str:
        """Carries out one request; returns what happened. The file is removed unless it should be looked at again."""
        task_id, board = str(request["task_id"]), request.get("board") or None
        now = self._clock()

        if now - float(request.get("at") or now) > EXPIRE_S:
            return self._done(path, request, "expired")

        if int(request.get("tries") or 0) >= MAX_TRIES:
            return self._done(path, request, "gave up")

        with self._open_board(board) as conn:
            card = self._api.card(conn, task_id)

            if card is None or card.get("status") != "blocked":
                return self._done(path, request, f"moved on ({card.get('status') if card else 'gone'})")

            if request["action"] == "retry":
                if card.get("block_kind") != "transient":
                    return self._done(path, request, "blocked for another reason now")

                self._api.comment(conn, task_id, "Retrying once: it blocked on a flaky failure. If it blocks the same way again, "
                                                 "Hermes moves it to triage for you.")
                retried = self._api.unblock(conn, task_id)
                self._emit("kanban_card_retried", task_id=task_id, board=board, retried=retried)

                return self._done(path, request, "retried" if retried else "could not unblock")

        target, message = str(request.get("target") or ""), str(request.get("message") or "")

        if not target or not message:
            return self._done(path, request, "nothing to send")

        request = {**request, "tries": int(request.get("tries") or 0) + 1, "input_id": request.get("input_id") or uuid.uuid4().hex}
        self._rewrite(path, request)

        try:
            reply, ok = self.turn(target, message, request["input_id"]), True
        except Busy:
            self._rewrite(path, {**request, "tries": request["tries"] - 1, "not_before": now + 60})

            return "busy"
        except Exception as error:
            reply, ok = f"{type(error).__name__}: {error}", False
            log.warning("hermes-kanban: waking %s for card %s failed: %s", target, task_id, reply)

            if request["tries"] < MAX_TRIES:
                self._rewrite(path, {**request, "not_before": now + 120})

                return "failed, will retry"

        self._emit("kanban_card_woke", task_id=task_id, board=board, target=target, ok=ok, reply=reply)

        return self._done(path, request, "woke" if ok else "failed", reply=reply)

    def _rewrite(self, path: Path, request: dict) -> None:
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(json.dumps(request), encoding="utf-8")
        tmp.replace(path)

    def _done(self, path: Path, request: dict, outcome: str, reply: str = "") -> str:
        path.unlink(missing_ok=True)
        entry = {"at": self._clock(), "action": request.get("action"), "task_id": request.get("task_id"),
                 "target": request.get("target"), "outcome": outcome, "reply": reply[:1000]}

        try:
            with open(B.data_dir(self.root) / "desk.log", "a", encoding="utf-8") as out:
                out.write(json.dumps(entry) + "\n")
        except OSError:
            pass

        log.info("hermes-kanban: %s for card %s: %s", request.get("action"), request.get("task_id"), outcome)

        return outcome

    # ── the loop ───────────────────────────────────────────────────────────────────────────────────────────────────

    def tick(self) -> list[threading.Thread]:
        """Starts a thread for each due request not already running, up to ``MAX_RUNNING`` at once."""
        started = []

        for path, request in self.due():
            with self._guard:
                if path.name in self._running or len(self._running) >= MAX_RUNNING:
                    continue

                self._running.add(path.name)

            thread = threading.Thread(target=self._run, args=(path, request), name=f"hermes-kanban-{path.stem}", daemon=True)
            thread.start()
            started.append(thread)

        return started

    def _run(self, path: Path, request: dict) -> None:
        try:
            self.handle(path, request)
        except Exception:
            log.exception("hermes-kanban: a desk request failed")
        finally:
            with self._guard:
                self._running.discard(path.name)

    def take_lock(self) -> bool:
        """Becomes this server's one runner, for as long as this process lives. False when another process is."""
        if fcntl is None:
            return False

        folder = B.data_dir(self.root)
        folder.mkdir(parents=True, exist_ok=True)
        handle = open(folder / "desk.lock", "a+")

        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()

            return False

        self._lock_handle = handle

        return True

    def serve(self, stop: threading.Event, every: float = EVERY_S) -> None:
        while not stop.is_set():
            try:
                if self._lock_handle is not None or self.take_lock():
                    self.tick()
            except Exception:
                log.exception("hermes-kanban: the desk runner hit an error")

            stop.wait(every if self._lock_handle is not None else 60.0)

    def start(self) -> threading.Event:
        stop = threading.Event()
        threading.Thread(target=self.serve, args=(stop,), name="hermes-kanban-desk", daemon=True).start()

        return stop
