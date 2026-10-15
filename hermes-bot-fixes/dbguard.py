"""Repairs SQLite files whose first bytes were overwritten by a stray TLS record.

A long-running Hermes can write one 24-byte TLS record (``17 03 03 00 13`` and 19 payload bytes) at offset 5 of an
SQLite file whose descriptor number a closing TLS connection still held. SQLite then refuses the whole file ("file is
not a database") and whatever lives in it stops: group rooms, kanban, cron ledgers. Only bytes 5..28 are hit, and they
are header fields with known values, so the file can be put back exactly:

    0..15   "SQLite format 3\\0"
    16..17  page size (one that divides the file size and passes SQLite's integrity check on a copy)
    18..19  file format: 2,2 (WAL) when a -wal file sits next to it, else 1,1
    20..23  reserved 0, payload fractions 64, 32, 32 (fixed by the format)
    24..27  change counter: the version-valid-for number at 92..95, so the stored page count is trusted
    28      high byte of the page count (29..31 are intact)

The guard acts only on that exact signature. It backs the file up, repairs a copy, runs ``integrity_check`` on the copy,
and only then writes those 29 bytes into the original, in place, so processes that hold it open keep a valid descriptor.
A file too big to copy cheaply (Hermes's own state.db) is reported, not repaired.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import sqlite3
import struct
import threading
import time
from pathlib import Path

log = logging.getLogger("hermes-bot-fixes")

MAGIC = b"SQLite format 3\x00"
PAGE_SIZES = (4096, 1024, 2048, 8192, 16384, 32768, 65536)
SUFFIXES = (".db", ".sqlite", ".sqlite3")
#: Folders under the Hermes root or a profile that hold no databases of Hermes's own (or hold this plugin's backups).
SKIP_DIRS = {"backups", "hermes-bot-fixes", "hermes-agent", "node_modules", "bot-desktop", "cache", "logs", "plugins", "skills"}
MAX_REPAIR_BYTES = 512 * 1024 * 1024
KEEP_BACKUPS = 3
KEEP_EVENTS = 50
INTERVAL_SECONDS = 600
FIRST_SCAN_DELAY = 30


def damaged(head: bytes) -> bool:
    """The exact signature: ``SQLit`` intact, then a TLS application-data record header at offset 5."""
    return len(head) >= 29 and head[:5] == b"SQLit" and head[5:8] == b"\x17\x03\x03" and head[:16] != MAGIC


def header(head: bytes, size: int, page: int, wal: bool) -> bytes:
    """The 29 bytes that go back at offset 0."""
    return (MAGIC + struct.pack(">H", 1 if page == 65536 else page) + bytes([2, 2] if wal else [1, 1])
            + bytes([0, 64, 32, 32]) + head[92:96] + bytes([((size // page) >> 24) & 0xFF]))


def candidates(root: Path) -> list[Path]:
    """SQLite-looking files in the Hermes root and each profile, two folder levels deep."""
    homes = [root] + sorted(p for p in (root / "profiles").glob("*") if p.is_dir())
    found: list[Path] = []

    for home in homes:
        for depth in ("*", "*/*"):
            for path in home.glob(depth):
                if path.suffix not in SUFFIXES or not path.is_file() or ".corrupt" in path.name:
                    continue

                if any(part in SKIP_DIRS for part in path.relative_to(home).parts[:-1]) or "profiles" in path.relative_to(home).parts[:-1]:
                    continue

                found.append(path)

    return sorted(set(found))


def _read_head(path: Path) -> bytes:
    with open(path, "rb") as f:
        return f.read(100)


def _check(copy: Path) -> list:
    con = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)

    try:
        return con.execute("PRAGMA integrity_check").fetchall()
    finally:
        con.close()


def _prune(folder: Path, stem: str) -> None:
    for old in sorted(folder.glob(f"{stem}.*.corrupt"))[:-KEEP_BACKUPS]:
        with contextlib.suppress(OSError):
            old.unlink()


def repair(path: Path, state: Path, max_bytes: int = MAX_REPAIR_BYTES, clock=time.time) -> dict | None:
    """Repairs one file if it has the signature. None when it doesn't; otherwise what happened."""
    head = _read_head(path)

    if not damaged(head):
        return None

    size = path.stat().st_size
    event = {"path": str(path), "at": int(clock()), "size": size}

    if size > max_bytes:
        log.error("hermes-bot-fixes: %s has the TLS header damage but is too big to repair automatically", path)

        return {**event, "status": "too_large"}

    backups, work = state / "backups", state / "work"
    backups.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(clock()))
    backup = backups / f"{path.name}.{stamp}.corrupt"
    shutil.copy2(path, backup)
    _prune(backups, path.name)
    wal = Path(f"{path}-wal").exists()
    copy = work / f"{path.name}.repairing"

    try:
        for page in PAGE_SIZES:
            if size % page:
                continue

            shutil.copy2(backup, copy)

            # The copy is checked as a rollback-journal file: a WAL header with no -wal beside it can't open read-only.
            with open(copy, "r+b") as f:
                f.seek(0)
                f.write(header(head, size, page, wal=False))

            try:
                result = _check(copy)
            except sqlite3.DatabaseError:
                continue

            if result != [("ok",)]:
                continue

            # The original must still be the file that was checked.
            if _read_head(path) != head or path.stat().st_size != size:
                return {**event, "status": "changed", "backup": str(backup)}

            fd = os.open(path, os.O_WRONLY)

            try:
                os.pwrite(fd, header(head, size, page, wal), 0)
                os.fsync(fd)
            finally:
                os.close(fd)

            log.warning("hermes-bot-fixes: repaired the TLS-damaged header of %s (page size %d); the damaged copy is %s", path, page, backup)

            return {**event, "status": "repaired", "page": page, "wal": wal, "backup": str(backup)}

        log.error("hermes-bot-fixes: %s has the TLS header damage, but no page size gave a clean integrity check; left as it was", path)

        return {**event, "status": "failed", "backup": str(backup)}
    finally:
        with contextlib.suppress(OSError):
            copy.unlink()


