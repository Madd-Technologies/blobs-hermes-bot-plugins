"""Restart this Hermes dashboard (and, if asked, the messaging gateway) from the app.

The app talks to `hermes dashboard`, which on a Linux server usually runs as a systemd unit of its own (a `--user` unit
such as `hermes-dashboard.service`). Restarting it from inside is the hard part: anything the dashboard starts runs in
its unit's cgroup, and systemd stops every process in that cgroup when the unit stops, so a plain
`systemctl --user restart` run from here would be killed halfway through. A bot's own shell has the same problem.

So the restart is handed to systemd itself: `systemd-run --user --on-active=2` makes a transient timer and service
*outside* this cgroup, and that service runs `systemctl --user restart <this unit>` two seconds later, after the app has
had its answer. Only this process's own unit is ever restarted (read from its cgroup, never from the request), and
only through argv lists, never a shell.

The gateway goes through Hermes's own restart (the function behind `POST /api/gateway/restart`), first, and the
dashboard is restarted only after that has finished, because `hermes gateway restart` runs inside this cgroup too and
waits while the gateway lets its turns finish. See the README.

Requests and their outcomes are kept in `restarts.json` (the last 20) under the plugin's data folder, so the dashboard
that comes back can say when it was restarted. Off systemd (macOS, a container, a terminal) it says why it cannot.
"""

from __future__ import annotations

import logging
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
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

MODES = ("now", "idle")
#: One restart a minute: a request that restarted something (or is restarting it) holds the next one back this long.
RATE_LIMIT_S = 60
IDLE_TIMEOUT_S = 600
IDLE_TIMEOUT_MIN, IDLE_TIMEOUT_MAX = 30, 1800
#: How often an idle restart looks at what is running.
POLL_S = 3.0
#: The transient timer's delay, so the app gets its answer before this dashboard stops.
DELAY_S = 2
#: The longest an idle-then-gateway restart waits for `hermes gateway restart` (it waits for the gateway's own drain).
GATEWAY_WAIT_S = 1800.0
#: How long the "can this be restarted" answer (a `systemctl show`) is reused.
PROBE_TTL_S = 30.0
#: A restart scheduled this long ago, while the same dashboard still answers, did not happen.
NOT_RESTARTED_AFTER_S = 120.0
KEEP = 20
COMMAND_TIMEOUT_S = 15

#: A systemd service name as this plugin will pass it on (escapes like `\x2d` and template instances allowed).
UNIT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9:_.@\\-]{0,240}\.service$")
USER_MANAGER = re.compile(r"^user@(\d+)\.service$")
CONTAINER = re.compile(r"docker|containerd|libpod|podman|kubepods|lxc\.payload|/lxc/")
#: Outcomes of a request that is still being carried out by the process that took it.
UNDER_WAY = ("waiting", "restarting_gateway", "scheduling")
TRANSIENT_PREFIX = "hermes-bot-restart-"


class Refused(Exception):
    """A request this will not carry out, with the HTTP status that says why (409, 422 or 429; 500 if systemd failed)."""

    def __init__(self, status: int, detail: str, **extra: Any):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.extra = extra


# -- where this process runs ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Placement:
    """The systemd service a process runs in, read from its cgroup, or why it is not in one."""

    unit: str | None = None
    #: "user" (a unit of a user's own systemd, restarted with `--user`) or "system".
    scope: str | None = None
    #: The user whose systemd runs a `--user` unit (from `user@<uid>.service`).
    uid: int | None = None
    why: str | None = None


def cgroup_path(text: str) -> str | None:
    """The cgroup path systemd tracks a process by: the unified (`0::`) line, else cgroup v1's `name=systemd` line."""
    unified = legacy = None

    for line in (text or "").splitlines():
        parts = line.strip().split(":", 2)

        if len(parts) != 3:
            continue

        hierarchy, controllers, path = parts

        if hierarchy == "0" and controllers == "":
            unified = path
        elif "name=systemd" in controllers.split(","):
            legacy = path

    if unified not in (None, "", "/"):
        return unified

    return legacy if legacy is not None else unified


