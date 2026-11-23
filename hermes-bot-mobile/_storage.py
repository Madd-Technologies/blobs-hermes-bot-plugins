"""Shared stdlib atomic JSON / JSONL storage and Hermes root resolution for hermes-bot-mobile modules."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


def hermes_root() -> Path:
    try:
        from hermes_constants import get_default_hermes_root

        return Path(get_default_hermes_root())
    except Exception:
        return Path.home() / ".hermes"


def default_folder(root: Path | None = None) -> Path:
    base = Path(root) if root is not None else hermes_root()
    return base / "hermes-bot-mobile"


class JsonFile:
    """One small JSON document with atomic writes and a lock around read-modify-write."""

    _guards: dict[str, threading.Lock] = {}
    _guards_lock = threading.Lock()

    def __init__(self, path: Path, empty):
        self.path = Path(path)
        self._empty = empty

        with JsonFile._guards_lock:
            self._guard = JsonFile._guards.setdefault(str(self.path), threading.Lock())

    def read(self):
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (FileNotFoundError, ValueError):
            return self._empty()

        return data if isinstance(data, type(self._empty())) else self._empty()

    def update(self, change):
        """Run `change(data)` under the lock and save what it leaves in `data`. Returns `change`'s result."""
        self.path.parent.mkdir(parents=True, exist_ok=True)

        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:  # pragma: no cover
            pass

        with self._guard:
            lock = open(self.path.with_suffix(self.path.suffix + ".lock"), "a+")

            try:
                if fcntl is not None:
                    fcntl.flock(lock, fcntl.LOCK_EX)

                data = self.read()
                result = change(data)
                self._write(data)

                return result
            finally:
                lock.close()

    def _write(self, data) -> None:
        handle, temp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name, suffix=".tmp")

        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                json.dump(data, out, indent=2, sort_keys=True)

            os.chmod(temp, 0o600)
            os.replace(temp, self.path)
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass

            raise


class JsonLog:
    """Locked bounded JSONL log for recent briefing activity events."""

    _guards: dict[str, threading.Lock] = {}
    _guards_lock = threading.Lock()

    def __init__(self, path: Path, max_rows: int = 500, max_age_s: int = 7 * 86_400):
        self.path = Path(path)
        self.max_rows = max_rows
        self.max_age_s = max_age_s

        with JsonLog._guards_lock:
            self._guard = JsonLog._guards.setdefault(str(self.path), threading.Lock())

    def read(self) -> list[dict]:
        rows: list[dict] = []

        try:
            with open(self.path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        parsed = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(parsed, dict):
                        rows.append(parsed)
        except FileNotFoundError:
            return []

        return rows

    def append(self, entry: dict, now: float | None = None) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        cutoff = float(now if now is not None else time.time()) - self.max_age_s

        with self._guard:
            lock = open(self.path.with_suffix(self.path.suffix + ".lock"), "a+")

            try:
                if fcntl is not None:
                    fcntl.flock(lock, fcntl.LOCK_EX)

                rows = [row for row in self.read() if float(row.get("at") or 0) >= cutoff]
                rows.append(dict(entry))

                if len(rows) > self.max_rows:
                    rows = rows[-self.max_rows :]

                handle, temp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name, suffix=".tmp")

                try:
                    with os.fdopen(handle, "w", encoding="utf-8") as out:
                        for row in rows:
                            out.write(json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n")

                    os.chmod(temp, 0o600)
                    os.replace(temp, self.path)
                except BaseException:
                    try:
                        os.unlink(temp)
                    except OSError:
                        pass
                    raise
            finally:
                lock.close()

        return dict(entry)
