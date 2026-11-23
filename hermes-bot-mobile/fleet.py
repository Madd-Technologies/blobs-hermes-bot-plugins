"""Fleet overview and emergency pause/resume state for hermes-bot-mobile."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

try:
    from ._storage import JsonFile, default_folder, hermes_root
except ImportError:  # tests import modules flat
    from _storage import JsonFile, default_folder, hermes_root  # type: ignore[no-redef]

PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def _count_routines(home: Path) -> int:
    jobs_file = home / "cron" / "jobs.json"
    if not jobs_file.is_file():
        return 0

    try:
        data = json.loads(jobs_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0

    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict) and isinstance(data.get("jobs"), list):
        return len(data["jobs"])

    return 0


def _last_session_at(home: Path) -> float | None:
    db_path = home / "state.db"
    if not db_path.is_file():
        return None

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
            for col in ("updated_at", "started_at", "created_at"):
                if col in cols:
                    row = conn.execute(f"SELECT MAX({col}) FROM sessions").fetchone()
                    if row and row[0] is not None:
                        val = float(row[0])
                        return val / 1000.0 if val > 10_000_000_000 else val
        finally:
            conn.close()
    except Exception:
        return None

    return None


def _bot_title(profile: str, home: Path) -> str:
    yaml_path = home / "profile.yaml"
    if yaml_path.is_file():
        try:
            for line in yaml_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                stripped = line.strip()
                if stripped.startswith("title:"):
                    val = stripped.split(":", 1)[1].strip().strip("'\"")
                    if val:
                        return val
        except OSError:
            pass

    return "Hermes" if profile == "default" else profile.replace("-", " ").replace("_", " ").title()


class Fleet:
    def __init__(self, folder: Path | None = None, root: Path | None = None, clock=time.time):
        self._clock = clock
        self._root = Path(root) if root is not None else hermes_root()
        self.folder = Path(folder) if folder is not None else default_folder(self._root)
        self._file = JsonFile(self.folder / "fleet.json", dict)

    def _state(self) -> dict:
        raw = self._file.read()
        return {
            "paused": bool(raw.get("paused", False)),
            "paused_at": raw.get("paused_at"),
            "reason": str(raw.get("reason") or ""),
            "resumed_at": raw.get("resumed_at"),
        }

    def is_paused(self) -> bool:
        return bool(self._state()["paused"])

    def _notify_gateway(self, action: str) -> None:
        # Best-effort signal to gateway control if running inside Hermes; never fail fleet state persistence.
        try:
            from gateway import status as gateway_status  # type: ignore[import-not-found]

            fn = getattr(gateway_status, f"{action}_all", None)
            if callable(fn):
                fn()
        except Exception:
            pass

    def pause(self, reason: str = "") -> dict:
        now = int(self._clock())
        clean_reason = str(reason or "").strip()[:200]

        def change(data: dict) -> dict:
            data["paused"] = True
            data["paused_at"] = now
            data["reason"] = clean_reason
            return dict(data)

        self._file.update(change)
        self._notify_gateway("pause")

        return self.status()

    def resume(self) -> dict:
        now = int(self._clock())

        def change(data: dict) -> dict:
            data["paused"] = False
            data["reason"] = ""
            data["resumed_at"] = now
            return dict(data)

        self._file.update(change)
        self._notify_gateway("resume")

        return self.status()

    def status(self) -> dict:
        state = self._state()
        now = int(self._clock())
        profiles: list[tuple[str, Path]] = [("default", self._root)]

        profiles_dir = self._root / "profiles"
        if profiles_dir.is_dir():
            for child in sorted(profiles_dir.iterdir(), key=lambda p: p.name):
                if child.is_dir() and PROFILE_RE.match(child.name):
                    profiles.append((child.name, child))

        bots = []
        for profile, home in profiles:
            bots.append(
                {
                    "profile": profile,
                    "name": _bot_title(profile, home),
                    "routines": _count_routines(home),
                    "last_active": _last_session_at(home),
                    "paused": state["paused"],
                }
            )

        return {**state, "bots": bots, "now": now}