def _write_status(state: Path, events: list[dict], scanned: int, clock=time.time) -> None:
    path = state / "status.json"

    try:
        previous = json.loads(path.read_text(encoding="utf-8")).get("events", [])
    except (OSError, ValueError):
        previous = []

    status = {"last_scan": int(clock()), "files_checked": scanned, "events": (previous + events)[-KEEP_EVENTS:]}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(status, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def scan(root: Path, state: Path, max_bytes: int = MAX_REPAIR_BYTES, clock=time.time) -> list[dict]:
    """Checks every candidate once (one process at a time, by a lock file). Returns what it did."""
    state.mkdir(parents=True, exist_ok=True)
    events: list[dict] = []
    files = candidates(root)

    with open(state / "guard.lock", "w") as lock:
        try:
            import fcntl

            fcntl.flock(lock, fcntl.LOCK_EX)
        except ImportError:  # not POSIX: run unlocked
            pass

        for path in files:
            try:
                event = repair(path, state, max_bytes, clock)
            except Exception as error:
                event = {"path": str(path), "at": int(clock()), "status": "error", "error": type(error).__name__}
                log.exception("hermes-bot-fixes: checking %s failed", path)

            if event:
                events.append(event)

        _write_status(state, events, len(files), clock)

    return events


_started = False
_start_lock = threading.Lock()


def default_root() -> Path:
    try:
        from hermes_constants import get_default_hermes_root

        return Path(get_default_hermes_root())
    except Exception:
        return Path.home() / ".hermes"


def start(root: Path | None = None, interval: float = INTERVAL_SECONDS, delay: float = FIRST_SCAN_DELAY) -> bool:
    """Checks now (after a short delay) and every ``interval`` seconds, once per process. True when this call started it."""
    global _started

    with _start_lock:
        if _started:
            return False

        _started = True

    base = root or default_root()

    def loop():
        time.sleep(delay)

        while True:
            try:
                scan(base, base / "hermes-bot-fixes")
            except Exception:
                log.exception("hermes-bot-fixes: database check failed")

            time.sleep(interval)

    threading.Thread(target=loop, name="hermes-bot-fixes-dbguard", daemon=True).start()

    return True
