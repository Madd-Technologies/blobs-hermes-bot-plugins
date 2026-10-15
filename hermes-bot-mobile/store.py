"""The phones that want pushes, and how loud each bot may be: two small JSON files under the Hermes root.

The agent hooks run inside the gateway and the dashboard API runs inside `hermes serve`; Hermes imports them as
separate modules, so they cannot share objects. They share these files instead, written atomically and re-read on
every use (a push is rare enough that reading a few hundred bytes costs nothing).

The files live under the Hermes *root*, not a profile home: there is one phone and many bots.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

try:  # POSIX only; Hermes servers run on Linux and macOS, and the in-process lock covers the rest.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

try:
    from . import webpush
except ImportError:  # the tests import the modules flat
    import webpush  # type: ignore[no-redef]

MAX_DEVICES = 10
MAX_ENDPOINT = 2048

#: A phone's "I am in front, showing these sessions" lasts this long unless repeated (it repeats every 20 s).
PRESENCE_TTL = 45.0
PRESENCE_TTL_MAX = 120.0
MAX_PRESENCE_SESSIONS = 16

#: Per-bot levels: `off` is silence. Since 0.1.2 `all` and `needs_you` are the same (approvals, questions and broken
#: routines): nothing else is pushed any more. Both stay accepted so older apps keep working.
LEVELS = ("all", "needs_you", "off")


#: What hermes-push (this plugin's former name) kept; carried over once so registered phones keep their pushes.
CARRIED_FILES = ("devices.json", "settings.json", "vapid.json", "presence.json")


def default_dir() -> Path:
    from hermes_constants import get_default_hermes_root

    root = get_default_hermes_root()
    folder = root / "hermes-bot-mobile"
    carry_over(root / "hermes-push", folder)

    return folder


def carry_over(old: Path, new: Path) -> list[str]:
    """Copy hermes-push's files into a new folder that has none of them yet, keeping the same push keys, so a phone
    registered with hermes-push goes on getting pushes without registering again. The old folder stays as it was.
    Returns the names copied (none once the new folder has its own files)."""
    if any((new / name).exists() for name in CARRIED_FILES) or not old.is_dir():
        return []

    copied = []
    new.mkdir(parents=True, exist_ok=True)
    os.chmod(new, 0o700)

    for name in CARRIED_FILES:
        source = old / name

        if source.is_file():
            temp = new / f".{name}.carry"
            temp.write_bytes(source.read_bytes())
            os.chmod(temp, 0o600)
            os.replace(temp, new / name)
            copied.append(name)

    return copied


def device_id(endpoint: str) -> str:
    """The same phone re-registering (a new key pair, the same endpoint) replaces its row instead of adding one."""
    return hashlib.sha256(endpoint.encode()).hexdigest()[:12]


def validate_device(endpoint: str, p256dh: str, auth: str) -> None:
    """Raise ValueError unless these three values are what a push to this device needs.

    Checked at registration so a bad row never reaches `webpush.encrypt` at the moment a bot is waiting on you.
    """
    if not isinstance(endpoint, str) or not endpoint or len(endpoint) > MAX_ENDPOINT:
        raise ValueError("endpoint is missing or too long")

    parts = urlsplit(endpoint)

    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("endpoint must be an http(s) URL")

    try:
        point = webpush.b64u_decode(p256dh)
        secret = webpush.b64u_decode(auth)
    except Exception as failure:
        raise ValueError("keys must be base64url") from failure

    if len(secret) != 16:
        raise ValueError("auth must be 16 bytes")

    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)
    except Exception as failure:
        raise ValueError("p256dh is not a P-256 public key") from failure


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

            os.chmod(temp, 0o600)  # the rows hold keys that let anyone with the endpoint push to the phone
            os.replace(temp, self.path)
        except BaseException:
            try:
                os.unlink(temp)
            except OSError:
                pass

            raise


class DeviceStore:
    def __init__(self, folder: Path | None = None, clock=time.time):
        self.folder = Path(folder) if folder else default_dir()
        self._clock = clock
        self._devices = JsonFile(self.folder / "devices.json", list)
        self._settings = JsonFile(self.folder / "settings.json", dict)
        self._vapid = JsonFile(self.folder / "vapid.json", dict)
        self._presence = JsonFile(self.folder / "presence.json", dict)

    # -- the server's VAPID key --------------------------------------------------------------------------------

    def vapid_public(self) -> str:
        """The server's VAPID public key, made on first use (87 characters: a P-256 point, base64url).

        The phone's push library will not register without a well-formed one, and hands it to the push app so a push
        service could tell who is sending. ntfy ignores it; a service that checks it would need the pushes signed
        with the private half, which is kept here (0600) for that day.
        """
        def change(data: dict) -> str:
            if not (isinstance(data.get("public"), str) and isinstance(data.get("private"), str)):
                key = ec.generate_private_key(ec.SECP256R1())
                data["private"] = webpush.b64u_encode(key.private_numbers().private_value.to_bytes(32, "big"))
                data["public"] = webpush.b64u_encode(key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))

            return data["public"]

        return self._vapid.update(change)

    # -- devices -----------------------------------------------------------------------------------------------

    def devices(self) -> list[dict]:
        return self._devices.read()

    def upsert(self, endpoint: str, p256dh: str, auth: str, name: str = "") -> dict:
        validate_device(endpoint, p256dh, auth)
        identity = device_id(endpoint)
        now = int(self._clock())

        def change(rows: list[dict]) -> dict:
            existing = next((row for row in rows if row.get("id") == identity), None)

            if existing is None and len(rows) >= MAX_DEVICES:
                raise ValueError(f"at most {MAX_DEVICES} devices can be registered")

            row = existing or {"id": identity, "created": now}
            row.update(endpoint=endpoint, p256dh=p256dh, auth=auth, name=str(name or "")[:60], updated=now)

            if existing is None:
                rows.append(row)

            return dict(row)

        return self._devices.update(change)

    def remove(self, identity: str) -> bool:
        def change(rows: list[dict]) -> bool:
            kept = [row for row in rows if row.get("id") != identity]
            removed = len(kept) != len(rows)
            rows[:] = kept

            return removed

        return self._devices.update(change)

    def record(self, identity: str, error: str | None) -> None:
        """Remember how the last push to a device went, so the app can say so instead of guessing."""
        now = int(self._clock())

        def change(rows: list[dict]) -> None:
            for row in rows:
                if row.get("id") == identity:
                    row["last_error"] = error
                    row["last_attempt"] = now

                    if error is None:
                        row["last_ok"] = now

        self._devices.update(change)

    # -- settings ----------------------------------------------------------------------------------------------

    def settings(self) -> dict:
        """`{"default": level, "bots": {profile: level}}`, with anything unreadable falling back to `all`."""
        raw = self._settings.read()
        bots = raw.get("bots") if isinstance(raw.get("bots"), dict) else {}

        return {
            "default": raw.get("default") if raw.get("default") in LEVELS else "all",
            "bots": {name: level for name, level in bots.items() if isinstance(name, str) and level in LEVELS},
        }

    def level(self, profile: str) -> str:
        current = self.settings()

        return current["bots"].get(profile, current["default"])

    def set_settings(self, default: str | None, bots: dict | None) -> dict:
        """Change the default and/or merge per-bot levels; a bot set to `null` goes back to the default."""
        if default is not None and default not in LEVELS:
            raise ValueError(f"level must be one of {', '.join(LEVELS)}")

        for name, level in (bots or {}).items():
            if not isinstance(name, str) or not name or (level is not None and level not in LEVELS):
                raise ValueError(f"bad level for {name!r}")

        def change(data: dict) -> None:
            if default is not None:
                data["default"] = default

            merged = data.setdefault("bots", {})

            for name, level in (bots or {}).items():
                if level is None:
                    merged.pop(name, None)
                else:
                    merged[name] = level

        self._settings.update(change)

        return self.settings()

    # -- what a phone has on screen ----------------------------------------------------------------------------

    def set_presence(self, device: str, sessions: list[str], foreground: bool, ttl: float = PRESENCE_TTL) -> None:
        """A registered phone saying it is in front (or not) and which chat's sessions it shows, for the next `ttl` seconds.

        The phone repeats it while it stays there, so a phone that dies or loses the network simply stops counting.
        """
        if not any(row.get("id") == device for row in self.devices()):
            raise ValueError("unknown device")

        until = self._clock() + max(0.0, min(float(ttl), PRESENCE_TTL_MAX))
        kept = [str(session) for session in sessions if isinstance(session, str) and session][:MAX_PRESENCE_SESSIONS]

        def change(data: dict) -> None:
            now = self._clock()

            for key in [key for key, row in data.items() if not isinstance(row, dict) or row.get("until", 0) <= now]:
                data.pop(key, None)

            if foreground:
                data[device] = {"until": until, "sessions": kept}
            else:
                data.pop(device, None)

        self._presence.update(change)

    def presence(self) -> dict:
        """`{"foreground": bool, "sessions": set}`: whether any registered phone is in front now, and the sessions on screen."""
        now = self._clock()
        rows = [row for row in self._presence.read().values() if isinstance(row, dict) and row.get("until", 0) > now]

        return {"foreground": bool(rows), "sessions": {session for row in rows for session in row.get("sessions", []) if isinstance(session, str)}}
