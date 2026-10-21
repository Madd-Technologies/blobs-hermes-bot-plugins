"""When a card blocks, the bot that created it sorts it out first; a card that keeps blocking asks you once.

Hermes blocks a card with a kind (``needs_input``, ``capability``, ``transient``, ``dependency``) and counts how often the same
kind comes back after an unblock; the second time it moves the card to ``triage`` for a person, where no bot can unblock it.
Every kind but ``dependency`` then waits for someone to look, and until now nobody was told to.

On ``kanban_task_blocked`` this picks one route:

- **wake**: a block a bot can act on (needs input, a missing capability, or untyped). The bot that created the card, or the
  card's own bot when no bot on this server created it, is woken in its "Kanban desk" chat with the card, the reason and the
  latest comments. It can answer and unblock it, re-plan, or set up a card for another bot first; what only the person can
  decide goes on the card as a question, which the app shows in Needs you. A card's bots get ``WAKES_PER_CARD`` such tries.
- **retry**: a transient block (a flaky failure). It is unblocked once, ``RETRY_AFTER_S`` later, with no model run; if it blocks
  as transient again, Hermes moves it to triage.
- **loop**: Hermes moved the card to triage, or its bots used their tries. No bot is woken again; the person is asked once,
  through the push and Needs you that cover blocked and triaged cards, and ``kanban_card_looping`` fires once.
- **skip**: the card is no longer blocked (a dependency wait fires this hook before it is saved, and someone may have
  unblocked it already), the same block was already handed over, or no bot made or works the card (Needs you shows it).

A wake or retry is a request file under the plugin's folder (``requests/``), never a process started here: this hook fires
inside the card's worker, which exits right after it blocks (and on Hermes 0.22 takes its children with it). The desk runner
in the gateway (``desk.py``) carries it out.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

try:
    from . import board as B
except ImportError:  # the tests import the modules flat
    import board as B  # type: ignore[no-redef]

log = logging.getLogger("hermes-kanban")

#: Kinds a bot can act on. Untyped blocks (None) count too: many bots block without naming a kind.
ACTIONABLE = {"needs_input", "capability", None}

#: How many different blocks of one card its bots are woken for before the person is asked instead.
WAKES_PER_CARD = 2

#: How long after a transient block the card is retried.
RETRY_AFTER_S = 10 * 60

#: Wakes and loops are remembered this long, so an old card that comes back is handled afresh.
REMEMBER_S = 30 * 24 * 3600


def wake_key(task_id: str, kind: Optional[str], reason: str) -> str:
    digest = hashlib.sha256(f"{kind}|{' '.join(reason.split())}".encode("utf-8")).hexdigest()[:16]

    return f"{task_id}:{digest}"


def wakes_for(woken: dict, task_id: str) -> int:
    return sum(1 for key in woken if key.startswith(f"{task_id}:"))


def decide(card: dict, reason: str, *, is_bot: Callable[[str], bool], woken: dict) -> dict:
    """``{route: wake|retry|loop|skip, why, target?, key?}`` for a card that just blocked. Pure."""
    kind, status = card.get("block_kind"), card.get("status")

    if status == "triage" or (status == "blocked" and int(card.get("block_recurrences") or 0) >= 2):
        return {"route": "loop", "why": "the same block came back"}

    if status != "blocked":
        return {"route": "skip", "why": f"the card is {status or 'gone'}, not blocked"}

    if kind == "transient":
        key = f"retry:{card['id']}"

        if key in woken:
            return {"route": "loop", "why": "it was already retried once"}

        return {"route": "retry", "why": "a flaky failure gets one retry", "key": key}

    if kind not in ACTIONABLE:
        return {"route": "skip", "why": f"a block of kind {kind!r}"}

    creator, assignee = card.get("created_by") or "", card.get("assignee") or ""
    target = creator if is_bot(creator) else assignee if is_bot(assignee) else ""

    if not target:
        return {"route": "skip", "why": "no bot on this server made or works this card"}

    key = wake_key(card["id"], kind, reason)

    if key in woken:
        return {"route": "skip", "why": "this block was already handed to a bot"}

    if wakes_for(woken, card["id"]) >= WAKES_PER_CARD:
        return {"route": "loop", "why": f"its bots already had {WAKES_PER_CARD} tries"}

    return {"route": "wake", "why": "a bot can sort this out", "target": target, "key": key}


def desk_message(card: dict, reason: str, comments: list[dict], board_name: str, target: str) -> str:
    """What the woken bot reads in its Kanban desk chat."""
    worker = card.get("assignee") or "no bot yet"
    role = "You created this card" if target == card.get("created_by") else "You work this card"
    on_board = f" on board {board_name}" if board_name and board_name != "default" else ""
    board_arg = f', board "{board_name}"' if on_board else ""
    cli = f"hermes kanban --board {board_name}" if on_board else "hermes kanban"
    lines = [
        f'[Kanban desk] Card {card["id"]} "{card.get("title") or "untitled"}"{on_board} is blocked'
        + (f" ({card['block_kind']})" if card.get("block_kind") else "") + f". It is worked by {worker}.",
        "",
        f"Why it blocked: {' '.join(reason.split()) or '(no reason given)'}",
    ]

    if comments:
        lines += ["", "Latest comments:"] + [f"- {c['author'] or 'someone'}: {' '.join(c['body'].split())[:600]}" for c in comments]

    lines += [
        "",
        f"{role}, so sort it out if you can. Use your kanban tools (task_id \"{card['id']}\"{board_arg}), or `{cli}` in the terminal:",
        f"- Read all of it: kanban_show (`{cli} show {card['id']}`).",
        "- If you can answer it or give it what it needs, say so on the card with kanban_comment, then kanban_unblock it "
        f"(`{cli} comment {card['id']} \"...\"`, `{cli} unblock {card['id']}`).",
        "- If another bot has to do something first, kanban_create a card for that bot and kanban_link it as this card's parent; "
        "this card then waits for it by itself.",
        "- If only the person can decide, leave it blocked and add a comment that starts with \"Question for you:\". "
        "They see it in Needs you, and their answer comes back on the card.",
        "Don't redo the card's work here, and don't unblock it unchanged: it would block again. "
        "Reply in one or two sentences saying what you did.",
    ]

    return "\n".join(lines)


class Supervisor:
    def __init__(self, root: Optional[Path] = None, clock: Callable[[], float] = time.time, emit: Optional[Callable[..., Any]] = None,
                 open_board=None, read_card=None, read_comments=None):
        self._root = root
        self._clock = clock
        self._emit = emit or (lambda *args, **kw: None)
        self._open_board = open_board or B.board
        self._read_card = read_card or B.card
        self._read_comments = read_comments or B.recent_comments

    @property
    def root(self) -> Path:
        return Path(self._root or B.hermes_root())

    def state(self) -> "B.State":
        return B.State(B.data_dir(self.root) / "woken.json")

    def on_blocked(self, task_id=None, board=None, assignee=None, run_id=None, reason=None, **_: Any) -> Optional[dict]:
        """The ``kanban_task_blocked`` hook; returns the decision. The caller keeps any error away from Hermes."""
        task_id, reason, board = str(task_id or ""), str(reason or ""), (str(board) if board else None)

        if not task_id:
            return None

        with self._open_board(board) as conn:
            card = self._read_card(conn, task_id)
            comments = self._read_comments(conn, task_id) if card else []

        if card is None:
            return None

        now = self._clock()
        woken = self.state().read()
        decision = decide(card, reason, is_bot=lambda name: B.is_bot(name, self.root), woken=woken)
        route = decision["route"]
        payload = dict(task_id=task_id, board=board, title=card.get("title"), assignee=card.get("assignee"),
                       created_by=card.get("created_by"), kind=card.get("block_kind"), recurrences=card.get("block_recurrences"),
                       reason=reason, route=route, target=decision.get("target"))
        self._emit("kanban_card_blocked", **payload)

        if route == "loop" and f"loop:{task_id}" not in woken:
            self._remember(f"loop:{task_id}", now)
            self._emit("kanban_card_looping", **payload)
        elif route == "wake":
            message = desk_message(card, reason, comments, board or "", decision["target"])
            self.request({"action": "wake", "target": decision["target"], "task_id": task_id, "board": board,
                          "key": decision["key"], "message": message, "not_before": now})
            self._remember(decision["key"], now)
        elif route == "retry":
            self.request({"action": "retry", "task_id": task_id, "board": board, "key": decision["key"],
                          "not_before": now + RETRY_AFTER_S})
            self._remember(decision["key"], now)

        log.info("hermes-kanban: card %s blocked -> %s (%s)", task_id, route, decision["why"])

        return decision

    def request(self, fields: dict) -> Path:
        """Writes a request down for the desk runner (atomically: it only reads finished ``.json`` files)."""
        folder = B.data_dir(self.root) / "requests"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{int(self._clock() * 1000)}-{uuid.uuid4().hex[:8]}.json"
        tmp = folder / f".{path.name}.tmp"
        tmp.write_text(json.dumps({"v": 1, "at": self._clock(), "tries": 0, **fields}), encoding="utf-8")
        tmp.replace(path)

        return path

    def _remember(self, key: str, now: float) -> None:
        def change(data: dict) -> None:
            for old in [k for k, at in data.items() if not isinstance(at, (int, float)) or now - at > REMEMBER_S]:
                del data[old]

            data[key] = now

        self.state().update(change)
