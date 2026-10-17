"""Taildrop from the server: the app's file viewer asks this Hermes server to send a file to one of the person's own
devices with `tailscale file cp`, so the file goes from the server to the device and never through the phone.

Three rules carry the weight:

- **Only what the dashboard would let this person download.** The file is checked by Hermes's own download check, the
  functions behind `GET /api/fs/download` (`_fs_download_path` and `_fs_regular_file` in
  `hermes_cli.web_routers.files`, Hermes 0.21.5), called in this process with the same `path`, `profile` and
  `session_id`, so this plugin can never reach further than that route. A Hermes without them is refused rather than
  checked some other way. The file is then opened (never following a link) and must be the very file Hermes checked
  (same device and inode); the CLI gets that open file (`/dev/fd/<n>`), so nothing can be swapped in between.
- **Only a device Taildrop itself offers.** The target must be one of `tailscale file cp --targets`, read again for
  every send, and the CLI is given that target's own Tailscale address, never the request's words.
- **Argv only, in the background.** The CLI runs in a thread; its progress line (it draws one only on a terminal, so
  it gets a pseudo-terminal for stderr) gives how far the device has received.

Sends are kept in `taildrop.json` (the last 20: when, which device, the file's name, the size and the outcome; never a
path or the contents).
"""

from __future__ import annotations

import errno
import ipaddress
import json
import logging
import os
import pwd
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

try:
    from . import store
except ImportError:  # the tests import the modules flat
    import store  # type: ignore[no-redef]

log = logging.getLogger("hermes-bot-mobile")

#: The largest file sent unless `HERMES_BOT_TAILDROP_MAX_BYTES` says otherwise.
MAX_BYTES = 2 * 1024 ** 3
MAX_BYTES_ENV = "HERMES_BOT_TAILDROP_MAX_BYTES"
MAX_DIFF_BYTES = 16 * 1024 ** 2
#: At most this many sends at once (and one per device).
MAX_SENDING = 3
KEEP = 20
COMMAND_TIMEOUT_S = 15
#: A cancelled CLI that has not stopped this long after SIGTERM is killed.
KILL_AFTER_S = 5.0
ID = re.compile(r"^[0-9a-f]{16}$")
FILES_MODULE = "hermes_cli.web_routers.files"

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
#: The CLI's progress line: name (padded to 36), bytes received, rate, and with a known size the percentage and ETA.
PROGRESS = re.compile(
    r"(?P<bytes>\d+\.\d{2})(?P<unit>[KMGT]i)?B\s+\d+\.\d{2}(?:[KMGT]i)?B/s(?:\s+(?P<percent>\d+\.\d{2})%\s+ETA\s+\S+)?\s*$"
)
UNITS = {None: 1, "Ki": 1 << 10, "Mi": 1 << 20, "Gi": 1 << 30, "Ti": 1 << 40}
#: The CLI's own advice when a send is denied, with its `$USER` placeholder.
OPERATOR_ADVICE = re.compile(r"use '([^'\n]*--operator=[^'\n]*)'", re.IGNORECASE)


class Refused(Exception):
    """A request this will not carry out, with the HTTP status that says why."""

    def __init__(self, status: int, detail: str, **extra: Any):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.extra = extra


# -- reading the CLI -------------------------------------------------------------------------------------------------


def parse_targets(text: str) -> list[dict]:
    """`tailscale file cp --targets`: one device per line, `<address>\\t<name>[\\t<detail>]`, where the detail is
    `offline` or `unknown-status`, maybe followed by `; last seen <duration> ago` (cmd/tailscale/cli/file.go). The CLI
    has no JSON form of this list. Lines whose first field is not an IP address are skipped."""
    targets = []

    for line in (text or "").splitlines():
        fields = line.rstrip("\r").split("\t") if "\t" in line else line.split(None, 2)
        fields = [part.strip() for part in fields]

        if len(fields) < 2 or not fields[1]:
            continue

        try:
            address = ipaddress.ip_address(fields[0])
        except ValueError:
            continue

        detail = fields[2] if len(fields) > 2 and fields[2] else None
        state = (detail or "").split(";", 1)[0].strip()
        online = False if state == "offline" else None if state == "unknown-status" else True
        targets.append({"name": printable(fields[1])[:128], "ip": str(address), "online": online, "detail": detail})

    return targets