def place(text: str | None, in_container: bool = False) -> Placement:
    """Which systemd service the process with this `cgroup` file runs in.

    The service is the deepest `*.service` in the path. One under a `user@<uid>.service` is that user's own (`--user`)
    unit; any other is a system unit. A login session, a desktop app's scope, the user manager itself, a container or
    no cgroup at all is not a service of its own, and the answer says which.
    """
    path = cgroup_path(text or "")

    if path is None:
        return Placement(why="This dashboard has no systemd cgroup, so systemd does not run it.")

    segments = [segment for segment in path.split("/") if segment]
    uid = unit = scope = None

    for segment in segments:
        manager = USER_MANAGER.match(segment)

        if manager:
            uid = int(manager.group(1))
        elif segment.endswith(".service"):
            unit, scope = segment, "user" if uid is not None else "system"

    if unit:
        if not UNIT.match(unit):
            return Placement(scope=scope, uid=uid, why="This dashboard's systemd unit has a name this plugin will not pass on.")

        return Placement(unit=unit, scope=scope, uid=uid)

    leaf = segments[-1] if segments else ""

    if in_container or CONTAINER.search(path):
        why = "This dashboard runs in a container, not as a systemd service. Restart the container instead."
    elif leaf.startswith("session-") and leaf.endswith(".scope"):
        why = "This dashboard was started from a login session (a terminal), not as a systemd service. Restart it where it was started."
    elif not segments:
        why = "This dashboard is not in a systemd service (its cgroup is the root one)."
    else:
        why = f"This dashboard runs in {leaf}, not in a systemd service of its own. Restart it where it was started."

    return Placement(uid=uid, why=why)


def proc_start(stat: str, boot_time: float, ticks_per_second: float) -> float:
    """A process's start time (epoch seconds) from its `/proc/<pid>/stat` line, the boot time and the clock rate.

    Field 22 is the start in clock ticks after boot; the command name (field 2) may hold spaces and brackets, so the
    fields are counted from the last `)`.
    """
    fields = stat[stat.rindex(")") + 2:].split()

    return boot_time + int(fields[19]) / ticks_per_second


def schedule_argv(systemd_run: str, systemctl: str, unit: str, scope: str, stamp: int) -> list[str]:
    """The `systemd-run` call that restarts `unit` from outside its cgroup, `DELAY_S` seconds from now.

    `--collect` and `RemainAfterElapse=no` let systemd forget the transient timer and service once they have run.
    """
    user = ["--user"] if scope == "user" else []

    return [
        systemd_run, *user, "--collect", f"--unit={TRANSIENT_PREFIX}{stamp}", f"--on-active={DELAY_S}",
        "--timer-property=RemainAfterElapse=no", f"--description=hermes-bot-mobile: restart {unit}",
        "--", systemctl, *user, "restart", unit,
    ]


def probe_argv(systemctl: str, unit: str, scope: str) -> list[str]:
    """A read-only `systemctl show` that proves this user can reach the unit's manager and the unit is loaded."""
    user = ["--user"] if scope == "user" else []

    return [systemctl, *user, "show", "--property=Id", "--property=LoadState", "--property=ActiveState", "--", unit]


def gateway_cli_argv() -> list[str]:
    """`hermes gateway restart` the way Hermes's dashboard runs it, for a Hermes without its in-process spawner."""
    return [sys.executable, "-m", "hermes_cli.main", "gateway", "restart"]


def gateway_cli_env(environ) -> dict[str, str]:
    """The dashboard's environment as Hermes hands it to its own `gateway restart` child: non-interactive, and without
    `_HERMES_GATEWAY`, which makes the CLI refuse to restart (its guard against a gateway restarting itself in a loop)."""
    env = {key: value for key, value in environ.items() if key != "_HERMES_GATEWAY"}
    env["HERMES_NONINTERACTIVE"] = "1"

    return env


def last_line(text: str | None) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]

    return lines[-1][:300] if lines else ""


# -- what is running -------------------------------------------------------------------------------------------------


