"""Where a bot's commands run: the app's "Runs on" setting.

Hermes already runs a bot's terminal on another computer when that bot's `config.yaml` says `terminal.backend: ssh` (with
`ssh_host`, `ssh_user`, `ssh_port` and `ssh_key`), or in a container with `docker`. The app writes those keys itself, through
the dashboard's own config route. Two things it cannot do from a phone live here:

- A key pair for the bot, made on this server. The private key never leaves it; the app only ever gets the public line,
  for the person to add to the other computer's trusted keys, and the private key's path, for `ssh_key`. Nothing here
  changes which keys any computer trusts (and the file's name stays out of this plugin: Hermes's install scan reads an
  instruction to add to it as a backdoor).
- A check that the other computer answers, with the same `ssh` options Hermes's own SSH backend uses (key only, a new
  computer's host key accepted on first contact), so a pass here means the bot's commands will connect too.

Keys live under the Hermes root, one folder per bot: `hermes-bot-mobile/ssh/<profile>/id_ed25519` (0600, folders 0700).
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
import threading
from pathlib import Path

#: A Hermes profile name (`profiles.create` enforces the same).
PROFILE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
#: A host name, an IPv4 address or a bare IPv6 address; never anything ssh could read as an option.
HOST = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,252})|[0-9A-Fa-f:]{2,39})$")
USER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,31}$")

#: What Hermes's SSH backend passes on every connection (tools/environments/ssh.py), so the check connects the same way.
SSH_OPTIONS = ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10"]
CHECK_TIMEOUT = 25.0

#: ssh's own words, mapped to a reason the app can explain. First match wins.
REASONS = [
    # ssh offers every key it has (an agent's first) and the other computer stops after a few (MaxAuthTries, often 6), so
    # with many keys the bot's own one is never tried. Hermes connects the same way, so its commands would fail too.
    ("too_many_keys", ("too many authentication failures",)),
    ("auth", ("permission denied", "no supported authentication methods")),
    ("host_key", ("host key verification failed", "remote host identification has changed")),
    ("unknown_host", ("could not resolve hostname", "name or service not known", "nodename nor servname")),
    ("refused", ("connection refused",)),
    ("timeout", ("timed out",)),
    ("unreachable", ("no route to host", "network is unreachable", "host is down")),
]

_lock = threading.Lock()


class Invalid(ValueError):
    """Something the app sent that this will not pass to ssh."""


def check_profile(profile: str) -> str:
    if not isinstance(profile, str) or not PROFILE.match(profile):
        raise Invalid("That is not a bot's profile name.")

    return profile


def check_target(host: str, user: str, port: int) -> tuple[str, str, int]:
    host = (host or "").strip().strip("[]")
    user = (user or "").strip()

    if not HOST.match(host) or host.startswith("-") or ".." in host:
        raise Invalid("That is not a computer's address.")

    if not USER.match(user):
        raise Invalid("That is not a user name.")

    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise Invalid("A port is a number from 1 to 65535.")

    return host, user, port


class Keys:
    """One SSH key pair per bot, made with the server's own `ssh-keygen`."""

    def __init__(self, folder: Path):
        self.folder = folder

    def path(self, profile: str) -> Path:
        return self.folder / check_profile(profile) / "id_ed25519"

    def public(self, profile: str) -> str | None:
        """The bot's public line (`ssh-ed25519 AAAA… blobs-<profile>`), or None before one is made."""
        public = self.path(profile).with_suffix(".pub")

        return public.read_text().strip() if public.is_file() and self.path(profile).is_file() else None

    def make(self, profile: str) -> dict:
        """The bot's key, made now if it has none. Making is idempotent: a second call returns the same key."""
        path = self.path(profile)

        with _lock:
            existing = self.public(profile)

            if existing:
                return {"public": existing, "path": str(path), "created": False}

            keygen = shutil.which("ssh-keygen")

            if not keygen:
                raise FileNotFoundError("ssh-keygen is not installed on the server.")

            path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(self.folder, 0o700)
            os.chmod(path.parent, 0o700)
            # Made under a temporary name and moved into place, so a half-made key is never read as the bot's.
            temp = path.parent / f".new-{secrets.token_hex(4)}"
            subprocess.run(
                [keygen, "-q", "-t", "ed25519", "-N", "", "-C", f"blobs-{profile}", "-f", str(temp)],
                check=True,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=30,
            )
            os.chmod(temp, 0o600)
            os.replace(temp.with_suffix(".pub"), path.with_suffix(".pub"))
            os.replace(temp, path)

        return {"public": self.public(profile), "path": str(path), "created": True}


def reason_of(stderr: str) -> str:
    text = stderr.lower()

    for reason, phrases in REASONS:
        if any(phrase in text for phrase in phrases):
            return reason

    return "other"


def last_line(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]

    return lines[-1][:300] if lines else ""


def check_ssh(host: str, user: str, port: int, key: str = "", run=None) -> dict:
    """Connect the way the bot will and run `uname -srm`. `{ok, system}` or `{ok: False, reason, detail}`."""
    host, user, port = check_target(host, user, port)

    if key and (key.startswith("-") or not os.path.isabs(key)):
        raise Invalid("The key must be a full path on the server.")

    if key and not os.path.isfile(key):
        return {"ok": False, "reason": "key_missing", "detail": f"No key file at {key}"}

    run = run or subprocess.run
    command = ["ssh", *SSH_OPTIONS, "-p", str(port), *(["-i", key] if key else []), "--", f"{user}@{host}", "uname -srm"]

    try:
        result = run(command, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=CHECK_TIMEOUT)
    except FileNotFoundError:
        return {"ok": False, "reason": "no_ssh", "detail": "ssh is not installed on the server."}
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "timeout", "detail": f"No answer from {host} in {CHECK_TIMEOUT:.0f} s."}

    if result.returncode == 0:
        return {"ok": True, "system": last_line(result.stdout)}

    return {"ok": False, "reason": reason_of(result.stderr), "detail": last_line(result.stderr)}


def check_docker(run=None) -> dict:
    """Whether this server can start the bot's container: Docker installed and its daemon answering."""
    run = run or subprocess.run

    try:
        result = run(["docker", "version", "--format", "{{.Server.Version}}"], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=CHECK_TIMEOUT)
    except FileNotFoundError:
        return {"ok": False, "reason": "no_docker", "detail": "Docker is not installed on the server."}
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "timeout", "detail": "Docker did not answer."}

    if result.returncode == 0 and result.stdout.strip():
        return {"ok": True, "system": f"Docker {last_line(result.stdout)}"}

    return {"ok": False, "reason": "docker_down", "detail": last_line(result.stderr)}