def parse_whois(text: str) -> dict | None:
    """`tailscale whois --json <ip>`: `{Node: {Name, ComputedName, Addresses, Hostinfo: {OS, Hostname}}, UserProfile}`.
    Returns `{name, os, addresses}`, or None when it is not that."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None

    node = data.get("Node") if isinstance(data, dict) else None

    if not isinstance(node, dict):
        return None

    hostinfo = node.get("Hostinfo") if isinstance(node.get("Hostinfo"), dict) else {}
    name = node.get("ComputedName") or str(node.get("Name") or "").rstrip(".").split(".", 1)[0] or hostinfo.get("Hostname")
    addresses = [str(prefix).split("/", 1)[0] for prefix in node.get("Addresses") or [] if isinstance(prefix, str)]

    if not name:
        return None

    return {"name": printable(str(name))[:128], "os": hostinfo.get("OS") or None, "addresses": addresses}


def parse_progress(line: str) -> tuple[float | None, int | None]:
    """One repaint of the CLI's progress line: `(percent or None, bytes received or None)`."""
    match = PROGRESS.search(line)

    if not match:
        return None, None

    received = int(float(match.group("bytes")) * UNITS[match.group("unit")])
    percent = float(match.group("percent")) if match.group("percent") else None

    return percent, received


def operator_setup(text: str, user: str) -> dict | None:
    """When the CLI says the send was denied because this user may not send through tailscaled (only root and its
    operator may), the one-time setup it advises, with `$USER` filled in. The CLI ends a denied command with
    "Access denied: file access denied" and its advice to set this user as the operator (cmd/tailscale/cli/cli.go)."""
    if "access denied" not in (text or "").lower():
        return None

    advice = OPERATOR_ADVICE.search(text)
    command = advice.group(1).replace("$USER", user) if advice else None

    return {"needed": "operator", "user": user, "command": command}


def cli_message(text: str) -> str:
    """The CLI's own words without its progress repaints and terminal codes: what to show when it failed."""
    lines = []

    for chunk in re.split(r"[\r\n]+", ANSI.sub("", text or "")):
        chunk = chunk.strip()

        if chunk and not PROGRESS.search(chunk):
            lines.append(chunk)

    return "\n".join(lines)[-600:]


def printable(text: str) -> str:
    return "".join(character for character in str(text) if character.isprintable())


def clean_name(name: str | None, default: str) -> str:
    """The file name the device receives: a base name with no control characters, no leading dots or dashes, at most
    128 characters (Taildrop checks names too)."""
    base = printable(str(name or "").replace("\\", "/").rsplit("/", 1)[-1]).strip().lstrip(".-").strip()

    if len(base) > 128:
        stem, dot, extension = base.rpartition(".")
        base = (stem[: 127 - len(extension)] + "." + extension) if dot and len(extension) < 16 else base[:128]

    return base or default


def target_arg(ip: str) -> str:
    """`<ip>:`, or `[<ipv6>]:` as `tailscale file cp` wants an IPv6 address."""
    return f"[{ip}]:" if ipaddress.ip_address(ip).version == 6 else f"{ip}:"


def cp_argv(tailscale: str, fd: int, name: str, ip: str) -> list[str]:
    return [tailscale, "file", "cp", f"--name={name}", f"/dev/fd/{fd}", target_arg(ip)]


def max_bytes(environ) -> int:
    try:
        value = int(environ.get(MAX_BYTES_ENV, "") or MAX_BYTES)
    except ValueError:
        return MAX_BYTES

    return value if value > 0 else MAX_BYTES


# -- Hermes's own download check ---------------------------------------------------------------------------------------


def hermes_files():
    """Hermes's file routes module, if this Hermes has the download check this plugin reuses (0.21.5's). The dashboard
    imports all its routers at startup, so it is only looked up, never imported from here."""
    module = sys.modules.get(FILES_MODULE)
    has = callable(getattr(module, "_fs_download_path", None)) and callable(getattr(module, "_fs_regular_file", None))

    return module if has else None


