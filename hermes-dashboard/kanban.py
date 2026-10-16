"""Background work: each bot's kanban cards, mirrored into a collection on its dashboard, so a blocked card needs you.

Hermes 0.21's kanban board (`~/.hermes/kanban.db`, shared by every profile) is where a bot queues work for a new background
worker, and a worker that needs its person blocks its card (`kanban_block`: it runs headless and can't ask). The app knows
nothing of the board, so whenever it reads dashboards (at most every SYNC_EVERY_S) the open cards assigned to each bot that
keeps a dashboard are copied into that bot's "Background work" collection. A blocked card is a record its panel flags as
needing you (Needs you, Home, the amber dot), with why it is blocked as its body; a finished card's record closes.

The board is the truth: the mirror writes as the bot's script, over the person's edits, but writes a status only when the
card's own status changed, so a record the person swiped done stays that way until the card moves again. A note the person
adds to a card's record is their answer: it is posted on the card as a comment, and a blocked card is unblocked, so the
dispatcher starts a new worker that reads it. Nothing else on the board is changed.

Only bots that have this plugin get the panel. The board is read and written through Hermes's own `hermes_cli.kanban_db`
(`HermesBoard`); the tests use a stand-in with the same four methods.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import threading
import time

try:
    from . import store as S
except ImportError:  # the tests import the modules flat
    import store as S  # type: ignore[no-redef]

log = logging.getLogger("hermes-dashboard")

COLLECTION = "background-work"
TITLE = "Background work"
SYNC_EVERY_S = 15
#: How the person's answers are signed on the card (Hermes's own CLI signs a person's comments "user").
AUTHOR = "user (HermesBot app)"
#: Card statuses the board still works on; the rest (done, archived) finish a card.
OPEN = ("triage", "todo", "scheduled", "ready", "running", "blocked", "review")
FINISHED = ("done", "archived", "gone")
STAGE = {"triage": "Queued", "todo": "Queued", "scheduled": "Queued", "ready": "Queued", "running": "Running", "blocked": "Blocked",
         "review": "In review", "done": "Done", "archived": "Archived", "gone": "Archived"}
TEXT_LIMIT = 1500

DEFINITION = {
    "id": COLLECTION,
    "title": TITLE,
    "description": "Work this bot queued for a background worker on Hermes's kanban board. A blocked task needs you: add a note to answer it.",
    "fields": [
        {"key": "title", "label": "Task", "type": "text", "required": True},
        {"key": "status", "label": "Status", "type": "status", "stages": [{"name": "Queued"}, {"name": "Running"}, {"name": "Blocked"}, {"name": "In review"}, {"name": "Done", "done": True}, {"name": "Archived", "done": True}]},
        {"key": "why", "label": "Why it's blocked", "type": "longtext"},
        {"key": "latest", "label": "Latest", "type": "longtext"},
        {"key": "card", "label": "Card", "type": "text"},
        {"key": "updated", "label": "Updated", "type": "datetime"},
    ],
    "feed": {"how": "script", "note": "copied from the kanban board; a note answers a blocked task"},
}

VIEW = {
    "id": COLLECTION,
    "title": TITLE,
    "kind": "list",
    "collection": COLLECTION,
    "config": {
        "show": {"title": "title", "subtitle": "why", "meta": "status"},
        "needs_you": {"field": "status", "op": "eq", "value": "Blocked"},
        "sort": [{"field": "updated", "dir": "desc"}],
    },
}


def _clip(text, limit: int = TEXT_LIMIT) -> str | None:
    text = " ".join(str(text or "").split()) if limit <= 300 else str(text or "").strip()

    return (text[: limit - 1] + "…" if len(text) > limit else text) or None


class HermesBoard:
    """Hermes's own kanban board, through `hermes_cli.kanban_db` (only inside a Hermes process)."""

    def __init__(self):
        from hermes_cli import kanban_db

        self.kb = kanban_db

        try:
            from hermes_cli import kanban_db_connect

            self.kbc = kanban_db_connect
        except ImportError:  # before 0.21.5 it lived in kanban_db
            self.kbc = kanban_db

    @contextlib.contextmanager
    def _conn(self, board: str):
        conn = self.kbc.connect(board=board)

        try:
            yield conn
        finally:
            with contextlib.suppress(Exception):
                conn.close()

    def boards(self) -> list[str]:
        """The boards that exist (never creates one)."""
        return [meta["slug"] for meta in self.kb.list_boards(include_archived=False) if self.kb.kanban_db_path(meta["slug"]).exists()]

    def _card(self, conn, board: str, task) -> dict:
        comments = self.kb.list_comments(conn, task.id)
        last = comments[-1] if comments else None
        reason, blocked_at = None, 0

        status = task.status

        if status in ("blocked", "triage"):
            event = next((event for event in reversed(self.kb.list_events(conn, task.id)) if event.kind in ("blocked", "block_loop_detected")), None)
            looped = bool(event and event.kind == "block_loop_detected")

            # Blocked twice for the same reason, Hermes sends a card to triage for a person: it needs them as much.
            if status == "blocked" or looped:
                reason = (event.payload or {}).get("reason") if event else None
                reason = reason or task.last_failure_error or "It needs your input."
                reason = f"Blocked again for the same reason: {reason}" if looped else reason
                blocked_at = event.created_at if event else 0
                status = "blocked"

        if task.status == "done":
            latest = self.kb.latest_summary(conn, task.id) or task.result
        else:
            latest = f"{last.author}: {last.body}" if last else None

        updated = max(task.created_at or 0, task.started_at or 0, task.completed_at or 0, last.created_at if last else 0, blocked_at)

        return {"board": board, "id": task.id, "title": task.title, "assignee": task.assignee, "status": status, "reason": reason, "latest": latest, "updated": updated}

    def open_cards(self, board: str) -> list[dict]:
        with self._conn(board) as conn:
            return [self._card(conn, board, task) for status in OPEN for task in self.kb.list_tasks(conn, status=status)]

    def card(self, board: str, task_id: str) -> dict | None:
        """A card as it is now (done or archived included), or None when it is gone."""
        with self._conn(board) as conn:
            task = self.kb.get_task(conn, task_id)

            return self._card(conn, board, task) if task else None

    def answer(self, board: str, task_id: str, text: str) -> str | None:
        """Posts the person's answer on the card, releases it if it waits on them (unblocked, or out of triage), and
        returns its status now."""
        with self._conn(board) as conn:
            self.kb.add_comment(conn, task_id, author=AUTHOR, body=text)
            task = self.kb.get_task(conn, task_id)

            if task is not None and task.status == "blocked":
                self.kb.unblock_task(conn, task_id)
                task = self.kb.get_task(conn, task_id)
            elif task is not None and task.status == "triage" and self._card(conn, board, task)["status"] == "blocked":
                # Only a card sent to triage for blocking again; one waiting for a planner is not the person's to release.
                self.kb.specify_triage_task(conn, task_id, author=AUTHOR)
                task = self.kb.get_task(conn, task_id)

            return task.status if task else None