def live_sessions(server, titles: bool = False) -> list[dict] | None:
    """The chats this dashboard process is running a turn for right now, from Hermes's own registry of live sessions
    (`tui_gateway.server._sessions`, what `session.active_list` lists). None when there is no registry to read.

    Read only: the registry is copied under its own lock and nothing in it is changed. A session counts while its turn
    runs (`running`), including one waiting for the person's OK (`status: waiting`).
    """
    registry = getattr(server, "_sessions", None)

    if not isinstance(registry, dict):
        return None

    lock = getattr(server, "_sessions_lock", None)

    try:
        if lock is not None:
            with lock:
                items = list(registry.items())
        else:
            items = list(registry.items())
    except Exception:
        return None

    status_of = getattr(server, "_session_live_status", None)
    key_of = getattr(server, "_session_lookup_key", None)
    title_of = getattr(server, "_session_live_title", None)
    rows = []

    for sid, session in items:
        if not isinstance(session, dict) or session.get("_finalized") or not session.get("running"):
            continue

        status = "working"
        key = str(session.get("session_key") or sid)

        try:
            status = str(status_of(sid, session)) if callable(status_of) else status
        except Exception:
            pass

        try:
            key = str(key_of(session, fallback=sid)) if callable(key_of) else key
        except Exception:
            pass

        title = str(session.get("pending_title") or "").strip()

        if titles and callable(title_of):
            try:
                title = str(title_of(session, key) or title)
            except Exception:
                pass

        rows.append({"id": str(sid), "session": key, "title": title, "profile": _profile_name(session.get("profile_home")), "status": status})

    return rows


def _profile_name(home) -> str | None:
    try:
        from hermes_constants import get_process_hermes_home, profile_name_for_home
    except Exception:
        return None

    try:
        return profile_name_for_home(home or get_process_hermes_home())
    except Exception:
        return None


# -- this computer ---------------------------------------------------------------------------------------------------


class System:
    """Everything the restarter reads from or runs on this computer, in one place so the tests can stand in for it."""

    PROC = Path("/proc")

    def __init__(self):
        self.platform = sys.platform
        self.environ = os.environ

    def pid(self) -> int:
        return os.getpid()

    def uid(self) -> int:
        return os.getuid()

    def euid(self) -> int:
        return os.geteuid()

    def cgroup(self, pid: int | str = "self") -> str | None:
        try:
            return (self.PROC / str(pid) / "cgroup").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def systemd_running(self) -> bool:
        """systemd's own test for "booted with systemd" (sd_booted)."""
        return os.path.isdir("/run/systemd/system")

    def in_container(self) -> bool:
        return os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")

    def started_at(self, pid: int | str = "self") -> float | None:
        """When a process started (epoch seconds), so the app can tell a restarted dashboard from the old one."""
        try:
            stat = (self.PROC / str(pid) / "stat").read_text()
            boot = next(float(line.split()[1]) for line in (self.PROC / "stat").read_text().splitlines() if line.startswith("btime "))

            return round(proc_start(stat, boot, os.sysconf("SC_CLK_TCK")), 2)
        except Exception:
            pass

        try:
            import psutil

            return round(psutil.Process(None if pid == "self" else int(pid)).create_time(), 2)
        except Exception:
            return None

    def alive(self, pid, started_at) -> bool:
        """Whether that process (the same one, not a later process given its pid) is still running."""
        if not isinstance(pid, int) or pid <= 0:
            return False

        now = self.started_at(pid)

        if now is not None and started_at is not None:
            return abs(now - float(started_at)) < 2

        try:
            os.kill(pid, 0)
        except PermissionError:
            return True
        except OSError:
            return False

        return True

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def run(self, argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=COMMAND_TIMEOUT_S)

    def spawn(self, argv: list[str], log_path: Path, env: dict) -> subprocess.Popen:
        """Start a detached child the way Hermes starts its own dashboard actions, its output appended to `log_path`."""
        log_path.parent.mkdir(parents=True, exist_ok=True)

        with open(log_path, "ab", buffering=0) as out:
            out.write(f"\n=== gateway restart started {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n".encode())

            return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, env=env, start_new_session=True)

    def sessions(self, titles: bool = False) -> list[dict] | None:
        # Only a registry the dashboard already loaded: importing tui_gateway.server here would start it up.
        return live_sessions(sys.modules.get("tui_gateway.server"), titles)

    def gateway_pid(self) -> int | None:
        """The running gateway's pid for this Hermes home, or None. Asked without Hermes's stale-file cleanup."""
        try:
            from gateway.status import get_running_pid

            pid = get_running_pid(cleanup_stale=False)
        except Exception:
            return None

        return pid if isinstance(pid, int) and pid > 0 else None

    def gateway_route(self) -> bool:
        """Whether this Hermes serves `POST /api/gateway/restart` (0.21.5 does)."""
        app = getattr(sys.modules.get("hermes_cli.web_server"), "app", None)

        for route in getattr(app, "routes", None) or []:
            if getattr(route, "path", None) == "/api/gateway/restart" and "POST" in (getattr(route, "methods", None) or ()):
                return True

        return False

    def gateway_spawner(self) -> Callable | None:
        """Hermes's own gateway restart (`hermes_cli.web_server._spawn_gateway_restart`), when the dashboard has it."""
        spawn = getattr(sys.modules.get("hermes_cli.web_server"), "_spawn_gateway_restart", None)

        return spawn if callable(spawn) else None