async def check_file(files, path: str, profile: str | None = None, session_id: str | None = None) -> tuple[Path, os.stat_result]:
    """The file `GET /api/fs/download?path=&profile=&session_id=` would send, or Refused with that route's status and
    words. Exactly the route's own steps: `_fs_download_path` (the profile or the chat must exist; a relative path
    resolves against the chat's folder; symlinks resolved) then `_fs_regular_file` (a readable regular file, not a
    sensitive one). Hermes's `_is_sensitive_path` is asked again when it has one: it can only narrow, never widen."""
    if files is None:
        raise Refused(409, "This Hermes has no download check this plugin can reuse (it needs Hermes 0.21.5's), so it sends nothing.")

    try:
        candidate = await files._fs_download_path(path, profile, session_id)
        target, info = files._fs_regular_file(candidate)
        sensitive = getattr(files, "_is_sensitive_path", None)

        if callable(sensitive) and sensitive(Path(target)):
            raise Refused(403, "Access to sensitive files is not allowed")
    except Refused:
        raise
    except Exception as failure:
        status = getattr(failure, "status_code", None)

        if isinstance(status, int):
            raise Refused(status, str(getattr(failure, "detail", "") or failure))

        raise Refused(500, f"Hermes's download check failed: {failure}")

    return Path(target), info


def open_checked(target: Path, checked: os.stat_result) -> tuple[int, os.stat_result]:
    """Open the file Hermes checked, never through a link, and make sure it is still that file (device and inode)."""
    try:
        fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    except OSError as failure:
        if failure.errno in (errno.ELOOP, errno.EMLINK):
            raise Refused(409, "The file changed into a link after it was checked.")

        raise Refused(403 if failure.errno in (errno.EACCES, errno.EPERM) else 404, "File is not readable")

    info = os.fstat(fd)

    if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != (checked.st_dev, checked.st_ino):
        os.close(fd)
        raise Refused(409, "The file changed after it was checked.")

    return fd, info


# -- this computer -----------------------------------------------------------------------------------------------------


class System:
    """What Taildrop runs on this computer, in one place so the tests can stand in for it."""

    def __init__(self):
        self.environ = os.environ

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def run(self, argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=COMMAND_TIMEOUT_S)

    def user(self) -> str:
        try:
            return pwd.getpwuid(os.geteuid()).pw_name
        except KeyError:
            return str(os.geteuid())

    def openpty(self) -> tuple[int, int]:
        """A pseudo-terminal for the CLI's stderr, so it draws its progress line; a pipe if there is none."""
        try:
            import pty

            return pty.openpty()
        except (ImportError, OSError):
            return os.pipe()

    def popen(self, argv: list[str], *, stderr: int, pass_fds: tuple[int, ...]):
        return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=stderr, pass_fds=pass_fds, start_new_session=True)


# -- sends -------------------------------------------------------------------------------------------------------------


@dataclass
class Send:
    id: str
    target: dict
    name: str
    kind: str
    total: int
    fd: int
    started_at: float
    cleanup: Path | None = None
    state: str = "running"
    sent: int = 0
    error: str | None = None
    note: str | None = None
    setup: dict | None = None
    finished_at: float | None = None
    proc: Any = None
    cancel_requested: bool = False
    output: list[str] = field(default_factory=list)

    def view(self) -> dict:
        return {
            "id": self.id, "state": self.state, "target": self.target["name"], "name": self.name, "kind": self.kind,
            "sent_bytes": self.sent, "total_bytes": self.total, "error": self.error, "note": self.note, "setup": self.setup,
            "started_at": self.started_at, "finished_at": self.finished_at,
        }