def values_of(card: dict) -> dict:
    """A card as the record's values. A card that is gone from the board only closes its record."""
    if card["status"] == "gone":
        return {"status": STAGE["gone"]}

    return {
        "title": _clip(card.get("title"), 300) or card["id"],
        "status": STAGE.get(card["status"], "Queued"),
        "why": _clip(card.get("reason")) if card["status"] == "blocked" else None,
        "latest": _clip(card.get("latest")),
        "card": card["id"] if card["board"] == "default" else f"{card['board']}/{card['id']}",
        "updated": int(card.get("updated") or 0) or None,
    }


def _seen(card: dict, values: dict) -> str:
    return hashlib.sha256(json.dumps([card["status"], values], sort_keys=True, default=str).encode()).hexdigest()[:16]


def ensure(store: "S.Store", profile: str) -> bool:
    """The bot's Background work collection, and its panel the first time. False when the bot retired the collection."""
    actor = S.Actor.script(profile)

    try:
        store.describe(actor, COLLECTION)

        return True
    except S.NotFound as error:
        if "retired" in str(error):
            return False

    store.create_collection(actor, DEFINITION)
    store.set_view(actor, VIEW)

    return True


def mirror(store: "S.Store", board, served) -> int:
    """Copies the board's cards into the dashboards of the bots `served(profile)` says keep one. Returns how many records
    changed."""
    known = store.mirrored_cards()
    slugs = board.boards()
    found: set = set()
    by_profile: dict[str, list[dict]] = {}

    for slug in slugs:
        for card in board.open_cards(slug):
            found.add((slug, card["id"]))

            if card.get("assignee"):
                by_profile.setdefault(card["assignee"], []).append(card)

    # Cards mirrored before that left the open lists: they finished, were archived, or are gone (with their board, too).
    for (slug, task), row in known.items():
        if (slug, task) in found or row["status"] in FINISHED:
            continue

        card = (board.card(slug, task) if slug in slugs else None) or {"board": slug, "id": task, "status": "gone"}
        card["assignee"] = card.get("assignee") or row["profile"]
        by_profile.setdefault(card["assignee"], []).append(card)

    changed = 0

    for profile, cards in by_profile.items():
        if not S.PROFILE.match(profile) or not served(profile) or not ensure(store, profile):
            continue

        actor = S.Actor.script(profile)
        items = []

        for card in cards:
            prior = known.get((card["board"], card["id"]))
            values = values_of(card)
            seen = _seen(card, values)

            if prior and prior["profile"] != profile:
                # Reassigned to another bot: the old bot's record closes, and this bot gets a new one.
                with contextlib.suppress(S.DashboardError, S.NotFound):
                    store.update(S.Actor.script(prior["profile"]), prior["record"], {"status": "Archived"}, override_person=True)

                prior = None

            if prior and prior["seen"] == seen:
                continue

            if prior and prior["status"] == card["status"]:
                values.pop("status")  # the card didn't move: a record the person closed or reopened stays as they left it

            if not values:
                store.remember_card(card["board"], card["id"], profile, prior["record"], card["status"], seen)
                continue

            items.append((card, seen, {"source_key": f"{card['board']}/{card['id']}", "values": values}))

        for start in range(0, len(items), S.MAX_BATCH):
            chunk = items[start:start + S.MAX_BATCH]
            results = store.upsert(actor, COLLECTION, [item for _card, _seen_key, item in chunk], override_person=True)

            for (card, seen, _item), result in zip(chunk, results):
                store.remember_card(card["board"], card["id"], profile, result["record"]["id"], card["status"], seen)
                changed += bool(result["changed"])

    return changed