# -- the restarter ---------------------------------------------------------------------------------------------------


@dataclass
class Job:
    """A request still being carried out: waiting for the chats to finish, restarting the gateway, or scheduling."""

    id: str
    mode: str
    include_gateway: bool
    deadline: float | None
    stage: str
    cancelled: threading.Event = field(default_factory=threading.Event)


def settle(entry: dict, me: dict, now: float, alive: Callable[[Any, Any], bool]) -> dict:
    """What became of a logged request, as far as this process can tell (a copy; the log is not changed here).

    A restart was scheduled and the process that scheduled it is gone, while this one started after: `done`. Scheduled
    long ago and the same process still answers: `not_restarted`. Still under way in a process that is gone:
    `interrupted`.
    """
    entry = dict(entry)
    by = entry.get("by") or {}
    mine = by.get("pid") == me.get("pid") and by.get("started_at") == me.get("started_at")
    outcome = entry.get("outcome")

    if outcome == "scheduled":
        scheduled = float(entry.get("scheduled_at") or 0)

        if mine and now - scheduled > NOT_RESTARTED_AFTER_S:
            entry.update(outcome="not_restarted", detail=f"systemd was asked to restart this dashboard, but it is still the same process. See `journalctl --user -u {entry.get('transient_unit') or TRANSIENT_PREFIX + '*'}`.")
        elif not mine and me.get("started_at") is not None and float(me["started_at"]) >= scheduled - 1 and not alive(by.get("pid"), by.get("started_at")):
            entry.update(outcome="done", restarted_at=me["started_at"])
    elif outcome in UNDER_WAY and not mine and not alive(by.get("pid"), by.get("started_at")):
        entry.update(outcome="interrupted", detail="The dashboard stopped before this request was carried out.")

    return entry


def public(entry: dict | None) -> dict | None:
    return None if entry is None else {key: value for key, value in entry.items() if key != "by"}


