"""hermes-phone's state: small JSON files under the Hermes root (`~/.hermes/hermes-phone/`), shared by the plugin (tools and
dashboard routes, inside Hermes) and the call service (its own process). Every write is atomic and locked (fcntl), and the
folder and files are private to the user: the never-share list holds the very values a call must not say.

- `settings.json`: the name to use on calls, the never-share list, limits, and where Twilio reaches the service.
- `errands/<id>.json`: one errand per file: the brief, the person's decision, and each call's progress and outcome.
- `ledger.json`: numbers this line has called (for callbacks, in a later version), and recent call starts (the daily cap).
- `service.json`: the call service's heartbeat.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

try:
    import fcntl
except ImportError:  # pragma: no cover - Hermes servers are Linux or macOS
    fcntl = None  # type: ignore[assignment]

DEFAULTS: Dict[str, Any] = {
    "name": "",
    "never_share": [],
    "do_not_call": [],
    "bots": ["default"],
    "stage": "test",
    "test_numbers": [],
    "hours": [8, 21],
    "daily_cap": 10,
    "max_minutes": 20,
    "hold_minutes": 15,
    "callback_days": 14,
    "transcripts": True,
    "ask_owner": True,
    "public_url": "",
    "port": 8796,
}
STAGES = ("test", "live")
PENDING_HOURS = 24
HEARTBEAT_S = 10
LOG_KEEP = 200

ERRAND_OPEN = ("pending", "approved", "running")
#: Errands Twilio can be calling back about: approved ones (and cancelled ones while a hang-up is under way).
DIALABLE = ("approved", "running", "cancelled")
CALL_ACTIVE = ("dialing", "ringing", "live")


class SettingsError(ValueError):
    pass


def default_root() -> Path:
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root()


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def brief_hash(brief: Dict[str, Any]) -> str:
    """What the person approved, so the dialer can refuse a brief that changed afterwards."""
    import hashlib

    return hashlib.sha256(json.dumps(brief, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class JsonFile:
    _guards: Dict[str, threading.Lock] = {}

    def __init__(self, path: Path, empty: Callable[[], Any]):
        self.path = Path(path)
        self.empty = empty
        self._guard = JsonFile._guards.setdefault(str(self.path), threading.Lock())

    def read(self) -> Any:
        try:
            with open(self.path, encoding="utf-8") as handle:
                return json.load(handle)
        except (FileNotFoundError, ValueError):
            return self.empty()

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        with self._guard:
            lock = open(self.path.with_suffix(self.path.suffix + ".lock"), "a+")
            try:
                if fcntl:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                yield
            finally:
                lock.close()

    def update(self, change: Callable[[Any], Any]) -> Any:
        with self._locked():
            data = change(self.read())
            self._write(data)
            return data

    def _write(self, data: Any) -> None:
        handle, temp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name, suffix=".tmp")
        try:
            os.fchmod(handle, 0o600)
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                json.dump(data, out, indent=1, sort_keys=True, ensure_ascii=False)
            os.replace(temp, self.path)
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise


def check_settings(change: Dict[str, Any]) -> Dict[str, Any]:
    """The settings a change may set, checked. Unknown fields are refused, not dropped."""
    out: Dict[str, Any] = {}

    for key, value in change.items():
        if value is None:
            continue
        if key == "name" and isinstance(value, str) and len(value.strip()) <= 60:
            out[key] = " ".join(value.split())
        elif key in ("transcripts", "ask_owner") and isinstance(value, bool):
            out[key] = value
        elif key == "stage" and value in STAGES:
            out[key] = value
        elif key in ("daily_cap", "max_minutes", "hold_minutes", "callback_days", "port") and isinstance(value, int) and not isinstance(value, bool):
            bounds = {"daily_cap": (1, 50), "max_minutes": (2, 60), "hold_minutes": (1, 45), "callback_days": (0, 60), "port": (1024, 65535)}[key]
            if not bounds[0] <= value <= bounds[1]:
                raise SettingsError(f"{key} must be between {bounds[0]} and {bounds[1]}.")
            out[key] = value
        elif key == "hours" and isinstance(value, list) and len(value) == 2 and all(isinstance(h, int) for h in value) and 0 <= value[0] < value[1] <= 24:
            out[key] = value
        elif key in ("bots", "do_not_call", "test_numbers") and isinstance(value, list) and all(isinstance(v, str) for v in value) and len(value) <= 100:
            out[key] = [v.strip() for v in value if v.strip()]
        elif key == "never_share" and isinstance(value, list) and len(value) <= 50 and all(
                isinstance(v, dict) and isinstance(v.get("label"), str) and isinstance(v.get("value"), str) for v in value):
            out[key] = [{"label": " ".join(v["label"].split())[:40], "value": v["value"].strip()[:200]} for v in value if v["value"].strip()]
        elif key == "public_url" and isinstance(value, str) and (value == "" or value.startswith("https://")) and len(value) <= 200:
            out[key] = value.rstrip("/")
        else:
            raise SettingsError(f"{key} cannot be set to that.")

    return out


class Store:
    def __init__(self, root: Optional[Path] = None, clock: Callable[[], float] = time.time):
        self.folder = Path(root if root is not None else default_root()) / "hermes-phone"
        self.clock = clock
        self._settings = JsonFile(self.folder / "settings.json", dict)
        self._ledger = JsonFile(self.folder / "ledger.json", lambda: {"numbers": {}, "starts": []})
        self._service = JsonFile(self.folder / "service.json", dict)

    # settings

    def settings(self) -> Dict[str, Any]:
        saved = self._settings.read()
        return {**DEFAULTS, **{k: v for k, v in (saved if isinstance(saved, dict) else {}).items() if k in DEFAULTS}}

    def update_settings(self, change: Dict[str, Any]) -> Dict[str, Any]:
        clean = check_settings(change)
        self._settings.update(lambda saved: {**(saved if isinstance(saved, dict) else {}), **clean})
        return self.settings()

    # errands

    def _errand_file(self, errand_id: str) -> JsonFile:
        if not errand_id.replace("_", "").isalnum():
            raise KeyError(errand_id)
        return JsonFile(self.folder / "errands" / f"{errand_id}.json", dict)

    def new_errand(self, profile: str, session_id: str, brief: Dict[str, Any], flags: List[Dict[str, Any]]) -> Dict[str, Any]:
        errand = {
            "id": new_id("e"), "profile": profile, "session_id": session_id or "", "created": self.clock(), "status": "pending",
            "brief": brief, "flags": flags, "ticked": [], "decided": None, "notified": False,
            "calls": [{"id": new_id("c"), "index": i, "name": c["name"], "number": c["number"], "status": "waiting",
                       "token": secrets.token_urlsafe(24), "sid": "", "answered_by": "", "started": None, "ended": None,
                       "outcome": None, "transcript": [], "question": None, "answers": [], "hangup": False, "error": "",
                       "hold": False} for i, c in enumerate(brief["calls"])],
        }
        self._errand_file(errand["id"]).update(lambda _: errand)
        return errand

    def errand(self, errand_id: str) -> Optional[Dict[str, Any]]:
        try:
            data = self._errand_file(errand_id).read()
        except KeyError:
            return None
        return data or None

    def errands(self, status: Optional[Tuple[str, ...]] = None, profile: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        out = []
        for path in sorted((self.folder / "errands").glob("e_*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            data = JsonFile(path, dict).read()
            if data and (status is None or data.get("status") in status) and (profile is None or data.get("profile") == profile):
                out.append(data)
            if len(out) >= limit:
                break
        return out

    def update_errand(self, errand_id: str, change: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
        """Run `change` on the stored errand under its lock (it edits in place) and save it."""
        def apply(data):
            if not data:
                raise KeyError(errand_id)
            change(data)
            return data

        return self._errand_file(errand_id).update(apply)

    def update_call(self, errand_id: str, call_id: str, change: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
        def apply(errand):
            call = next((c for c in errand["calls"] if c["id"] == call_id), None)
            if call is None:
                raise KeyError(call_id)
            change(call)

        errand = self.update_errand(errand_id, apply)
        return next(c for c in errand["calls"] if c["id"] == call_id)

    def find_call(self, token: str) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
        """The approved errand and call a stream or callback token belongs to."""
        if not token:
            return None
        for errand in self.errands(status=DIALABLE, limit=200):
            for call in errand["calls"]:
                if secrets.compare_digest(str(call.get("token") or ""), token):
                    return errand, call
        return None

    def expire_pending(self) -> None:
        cutoff = self.clock() - PENDING_HOURS * 3600
        for errand in self.errands(status=("pending",), limit=200):
            if errand["created"] < cutoff:
                self.update_errand(errand["id"], lambda e: e.update(status="expired"))

    # ledger and the daily cap

    def remember_number(self, number: str, entry: Dict[str, Any]) -> None:
        def apply(data):
            data.setdefault("numbers", {})[number] = {**entry, "at": self.clock()}
            return data

        self._ledger.update(apply)

    def known_number(self, number: str, days: int) -> Optional[Dict[str, Any]]:
        entry = (self._ledger.read().get("numbers") or {}).get(number)
        return entry if entry and entry.get("at", 0) >= self.clock() - days * 86400 else None

    def log_start(self) -> None:
        def apply(data):
            data["starts"] = (data.get("starts") or [])[-(LOG_KEEP - 1):] + [self.clock()]
            return data

        self._ledger.update(apply)

    def starts_today(self) -> int:
        since = self.clock() - 86400
        return sum(1 for t in self._ledger.read().get("starts") or [] if t >= since)

    # the service's heartbeat

    def beat(self, info: Dict[str, Any]) -> None:
        self._service.update(lambda _: {**info, "beat": self.clock()})

    def service(self) -> Dict[str, Any]:
        data = self._service.read() or {}
        return {**data, "running": bool(data) and self.clock() - data.get("beat", 0) < 3 * HEARTBEAT_S}