_lock = threading.Lock()
_last = [0.0]


def sync(store: "S.Store", board=None, served=None, *, force: bool = False) -> int:
    """Mirrors the board, at most every SYNC_EVERY_S unless forced. Never raises; returns how many records changed."""
    if not force and time.monotonic() - _last[0] < SYNC_EVERY_S:
        return 0

    if not _lock.acquire(blocking=False):
        return 0

    try:
        _last[0] = time.monotonic()
        board = board or HermesBoard()
        served = served or (lambda profile: S.L.plugin_installed(store.root, profile) is True)

        return mirror(store, board, served)
    except ImportError:  # not inside Hermes: no board
        return 0
    except Exception:
        log.exception("hermes-dashboard: mirroring the kanban board failed")

        return 0
    finally:
        _lock.release()


def answer(store: "S.Store", record_id: str, text: str, board=None, served=None) -> dict | None:
    """The person's note on a card's record is their answer: posted on the card, and a blocked card unblocked. None when
    the record mirrors no card; never raises (the note is kept either way)."""
    link = store.card_of(record_id)

    if link is None:
        return None

    if link["status"] in FINISHED:
        return {"card": link["task"], "posted": False}

    try:
        board = board or HermesBoard()
        status = board.answer(link["board"], link["task"], str(text))
    except Exception as error:
        log.exception("hermes-dashboard: posting an answer on kanban card %s failed", link["task"])

        return {"card": link["task"], "posted": False, "error": str(error)[:200]}

    sync(store, board, served, force=True)

    return {"card": link["task"], "posted": True, "status": status}
