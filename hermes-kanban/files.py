"""A card's files, and the files of the cards it waits for, copied into its workspace before its worker starts.

Hermes tells a worker about its card's attachments by their path on the server (``<root>/kanban/attachments/<card>/...``). A
worker whose terminal runs in Docker or on another machine cannot open that path unless someone mounted the folder, and a
parent card's attachments are not mentioned at all. The workspace is the one folder every terminal backend has: Docker mounts it
at ``/workspace``, and it is the worker's working directory on the server itself.

On ``kanban_task_claimed`` (in the dispatcher, right before the worker starts) the files are copied, never moved or linked, so
the originals stay as they were, to ``.kanban-files/<card>/`` in the workspace (parents' files under ``from-<parent>/``), and one
comment on the card lists them by their path relative to the workspace. The worker reads that comment in its context. A git
worktree workspace is left alone (copies would show up as changes in the repository); its worker keeps Hermes's own paths.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any, Callable, Optional

try:
    from . import board as B
except ImportError:  # the tests import the modules flat
    import board as B  # type: ignore[no-redef]

log = logging.getLogger("hermes-kanban")

FOLDER = ".kanban-files"
MARK = "[Files for this card]"

#: Bigger files are left where they are (the claim hook should stay quick); the comment says so.
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024


def safe_name(name: str) -> str:
    base = Path(str(name or "").replace("\\", "/")).name.strip()

    return base if base not in ("", ".", "..") else "file"


def plan(task_id: str, own: list[dict], from_parents: list[tuple[dict, list[dict]]]) -> list[dict]:
    """``[{source, rel, size, label}]``: where each file goes, relative to the workspace. Names that collide get a number."""
    items, taken = [], set()

    def add(attachment: dict, folder: str, label: str) -> None:
        name = safe_name(attachment.get("filename") or Path(attachment["stored_path"]).name)
        stem, dot, ext = name.rpartition(".")
        rel, n = f"{folder}/{name}", 2

        while rel in taken:
            rel = f"{folder}/{stem or ext}-{n}{dot}{ext if stem else ''}"
            n += 1

        taken.add(rel)
        items.append({"source": attachment["stored_path"], "rel": rel, "size": int(attachment.get("size") or 0), "label": label})

    for attachment in own:
        add(attachment, f"{FOLDER}/{task_id}", "")

    for parent, attachments in from_parents:
        title = f' "{parent["title"]}"' if parent.get("title") else ""

        for attachment in attachments:
            add(attachment, f"{FOLDER}/{task_id}/from-{safe_name(parent['id'])}", f"from card {parent['id']}{title}")

    return items


def copy_into(workspace: Path, items: list[dict]) -> tuple[list[dict], list[dict]]:
    """Copies what fits; ``(copied, left)``. A file already there at the same size and no older is not copied again."""
    copied, left, total = [], [], 0
    root = workspace.resolve()

    for item in items:
        source, target = Path(item["source"]), (workspace / item["rel"])

        try:
            size = source.stat().st_size
        except OSError:
            left.append({**item, "why": "missing"})
            continue

        if size > MAX_FILE_BYTES or total + size > MAX_TOTAL_BYTES:
            left.append({**item, "why": "too big to copy"})
            continue

        if not target.resolve().is_relative_to(root):  # never write outside the workspace
            left.append({**item, "why": "bad name"})
            continue

        total += size

        try:
            there = target.stat()

            if there.st_size == size and there.st_mtime >= source.stat().st_mtime:
                copied.append(item)
                continue
        except OSError:
            pass

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{target.name}.part")
            shutil.copy2(source, tmp)
            os.replace(tmp, target)
            copied.append(item)
        except OSError as error:
            log.warning("hermes-kanban: copying a file into a card's workspace failed: %s", error)
            left.append({**item, "why": "could not be copied"})

    return copied, left


def note(copied: list[dict], left: list[dict]) -> str:
    """The comment that tells the worker where its files are."""
    lines = [
        f"{MARK} Copied into this card's workspace, so they open wherever the work runs: use these paths from the workspace "
        "folder (in Docker that is /workspace).",
    ]
    lines += [f"- {item['rel']}" + (f" ({item['label']})" if item["label"] else "") for item in copied]

    if left:
        lines += ["", "Not copied (still at their original paths on the server):"]
        lines += [f"- {item['source']} ({item['why']}" + (f", {item['label']}" if item["label"] else "") + ")" for item in left]

    return "\n".join(lines)


class Stager:
    def __init__(self, emit: Optional[Callable[..., Any]] = None, open_board=None, board_api=B):
        self._emit = emit or (lambda *args, **kw: None)
        self._open_board = open_board or B.board
        self._api = board_api

    def on_claimed(self, task_id=None, board=None, **_: Any) -> Optional[dict]:
        """The ``kanban_task_claimed`` hook: stage the files and say where they are. Returns what it did, or None."""
        task_id, board = str(task_id or ""), (str(board) if board else None)

        if not task_id:
            return None

        api = self._api

        with self._open_board(board) as conn:
            own = api.attachments(conn, task_id)
            from_parents = [(parent, api.attachments(conn, parent["id"])) for parent in api.parents(conn, task_id)]
            items = plan(task_id, own, [(p, a) for p, a in from_parents if a])

            if not items:
                return None

            workspace = api.workspace(conn, task_id, board)

            if workspace is None:
                return None

            copied, left = copy_into(workspace, items)

            if not copied:
                return {"copied": [], "left": left}

            body = note(copied, left)

            if not any(c["author"] == api.AUTHOR and c["body"] == body for c in api.recent_comments(conn, task_id, None)):
                api.comment(conn, task_id, body)

        self._emit("kanban_card_files_staged", task_id=task_id, board=board, workspace=str(workspace),
                   files=[item["rel"] for item in copied], left=[item["source"] for item in left])
        log.info("hermes-kanban: copied %d file(s) into card %s's workspace", len(copied), task_id)

        return {"copied": copied, "left": left}
