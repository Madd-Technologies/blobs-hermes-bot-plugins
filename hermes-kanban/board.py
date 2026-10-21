"""What this plugin reads from Hermes's kanban board, and where it keeps its own small state. Hermes 0.21 and 0.22 have the same
board API for these (``hermes_cli.kanban_db`` and ``kanban_db_connect``), so nothing here differs by version.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from contextlib import contextmanager

try:
    import fcntl
except ImportError:  # Windows: one process at a time is all the thread lock promises there
    fcntl = None  # type: ignore[assignment]
from pathlib import Path
from typing import Any, Optional

#: The author this plugin's comments carry on a card.
AUTHOR = "hermes-kanban"

#: Hermes's profile names (`-p <name>`); a card's `created_by` that is one of these, on this server, is a bot.
PROFILE_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789_-")

_lock = threading.Lock()


def hermes_root() -> Path:
    from hermes_constants import get_default_hermes_root

    return Path(get_default_hermes_root())


def data_dir(root: Optional[Path] = None) -> Path:
    """The plugin's own folder at the Hermes root, shared by every process and bot on this server."""
    return Path(root or hermes_root()) / "hermes-kanban"


def is_bot(name: Optional[str], root: Optional[Path] = None) -> bool:
    """Whether a name is a bot (profile) on this server: `default`, or a folder under the root's `profiles/`."""
    name = str(name or "")

    if not name or not set(name) <= PROFILE_CHARS or name.startswith(("-", "_")):
        return False

    return name == "default" or (Path(root or hermes_root()) / "profiles" / name).is_dir()


def profile_home(name: str, root: Optional[Path] = None) -> Path:
    root = Path(root or hermes_root())

    return root if name == "default" else root / "profiles" / name


@contextmanager
def board(name: Optional[str] = None):
    """A connection to a kanban board (the current one when not named), closed after."""
    from hermes_cli import kanban_db_connect

    conn = kanban_db_connect.connect(board=name or None)

    try:
        yield conn
    finally:
        conn.close()


def card(conn, task_id: str) -> Optional[dict[str, Any]]:
    """The card as this plugin needs it, or None: title, assignee, status, who created it and from which chat, and its last
    block's kind and how often that kind recurred."""
    from hermes_cli import kanban_db

    task = kanban_db.get_task(conn, task_id)

    if task is None:
        return None

    row = conn.execute("SELECT block_kind, block_recurrences FROM tasks WHERE id = ?", (task_id,)).fetchone()

    return {
        "id": task.id,
        "title": str(getattr(task, "title", "") or ""),
        "assignee": str(getattr(task, "assignee", "") or ""),
        "status": str(getattr(task, "status", "") or ""),
        "created_by": str(getattr(task, "created_by", "") or ""),
        "session_id": str(getattr(task, "session_id", "") or ""),
        "workspace_path": str(getattr(task, "workspace_path", "") or ""),
        "block_kind": (row[0] if row else None),
        "block_recurrences": int((row[1] if row else 0) or 0),
    }


def recent_comments(conn, task_id: str, limit: Optional[int] = 5) -> list[dict[str, str]]:
    """The card's comments, oldest first: the last ``limit`` of them, or all with None."""
    from hermes_cli import kanban_db

    try:
        comments = kanban_db.list_comments(conn, task_id)
    except Exception:
        return []

    shown = comments if limit is None else comments[-limit:]

    return [{"author": str(getattr(c, "author", "") or ""), "body": str(getattr(c, "body", "") or "")} for c in shown]


def attachments(conn, task_id: str) -> list[dict[str, Any]]:
    """The files attached to a card (URL attachments have no stored file and are left out)."""
    from hermes_cli import kanban_db

    return [
        {"filename": str(a.filename or ""), "stored_path": str(a.stored_path or ""), "size": int(a.size or 0)}
        for a in kanban_db.list_attachments(conn, task_id)
        if getattr(a, "stored_path", None)
    ]


def parents(conn, task_id: str) -> list[dict[str, str]]:
    """The cards this one waits for, with their titles."""
    from hermes_cli import kanban_db

    found = []

    for parent_id in kanban_db.parent_ids(conn, task_id):
        task = kanban_db.get_task(conn, parent_id)
        found.append({"id": str(parent_id), "title": str(getattr(task, "title", "") or "") if task else ""})

    return found


def comment(conn, task_id: str, body: str) -> None:
    from hermes_cli import kanban_db

    kanban_db.add_comment(conn, task_id, AUTHOR, body)


def unblock(conn, task_id: str) -> bool:
    from hermes_cli import kanban_db

    return bool(kanban_db.unblock_task(conn, task_id))


def workspace(conn, task_id: str, board_name: Optional[str] = None) -> Optional[Path]:
    """The card's workspace folder (created if missing), the one its worker runs in, or None for a git worktree: a worktree
    is the dispatcher's to set up, and files copied into one would show as changes in the repository."""
    from hermes_cli import kanban_db, kanban_db_workspace

    task = kanban_db.get_task(conn, task_id)

    if task is None or (getattr(task, "workspace_kind", None) or "scratch") not in ("scratch", "dir"):
        return None

    return Path(kanban_db_workspace.resolve_workspace(task, board=board_name or None))


@contextmanager
def file_lock(path: Path):
    """Holds an exclusive lock on ``path`` (created if missing) across processes, and this process's threads."""
    path.parent.mkdir(parents=True, exist_ok=True)

    with _lock, open(path, "a+") as handle:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_EX)

        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle, fcntl.LOCK_UN)


class State:
    """A small JSON file under the plugin's folder (which wakes were already asked for), written atomically. Bots' workers
    write it from their own processes, so updates hold a lock file next to it."""

    def __init__(self, path: Path):
        self.path = path

    def read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

        return data if isinstance(data, dict) else {}

    def update(self, change) -> dict:
        """Read, apply ``change(data)`` (which edits it in place), and write back, under the lock."""
        with file_lock(self.path.with_name(f".{self.path.name}.lock")):
            data = self.read()
            change(data)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".state-", suffix=".json")

            try:
                with os.fdopen(fd, "w", encoding="utf-8") as out:
                    json.dump(data, out, indent=1, sort_keys=True)

                os.replace(tmp, self.path)
            except Exception:
                Path(tmp).unlink(missing_ok=True)
                raise

            return data