class Restarter:
    """Takes the app's restart requests for this dashboard; one per process (the pending request lives in memory)."""

    def __init__(self, folder: Path | None = None, *, system: System | None = None, clock=time.time, wait=None, start=None):
        #: Worked out on first use (the data folder is the store's), so loading the plugin touches nothing.
        self._folder = Path(folder) if folder else None
        self.system = system or System()
        self._clock = clock
        self._wait = wait or (lambda event, seconds: event.wait(seconds))
        self._start = start or (lambda target: threading.Thread(target=target, name="hermes-bot-restart", daemon=True).start())
        self._lock = threading.RLock()
        self._job: Job | None = None
        self._support: tuple[float, dict] | None = None
        self._me = {"pid": self.system.pid(), "started_at": self.system.started_at()}

    @property
    def folder(self) -> Path:
        return self._folder or store.default_dir()

    def _file(self) -> "store.JsonFile":
        return store.JsonFile(self.folder / "restarts.json", list)

    # -- can it be restarted -----------------------------------------------------------------------------------

    def support(self) -> dict:
        """`{supported, why, unit, scope, gateway_same_unit}`, reused for `PROBE_TTL_S`."""
        now = self._clock()

        with self._lock:
            if self._support is not None and now - self._support[0] < PROBE_TTL_S:
                return self._support[1]

        answer = self._check()

        with self._lock:
            self._support = (now, answer)

        return answer

    def _check(self) -> dict:
        system = self.system
        answer = {"supported": False, "why": None, "unit": None, "scope": None, "gateway_same_unit": None}

        if system.platform == "darwin":
            return {**answer, "why": "Restarting from the app needs systemd, which macOS does not have: Hermes runs under launchd or in a terminal here. Restart it there."}

        if not system.platform.startswith("linux"):
            return {**answer, "why": "Restarting from the app needs systemd (Linux)."}

        text = system.cgroup()

        if text is None:
            return {**answer, "why": "This dashboard cannot read its own cgroup, so it cannot tell which systemd service it runs in."}

        placement = place(text, system.in_container())
        answer.update(unit=placement.unit, scope=placement.scope)

        if placement.why:
            return {**answer, "why": placement.why}

        if not system.systemd_running():
            return {**answer, "why": "This computer does not run systemd."}

        # Importing Hermes's gateway modules marks a process with _HERMES_GATEWAY=1, and a standalone `hermes dashboard`
        # imports some of them (0.21.6), so the marker alone does not make this dashboard the gateway: only the running
        # gateway's own pid being this process does.
        if system.environ.get("_HERMES_GATEWAY") == "1" and system.gateway_pid() == system.pid():
            return {**answer, "why": "This dashboard runs inside the messaging gateway's process. Restart the gateway instead (POST /api/gateway/restart)."}

        if placement.scope == "user" and placement.uid != system.uid():
            return {**answer, "why": f"{placement.unit} belongs to another user's systemd, not this dashboard's user."}

        if placement.scope == "system" and system.euid() != 0:
            return {**answer, "why": f"This dashboard runs as the system service {placement.unit}. Restarting a system service needs root, which this dashboard does not have: restart it on the server with systemctl."}

        systemd_run, systemctl = system.which("systemd-run"), system.which("systemctl")

        if not systemd_run or not systemctl:
            return {**answer, "why": "systemd-run or systemctl is not installed on this server."}

        same = self._gateway_shares(placement)
        answer["gateway_same_unit"] = same

        if same:
            return {**answer, "why": f"The messaging gateway runs in the same systemd service ({placement.unit}), so restarting it from here would stop the gateway without letting it finish its turns. Restart the gateway with its own restart instead."}

        try:
            result = system.run(probe_argv(systemctl, placement.unit, placement.scope))
        except (OSError, subprocess.SubprocessError) as failure:
            return {**answer, "why": f"systemctl could not be asked about {placement.unit}: {failure}"}

        shown = dict(line.split("=", 1) for line in (result.stdout or "").splitlines() if "=" in line)

        if result.returncode != 0 or shown.get("Id") != placement.unit or shown.get("LoadState") != "loaded":
            reason = last_line(result.stderr) or f"LoadState={shown.get('LoadState') or 'unknown'}"

            return {**answer, "why": f"systemctl{' --user' if placement.scope == 'user' else ''} cannot reach {placement.unit} ({reason})."}

        return {**answer, "supported": True}

    def _gateway_shares(self, placement: Placement) -> bool | None:
        pid = self.system.gateway_pid()

        if pid is None or pid == self.system.pid():
            return None

        text = self.system.cgroup(pid)

        return None if text is None else place(text).unit == placement.unit

    # -- the status the app shows ------------------------------------------------------------------------------

    def _rows(self, now: float) -> list[dict]:
        return [settle(row, self._me, now, self.system.alive) for row in self._file().read() if isinstance(row, dict)]

    def status(self) -> dict:
        support = self.support()
        now = self._clock()
        rows = self._rows(now)
        last = next((row for row in reversed(rows) if row.get("outcome") != "refused"), None)

        return {
            "supported": support["supported"],
            "why": support["why"],
            "unit": support["unit"],
            "scope": support["scope"],
            "pid": self._me["pid"],
            "started_at": self._me["started_at"],
            "now": now,
            "busy": self.system.sessions(titles=True),
            "gateway": {
                "route": self.system.gateway_route(),
                "restart": "hermes" if self.system.gateway_spawner() else "cli",
                "same_unit": support.get("gateway_same_unit"),
            },
            "last": public(last),
            "retry_after": retry_after(rows, now),
        }

    # -- requests ----------------------------------------------------------------------------------------------

    def request(self, when: str, idle_timeout_s: int = IDLE_TIMEOUT_S, include_gateway: bool = False) -> dict:
        """Take a restart request: `{accepted, id, mode, unit, include_gateway}`, or raise Refused."""
        if when not in MODES:
            raise Refused(422, "`when` is now or idle.")

        if isinstance(idle_timeout_s, bool) or not isinstance(idle_timeout_s, int) or not IDLE_TIMEOUT_MIN <= idle_timeout_s <= IDLE_TIMEOUT_MAX:
            raise Refused(422, f"`idle_timeout_s` is {IDLE_TIMEOUT_MIN} to {IDLE_TIMEOUT_MAX} seconds.")

        support = self.support()
        now = self._clock()
        entry = {
            "id": f"{int(now)}-{secrets.token_hex(3)}",
            "requested_at": now,
            "mode": when,
            "include_gateway": bool(include_gateway),
            "unit": support["unit"],
            "scope": support["scope"],
            "by": dict(self._me),
        }

        if when == "idle":
            entry.update(idle_timeout_s=idle_timeout_s, deadline=now + idle_timeout_s)

        with self._lock:
            try:
                self._admit(when, support, now)
            except Refused as refusal:
                self._refused(entry, refusal)
                raise

            replaced = self._job

            if replaced is not None:  # only a request still waiting for idle gets here
                replaced.cancelled.set()
                self._job = None
                self._update(replaced.id, outcome="replaced", finished_at=now, detail=f"Replaced by a newer request ({when}).")

            accepted = {"accepted": True, "id": entry["id"], "mode": when, "unit": support["unit"], "include_gateway": bool(include_gateway)}

            if when == "now" and not include_gateway:
                self._append({**entry, "outcome": "scheduling"})
                ok, detail = self._schedule(entry["id"], support)

                if not ok:
                    raise Refused(500, detail)

                return accepted

            job = Job(entry["id"], when, bool(include_gateway), entry.get("deadline"), "waiting" if when == "idle" else "restarting_gateway")
            self._append({**entry, "outcome": job.stage})
            self._job = job

        self._start(lambda: self._carry_out(job, support))

        return accepted

    def _admit(self, when: str, support: dict, now: float) -> None:
        if not support["supported"]:
            raise Refused(409, support["why"] or "This dashboard cannot be restarted from here.")

        if when == "idle" and self.system.sessions() is None:
            raise Refused(409, "This dashboard cannot see which chats are running, so it cannot wait for them. Restart now instead.")

        if self._job is not None and self._job.stage != "waiting":
            raise Refused(409, "A restart is already under way.")

        wait = retry_after(self._rows(now), now)

        if wait:
            raise Refused(429, f"One restart a minute: try again in {wait} s.", retry_after=wait)

    def cancel(self) -> dict:
        """Cancel the restart waiting for the chats to finish: `{cancelled: true, id}`, or Refused 404 / 409."""
        with self._lock:
            job = self._job

            if job is None:
                raise Refused(404, "No restart is waiting.")

            if job.stage != "waiting":
                raise Refused(409, "The restart is already under way and cannot be cancelled.")

            job.cancelled.set()
            self._job = None
            self._update(job.id, outcome="cancelled", finished_at=self._clock())

        return {"cancelled": True, "id": job.id}

    # -- carrying a request out --------------------------------------------------------------------------------

    def _carry_out(self, job: Job, support: dict) -> None:
        try:
            if job.mode == "idle" and not self._wait_for_idle(job):
                return

            if job.include_gateway:
                self._restart_gateway(job)

            with self._lock:
                job.stage = "scheduling"

            self._schedule(job.id, support)
        except Exception as failure:  # never let a background thread die silently
            log.exception("hermes-bot-mobile: restart %s failed", job.id)
            self._update(job.id, outcome="failed", finished_at=self._clock(), detail=str(failure)[:300])
        finally:
            with self._lock:
                if self._job is job:
                    self._job = None

    def _wait_for_idle(self, job: Job) -> bool:
        """Wait until no chat is running a turn, then claim the job (True); False once cancelled, replaced or timed out."""
        while True:
            with self._lock:
                if job.cancelled.is_set() or self._job is not job:
                    return False

            busy = self.system.sessions()
            now = self._clock()

            with self._lock:
                if job.cancelled.is_set() or self._job is not job:
                    return False

                if busy is not None and not busy:
                    job.stage = "restarting_gateway" if job.include_gateway else "scheduling"
                    self._update(job.id, outcome=job.stage, idle_at=now)

                    return True

                if busy is None or now >= job.deadline:
                    self._job = None
                    count = len(busy or [])
                    detail = f"Timed out with {count} still busy." if busy is not None else "Lost sight of the running chats."
                    self._update(job.id, outcome="timed_out" if busy is not None else "failed", busy_left=count, finished_at=now, detail=detail)

                    return False

            if self._wait(job.cancelled, max(0.0, min(POLL_S, job.deadline - now))):
                return False

    def _restart_gateway(self, job: Job) -> None:
        """Hermes's own gateway restart, waited for: it runs in this cgroup, so the dashboard must outlive it."""
        now = self._clock()
        self._update(job.id, outcome="restarting_gateway", carried_out_at=now, gateway={"started_at": now})
        spawner = self.system.gateway_spawner()

        try:
            if spawner is not None:
                proc, reused = spawner(None)
                how = "hermes"
            else:
                proc = self.system.spawn(gateway_cli_argv(), self.folder / "gateway-restart.log", gateway_cli_env(self.system.environ))
                reused, how = False, "cli"
        except Exception as failure:
            self._update(job.id, gateway={"started_at": now, "outcome": "failed", "detail": str(failure)[:300]})

            return

        try:
            code = proc.wait(timeout=GATEWAY_WAIT_S)
        except subprocess.TimeoutExpired:
            code = None

        self._update(job.id, gateway={
            "started_at": now, "how": how, "reused": bool(reused), "pid": getattr(proc, "pid", None),
            "outcome": "exited" if code is not None else "still_running", "exit_code": code, "finished_at": self._clock(),
        })

    def _schedule(self, entry_id: str, support: dict) -> tuple[bool, str]:
        """Ask systemd to restart this dashboard's unit `DELAY_S` seconds from now, from outside its cgroup."""
        now = self._clock()
        systemd_run, systemctl = self.system.which("systemd-run"), self.system.which("systemctl")

        if not systemd_run or not systemctl:
            detail = "systemd-run or systemctl is not installed on this server."
            self._update(entry_id, outcome="failed", finished_at=now, detail=detail)

            return False, detail

        stamp = int(now)
        argv = schedule_argv(systemd_run, systemctl, support["unit"], support["scope"], stamp)

        try:
            result = self.system.run(argv)
            ok, detail = result.returncode == 0, last_line(result.stderr) or last_line(result.stdout)
        except (OSError, subprocess.SubprocessError) as failure:
            ok, detail = False, str(failure)

        if not ok:
            detail = f"systemd-run failed: {detail or 'no reason given'}"
            self._update(entry_id, outcome="failed", finished_at=now, detail=detail[:300])
            log.warning("hermes-bot-mobile: could not schedule a restart of %s: %s", support["unit"], detail)

            return False, detail

        self._update(entry_id, outcome="scheduled", scheduled_at=now, carried_out_at=now, transient_unit=f"{TRANSIENT_PREFIX}{stamp}")
        log.info("hermes-bot-mobile: %s restarts in %s s (%s%s)", support["unit"], DELAY_S, TRANSIENT_PREFIX, stamp)

        return True, ""

    # -- the log -----------------------------------------------------------------------------------------------

    def _append(self, entry: dict) -> None:
        def change(rows: list) -> None:
            rows.append(entry)
            rows[:] = rows[-KEEP:]

        self._file().update(change)

    def _update(self, entry_id: str, **fields: Any) -> None:
        def change(rows: list) -> None:
            for row in rows:
                if isinstance(row, dict) and row.get("id") == entry_id:
                    row.update(fields)

        self._file().update(change)

    def _refused(self, entry: dict, refusal: Refused) -> None:
        """Log a refused request; a run of the same refusal is one row with a count, so a retry loop cannot push the
        restarts that happened out of the log."""
        def change(rows: list) -> None:
            last = rows[-1] if rows and isinstance(rows[-1], dict) else None

            if last and last.get("outcome") == "refused" and last.get("status") == refusal.status and last.get("mode") == entry["mode"]:
                last.update(requested_at=entry["requested_at"], count=int(last.get("count") or 1) + 1, detail=refusal.detail)
            else:
                rows.append({**entry, "outcome": "refused", "status": refusal.status, "detail": refusal.detail})

            rows[:] = rows[-KEEP:]

        self._file().update(change)


def retry_after(rows: list[dict], now: float) -> int:
    """Seconds until the next restart may start: one a minute, counted from the last request that restarted something."""
    carried = [float(row["carried_out_at"]) for row in rows if isinstance(row, dict) and row.get("carried_out_at")]

    if not carried:
        return 0

    left = RATE_LIMIT_S - (now - max(carried))

    return min(RATE_LIMIT_S, math.ceil(left)) if left > 0 else 0