class Taildrop:
    """The server's Taildrop sends for the app; one per dashboard process (the running sends live in memory)."""

    def __init__(self, folder: Path | None = None, *, system: System | None = None, files: Callable | None = None,
                 clock=time.time, start=None):
        self._folder = Path(folder) if folder else None
        self.system = system or System()
        self._files = files or hermes_files
        self._clock = clock
        self._start = start or (lambda target: threading.Thread(target=target, name="hermes-bot-taildrop", daemon=True).start())
        self._lock = threading.RLock()
        self._sends: dict[str, Send] = {}

    @property
    def folder(self) -> Path:
        return self._folder or store.default_dir()

    def _file(self) -> "store.JsonFile":
        return store.JsonFile(self.folder / "taildrop.json", list)

    # -- what can be sent where --------------------------------------------------------------------------------

    def _tailscale(self) -> str:
        tailscale = self.system.which("tailscale")

        if not tailscale:
            raise Refused(409, "Tailscale's command line (`tailscale`) is not installed on this server.")

        return tailscale

    def _connected(self, tailscale: str) -> dict:
        """`tailscale status --json`, refusing when tailscaled cannot be reached or is not connected."""
        try:
            result = self.system.run([tailscale, "status", "--json"])
        except (OSError, subprocess.SubprocessError) as failure:
            raise Refused(409, f"Tailscale did not answer: {failure}")

        try:
            status = json.loads(result.stdout or "")
        except ValueError:
            status = None

        if not isinstance(status, dict):
            raise Refused(409, f"Tailscale is not running on this server, or this user cannot reach it: {cli_message(result.stderr) or 'no answer'}")

        state = status.get("BackendState")

        if state != "Running":
            raise Refused(409, f"Tailscale is not connected on this server ({state or 'unknown state'}).")

        reason = (status.get("Self") or {}).get("NoFileSharingReason") if isinstance(status.get("Self"), dict) else None

        if reason:
            raise Refused(409, f"Taildrop is off for this server: {printable(reason)}")

        return status

    def targets(self) -> list[dict]:
        """The devices Taildrop offers right now (`tailscale file cp --targets`), after checking Tailscale is up."""
        tailscale = self._tailscale()
        self._connected(tailscale)

        try:
            result = self.system.run([tailscale, "file", "cp", "--targets"])
        except (OSError, subprocess.SubprocessError) as failure:
            raise Refused(409, f"Tailscale did not list its Taildrop targets: {failure}")

        if result.returncode != 0:
            raise Refused(409, f"Tailscale did not list its Taildrop targets: {cli_message(result.stderr) or 'no reason given'}")

        return parse_targets(result.stdout)

    def whois(self, client_ip: str | None) -> dict | None:
        """The requesting device, if it reached the dashboard over the tailnet (`tailscale whois --json`)."""
        try:
            address = ipaddress.ip_address(str(client_ip or "").split("%", 1)[0])
        except ValueError:
            return None

        # A dual-stack socket reports an IPv4 peer as ::ffff:a.b.c.d.
        ip = str(getattr(address, "ipv4_mapped", None) or address)

        tailscale = self.system.which("tailscale")

        if not tailscale:
            return None

        try:
            result = self.system.run([tailscale, "whois", "--json", ip])
        except (OSError, subprocess.SubprocessError):
            return None

        you = parse_whois(result.stdout) if result.returncode == 0 else None

        return {**you, "ip": ip} if you else None

    def status(self, client_ip: str | None = None) -> dict:
        """`{supported, why, targets, you, setup, max_bytes, sending}`."""
        with self._lock:
            sending = [send.id for send in self._sends.values() if send.state == "running"]

        answer = {"supported": False, "why": None, "targets": [], "you": None, "setup": self._last_setup(), "max_bytes": max_bytes(self.system.environ), "sending": sending}

        try:
            targets = self.targets()
        except Refused as refusal:
            return {**answer, "why": refusal.detail}

        you = self.whois(client_ip)
        mine = set((you or {}).get("addresses") or []) | ({you["ip"]} if you else set())

        for target in targets:
            target["you"] = target["ip"] in mine

        return {**answer, "supported": True, "targets": targets, "you": you and {"name": you["name"], "os": you["os"], "ip": you["ip"]}}

    async def check(self, path: str, profile: str | None = None, session_id: str | None = None) -> tuple[Path, os.stat_result]:
        return await check_file(self._files(), path, profile, session_id)

    # -- sending -----------------------------------------------------------------------------------------------

    def send(self, target: str, *, source: tuple[Path, os.stat_result] | None = None, diff: str | None = None, name: str | None = None) -> dict:
        """Start sending a checked file (`source`, from `check`) or a patch (`diff`) to `target`: `{id, ...}`."""
        if (source is None) == (diff is None):
            raise Refused(422, "Send either a file (`path`) or a patch (`diff`).")

        wanted = str(target or "").strip()
        device = next((t for t in self.targets() if wanted and (t["ip"] == wanted or t["name"].lower() == wanted.lower())), None)

        if device is None:
            raise Refused(422, "That is not one of this server's Taildrop targets.")

        limit = max_bytes(self.system.environ)
        fd = cleanup = None

        try:
            if diff is not None:
                data = diff.encode("utf-8")

                if len(data) > min(limit, MAX_DIFF_BYTES):
                    raise Refused(413, "The patch is too large to send.")

                # The patch goes in a private folder of its own, as <name>.patch, and is deleted once the CLI is done.
                stem = clean_name(name, "changes")
                sent_name = stem if stem.lower().endswith(".patch") else f"{stem}.patch"
                cleanup = Path(tempfile.mkdtemp(prefix="hermes-bot-taildrop-"))
                path = cleanup / sent_name
                path.write_bytes(data)
                os.chmod(path, 0o600)
                fd, info = open_checked(path, os.stat(path))
                kind = "diff"
            else:
                target_path, checked = source
                fd, info = open_checked(target_path, checked)
                sent_name = clean_name(name, target_path.name) if name else clean_name(target_path.name, "file")
                kind = "file"

            if info.st_size > limit:
                raise Refused(413, f"The file is {info.st_size} bytes; this server sends at most {limit}.")

            with self._lock:
                running = [send for send in self._sends.values() if send.state == "running"]

                if any(send.target["ip"] == device["ip"] for send in running):
                    raise Refused(409, f"A file is already on its way to {device['name']}.")

                if len(running) >= MAX_SENDING:
                    raise Refused(429, f"At most {MAX_SENDING} files are sent at a time.")

                send = Send(secrets.token_hex(8), device, sent_name, kind, info.st_size, fd, self._clock(), cleanup)
                self._sends[send.id] = send
                self._forget_old()
                self._log_start(send)
        except BaseException:
            if fd is not None:
                os.close(fd)

            if cleanup is not None:
                shutil.rmtree(cleanup, ignore_errors=True)

            raise

        accepted = send.view()
        self._start(lambda: self._run(send))

        return accepted

    def _run(self, send: Send) -> None:
        """The CLI, from start to exit, in the background. Everything is closed and a patch's folder deleted before the
        outcome is shown, so a finished send has left nothing behind."""
        master = slave = None
        code: int | None = -1

        try:
            tailscale = self._tailscale()
            master, slave = self.system.openpty()

            with self._lock:
                cancelled = send.cancel_requested

                if not cancelled:
                    send.proc = self.system.popen(cp_argv(tailscale, send.fd, send.name, send.target["ip"]), stderr=slave, pass_fds=(send.fd,))

            if cancelled:  # cancelled before the CLI started
                code = None
            else:
                # The CLI holds its own copies now; ours would keep the pseudo-terminal open after it exits.
                os.close(slave)
                slave = None
                os.close(send.fd)
                send.fd = -1
                self._read(send, master)
                code = send.proc.wait()
        except Exception as failure:
            log.exception("hermes-bot-mobile: taildrop %s failed", send.id)
            send.output.append(getattr(failure, "detail", None) or str(failure))
        finally:
            for fd in (slave, master, send.fd if send.fd >= 0 else None):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

            send.fd = -1

            if send.cleanup is not None:
                shutil.rmtree(send.cleanup, ignore_errors=True)

        self._finish(send, code)

    def _read(self, send: Send, master: int) -> None:
        """Follow the CLI's stderr until it closes: progress repaints move `sent`, anything else is kept as its words."""
        pending = ""

        while True:
            try:
                chunk = os.read(master, 4096)
            except OSError:  # EIO: the pseudo-terminal's other end closed with the CLI
                break

            if not chunk:
                break

            pending += ANSI.sub("", chunk.decode("utf-8", errors="replace"))
            *lines, pending = re.split(r"[\r\n]", pending)

            for line in lines:
                self._line(send, line)

        if pending:
            self._line(send, pending)

    def _line(self, send: Send, line: str) -> None:
        line = line.strip()

        if not line:
            return

        percent, received = parse_progress(line)

        if percent is not None or received is not None:
            progress = round(send.total * percent / 100) if percent is not None and send.total else received
            send.sent = max(send.sent, min(send.total, progress or 0))
        elif line.startswith("# warning:"):
            send.note = line[len("# warning:"):].strip()[:200]
        else:
            send.output.append(line)
            del send.output[:-20]

    def _finish(self, send: Send, code: int | None) -> None:
        """Log the outcome, then show it (so a send seen as finished is also in the log)."""
        message = cli_message("\n".join(send.output))
        error = setup = None

        if code == 0:
            state = "done"
        elif send.cancel_requested:
            state = "cancelled"
        else:
            state = "failed"
            error = message or f"tailscale file cp failed (exit {code})."
            setup = operator_setup(message, self.system.user())

        finished = self._clock()
        self._log_finish(send.id, {"state": state, "finished_at": finished, "error": error and error[:300], "setup": setup})

        with self._lock:
            send.state, send.finished_at, send.error, send.setup = state, finished, error, setup

            if state == "done":
                send.sent = send.total

    def progress(self, send_id: str) -> dict:
        """A send's state: `{state, sent_bytes, total_bytes, error, ...}`, from memory or, for an older one, the log."""
        if not ID.match(send_id or ""):
            raise Refused(404, "No such send.")

        with self._lock:
            send = self._sends.get(send_id)

            if send is not None:
                return send.view()

        row = next((row for row in self._file().read() if isinstance(row, dict) and row.get("id") == send_id), None)

        if row is None:
            raise Refused(404, "No such send.")

        state = row.get("state")

        if state == "running":  # a send of a dashboard process that is gone
            state, row = "failed", {**row, "error": "The dashboard restarted while the file was being sent."}

        return {
            "id": send_id, "state": state, "target": row.get("target"), "name": row.get("file"), "kind": row.get("kind"),
            "sent_bytes": row.get("bytes") if state == "done" else None, "total_bytes": row.get("bytes"),
            "error": row.get("error"), "note": None, "setup": row.get("setup"), "started_at": row.get("requested_at"),
            "finished_at": row.get("finished_at"),
        }

    def cancel(self, send_id: str) -> dict:
        """Stop a running send by terminating its CLI: `{cancelled: true, id}`; 404 unknown, 409 already finished."""
        with self._lock:
            send = self._sends.get(send_id) if ID.match(send_id or "") else None

            if send is None:
                raise Refused(404, "No such send.")

            if send.state != "running":
                raise Refused(409, f"That send has already {send.state}.")

            send.cancel_requested = True
            proc = send.proc

        if proc is not None:
            try:
                proc.terminate()
            except OSError:
                pass

            timer = threading.Timer(KILL_AFTER_S, lambda: proc.poll() is None and proc.kill())
            timer.daemon = True
            timer.start()

        return {"cancelled": True, "id": send_id}

    # -- the log -----------------------------------------------------------------------------------------------

    def _forget_old(self) -> None:
        finished = [send for send in self._sends.values() if send.state != "running"]

        for send in sorted(finished, key=lambda s: s.started_at)[: max(0, len(self._sends) - KEEP)]:
            self._sends.pop(send.id, None)

    def _log_start(self, send: Send) -> None:
        row = {"id": send.id, "requested_at": send.started_at, "target": send.target["name"], "file": send.name, "kind": send.kind, "bytes": send.total, "state": "running"}

        def change(rows: list) -> None:
            rows.append(row)
            rows[:] = rows[-KEEP:]

        self._file().update(change)

    def _log_finish(self, send_id: str, fields: dict) -> None:
        def change(rows: list) -> None:
            for row in rows:
                if isinstance(row, dict) and row.get("id") == send_id:
                    row.update(fields)

        self._file().update(change)

    def _last_setup(self) -> dict | None:
        """The operator setup the last finished send ran into, until a send gets through."""
        for row in reversed(self._file().read()):
            if isinstance(row, dict) and row.get("state") in ("done", "failed"):
                return row.get("setup") if row.get("state") == "failed" else None

        return None
