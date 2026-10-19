import copy
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import helpers  # noqa: F401  (puts the plugin on sys.path)
import restart
from test_api import load_api

UNIT = "hermes-dashboard.service"
USER_CGROUP = f"0::/user.slice/user-1000.slice/user@1000.service/app.slice/{UNIT}\n"
SYSTEM_CGROUP = f"0::/system.slice/{UNIT}\n"
GATEWAY_CGROUP = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/hermes-gateway.service\n"
SYSTEMD_RUN, SYSTEMCTL = "/usr/bin/systemd-run", "/usr/bin/systemctl"


def done(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class FakeProc:
    """A `hermes gateway restart` child: `wait` answers with its exit code, or times out."""

    def __init__(self, code=0, pid=777):
        self.code, self.pid, self.waited = code, pid, []

    def wait(self, timeout=None):
        self.waited.append(timeout)

        if self.code is None:
            raise subprocess.TimeoutExpired("hermes", timeout)

        return self.code


class FakeSystem(restart.System):
    """This computer as the restarter sees it: a Linux box whose dashboard is the `--user` unit hermes-dashboard.service."""

    def __init__(self, *, platform="linux", cgroup=USER_CGROUP, uid=1000, euid=1000, systemd=True, container=False,
                 environ=None, tools=("systemd-run", "systemctl"), busy=(), gateway_pid=None, gateway_cgroup=GATEWAY_CGROUP,
                 spawner=None, route=True, pid=4242, started=1000.0, show=None, systemd_run=None):
        self.platform = platform
        self.environ = environ if environ is not None else {"PATH": "/usr/bin", "HOME": "/home/scout"}
        self._cgroup, self._uid, self._euid, self._systemd, self._container = cgroup, uid, euid, systemd, container
        self._tools, self._gateway_pid, self._gateway_cgroup = tools, gateway_pid, gateway_cgroup
        self._spawner, self._route, self._pid, self._started = spawner, route, pid, started
        #: The chats running a turn (None: the registry cannot be read).
        self.busy = None if busy is None else list(busy)
        self.show = show
        self.systemd_run = systemd_run or done()
        self.ran, self.spawned = [], []
        #: pids of earlier dashboards that are still running.
        self.living = set()
        self.proc = FakeProc()

    def pid(self):
        return self._pid

    def uid(self):
        return self._uid

    def euid(self):
        return self._euid

    def cgroup(self, pid="self"):
        return self._cgroup if pid == "self" else self._gateway_cgroup

    def systemd_running(self):
        return self._systemd

    def in_container(self):
        return self._container

    def started_at(self, pid="self"):
        return self._started if pid == "self" else None

    def alive(self, pid, started_at):
        return pid in self.living

    def which(self, name):
        return f"/usr/bin/{name}" if name in self._tools else None

    def run(self, argv):
        self.ran.append(argv)

        if "show" in argv:
            if isinstance(self.show, BaseException):
                raise self.show

            return self.show or done(stdout=f"Id={argv[-1]}\nLoadState=loaded\nActiveState=active\n")

        if isinstance(self.systemd_run, BaseException):
            raise self.systemd_run

        return self.systemd_run

    def spawn(self, argv, log_path, env):
        self.spawned.append((argv, log_path, env))

        return self.proc

    def sessions(self, titles=False):
        return None if self.busy is None else [dict(row) for row in self.busy]

    def gateway_pid(self):
        return self._gateway_pid

    def gateway_route(self):
        return self._route

    def gateway_spawner(self):
        return self._spawner

    def scheduled(self):
        return [argv for argv in self.ran if argv[0] == SYSTEMD_RUN]


class Clock:
    """A fake clock; waiting moves it on, and `on_wait` lets a test change the world between two looks."""

    def __init__(self, now=1_760_000_000.0):
        self.now = now
        self.waits = []
        self.on_wait = None

    def __call__(self):
        return self.now

    def wait(self, event, seconds):
        self.waits.append(seconds)
        self.now += seconds

        if self.on_wait:
            self.on_wait(len(self.waits))

        return event.is_set()


def user_argv(stamp):
    return [
        SYSTEMD_RUN, "--user", "--collect", f"--unit=hermes-bot-restart-{stamp}", "--on-active=2",
        "--timer-property=RemainAfterElapse=no", f"--description=hermes-bot-mobile: restart {UNIT}",
        "--", SYSTEMCTL, "--user", "restart", UNIT,
    ]


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.folder = Path(self.dir.name)
        self.clock = Clock()
        self.deferred = []

    def restarter(self, system=None, deferred=False, **options):
        self.system = system or FakeSystem(**options)
        start = self.deferred.append if deferred else (lambda target: target())

        return restart.Restarter(self.folder, system=self.system, clock=self.clock, wait=self.clock.wait, start=start)

    def rows(self):
        return json.loads((self.folder / "restarts.json").read_text())

    def refused(self, call, status):
        with self.assertRaises(restart.Refused) as caught:
            call()

        self.assertEqual(caught.exception.status, status, caught.exception.detail)

        return caught.exception


class Cgroups(unittest.TestCase):
    def test_a_user_unit(self):
        self.assertEqual(restart.place(USER_CGROUP), restart.Placement(unit=UNIT, scope="user", uid=1000))
        instance = restart.place("0::/user.slice/user-1001.slice/user@1001.service/app.slice/hermes-dashboard@work.service")
        self.assertEqual((instance.unit, instance.scope, instance.uid), ("hermes-dashboard@work.service", "user", 1001))

    def test_a_system_unit(self):
        self.assertEqual(restart.place(SYSTEM_CGROUP), restart.Placement(unit=UNIT, scope="system"))
        # A process in a sub-cgroup its service delegated still belongs to that service.
        self.assertEqual(restart.place(f"0::/system.slice/{UNIT}/payload").unit, UNIT)

    def test_cgroup_v1_and_hybrid(self):
        legacy = f"12:pids:/user.slice/user-1000.slice\n1:name=systemd:/user.slice/user-1000.slice/user@1000.service/app.slice/{UNIT}\n"
        self.assertEqual(restart.place(legacy).unit, UNIT)
        hybrid = legacy + f"0::/user.slice/user-1000.slice/user@1000.service/app.slice/{UNIT}\n"
        self.assertEqual(restart.place(hybrid).scope, "user")
        # A hybrid host whose unified line is the root falls back to systemd's own hierarchy.
        self.assertEqual(restart.place(f"1:name=systemd:/system.slice/{UNIT}\n0::/\n").unit, UNIT)

    def test_not_a_service_of_its_own(self):
        cases = {
            "0::/user.slice/user-1000.slice/session-4.scope": "login session",
            "0::/user.slice/user-1000.slice/user@1000.service/init.scope": "init.scope",
            "0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-org.gnome.Terminal.slice/vte-spawn-1a2b.scope": "vte-spawn-1a2b.scope",
            "0::/": "root",
        }

        for text, words in cases.items():
            placement = restart.place(text)
            self.assertIsNone(placement.unit, text)
            self.assertIn(words, placement.why, text)

    def test_containers(self):
        for text in [
            "0::/system.slice/docker-3f2a9c1d.scope",
            "12:pids:/docker/3f2a9c1d\n1:name=systemd:/docker/3f2a9c1d",
            "0::/kubepods/burstable/pod1234/3f2a9c1d",
            "0::/machine.slice/libpod-3f2a9c1d.scope/container",
        ]:
            placement = restart.place(text)
            self.assertIsNone(placement.unit, text)
            self.assertIn("container", placement.why, text)

        self.assertIn("container", restart.place("0::/", in_container=True).why)

    def test_no_cgroup(self):
        for text in [None, "", "garbage"]:
            self.assertIn("no systemd cgroup", restart.place(text).why)

    def test_only_plain_unit_names_are_passed_on(self):
        for name in [UNIT, "hermes-dashboard@default.service", "hermes\\x2ddash.service", "a:b_c.d-e.service"]:
            self.assertTrue(restart.UNIT.match(name), name)

        for name in ["-x.service", "a b.service", "a;id.service", "x.scope", "x.service ", "x" * 250 + ".service", "$(id).service"]:
            self.assertFalse(restart.UNIT.match(name), name)

        odd = restart.place("0::/system.slice/a;id.service")
        self.assertIsNone(odd.unit)
        self.assertIn("will not pass on", odd.why)

    def test_reads_a_process_start_from_its_stat_line(self):
        line = "4242 (hermes (dash) x) S 1 4242 4242 0 -1 4194560 100 0 0 0 7 3 0 0 20 0 9 0 12345 1000 50 0"
        self.assertEqual(restart.proc_start(line, 1_700_000_000, 100), 1_700_000_000 + 123.45)


class Support(Base):
    def test_a_user_unit_it_can_reach(self):
        answer = self.restarter().support()
        self.assertEqual(answer, {"supported": True, "why": None, "unit": UNIT, "scope": "user", "gateway_same_unit": None})
        self.assertEqual(self.system.ran, [[SYSTEMCTL, "--user", "show", "--property=Id", "--property=LoadState", "--property=ActiveState", "--", UNIT]])

    def test_macos_and_other_systems(self):
        answer = self.restarter(platform="darwin").support()
        self.assertFalse(answer["supported"])
        self.assertIn("launchd", answer["why"])
        self.assertIn("systemd", self.restarter(platform="win32").support()["why"])

    def test_no_systemd(self):
        self.assertIn("does not run systemd", self.restarter(systemd=False).support()["why"])

    def test_a_system_unit_needs_root(self):
        answer = self.restarter(cgroup=SYSTEM_CGROUP).support()
        self.assertEqual((answer["supported"], answer["unit"], answer["scope"]), (False, UNIT, "system"))
        self.assertIn("needs root", answer["why"])
        self.assertEqual(self.system.ran, [])

        answer = self.restarter(cgroup=SYSTEM_CGROUP, euid=0, uid=0).support()
        self.assertTrue(answer["supported"])
        self.assertEqual(self.system.ran[0][:3], [SYSTEMCTL, "show", "--property=Id"])

    def test_another_users_systemd(self):
        self.assertIn("another user", self.restarter(uid=1001).support()["why"])

    def test_missing_tools(self):
        self.assertIn("systemd-run", self.restarter(tools=("systemctl",)).support()["why"])

    def test_says_what_systemctl_said(self):
        answer = self.restarter(show=done(1, stderr="Failed to connect to bus: No medium found\n")).support()
        self.assertFalse(answer["supported"])
        self.assertIn("No medium found", answer["why"])
        self.assertIn("systemctl --user", answer["why"])

        not_loaded = self.restarter(show=done(stdout=f"Id={UNIT}\nLoadState=not-found\n")).support()
        self.assertIn("LoadState=not-found", not_loaded["why"])

        missing = self.restarter(show=FileNotFoundError("systemctl")).support()
        self.assertIn("could not be asked", missing["why"])

    def test_not_inside_the_gateway(self):
        me = FakeSystem().pid()
        answer = self.restarter(environ={"_HERMES_GATEWAY": "1"}, gateway_pid=me).support()
        self.assertIn("/api/gateway/restart", answer["why"])

    def test_the_gateway_marker_alone_is_not_the_gateway(self):
        # A standalone dashboard that imported Hermes's gateway modules carries the marker too (0.21.6).
        apart = self.restarter(environ={"_HERMES_GATEWAY": "1"}, gateway_pid=99).support()
        self.assertEqual((apart["supported"], apart["why"]), (True, None))
        no_gateway = self.restarter(environ={"_HERMES_GATEWAY": "1"}).support()
        self.assertTrue(no_gateway["supported"])

    def test_not_when_the_gateway_shares_the_unit(self):
        answer = self.restarter(gateway_pid=99, gateway_cgroup=USER_CGROUP).support()
        self.assertEqual((answer["supported"], answer["gateway_same_unit"]), (False, True))
        self.assertIn("same systemd service", answer["why"])

        apart = self.restarter(gateway_pid=99).support()
        self.assertEqual((apart["supported"], apart["gateway_same_unit"]), (True, False))

    def test_the_answer_is_reused_for_a_while(self):
        restarter = self.restarter()
        restarter.support()
        restarter.support()
        self.assertEqual(len(self.system.ran), 1)
        self.clock.now += restart.PROBE_TTL_S + 1
        restarter.support()
        self.assertEqual(len(self.system.ran), 2)


class Now(Base):
    def test_schedules_its_own_unit_from_outside_its_cgroup(self):
        restarter = self.restarter()
        accepted = restarter.request("now")

        self.assertEqual({key: accepted[key] for key in ("accepted", "mode", "unit", "include_gateway")}, {"accepted": True, "mode": "now", "unit": UNIT, "include_gateway": False})
        self.assertEqual(self.system.scheduled(), [user_argv(int(self.clock.now))])
        (row,) = self.rows()
        self.assertEqual((row["id"], row["outcome"], row["mode"], row["transient_unit"]), (accepted["id"], "scheduled", "now", f"hermes-bot-restart-{int(self.clock.now)}"))
        self.assertEqual((row["scheduled_at"], row["carried_out_at"]), (self.clock.now, self.clock.now))

    def test_a_system_unit_as_root(self):
        self.restarter(cgroup=SYSTEM_CGROUP, euid=0, uid=0).request("now")
        stamp = int(self.clock.now)
        self.assertEqual(self.system.scheduled(), [[
            SYSTEMD_RUN, "--collect", f"--unit=hermes-bot-restart-{stamp}", "--on-active=2", "--timer-property=RemainAfterElapse=no",
            f"--description=hermes-bot-mobile: restart {UNIT}", "--", SYSTEMCTL, "restart", UNIT,
        ]])

    def test_says_when_systemd_refused_and_lets_it_be_tried_again(self):
        restarter = self.restarter(systemd_run=done(1, stderr="Failed to start transient timer unit: Unit already exists.\n"))

        with self.assertLogs("hermes-bot-mobile", "WARNING") as logged:
            failure = self.refused(lambda: restarter.request("now"), 500)

        self.assertIn("Unit already exists", failure.detail)
        self.assertIn("could not schedule a restart of hermes-dashboard.service", logged.output[0])
        self.assertEqual(self.rows()[-1]["outcome"], "failed")

        self.system.systemd_run = done()
        self.clock.now += 1
        self.assertTrue(restarter.request("now")["accepted"])

    def test_refuses_where_it_cannot_restart(self):
        restarter = self.restarter(platform="darwin")
        refusal = self.refused(lambda: restarter.request("now"), 409)
        self.assertIn("launchd", refusal.detail)
        self.assertEqual(self.system.scheduled(), [])
        self.assertEqual((self.rows()[-1]["outcome"], self.rows()[-1]["status"]), ("refused", 409))

    def test_checks_its_arguments(self):
        restarter = self.restarter()
        self.refused(lambda: restarter.request("later"), 422)

        for timeout in [29, 1801, "600", True]:
            self.refused(lambda: restarter.request("idle", timeout), 422)


class RateLimit(Base):
    def test_one_restart_a_minute(self):
        restarter = self.restarter()
        restarter.request("now")
        self.clock.now += 20.5
        refusal = self.refused(lambda: restarter.request("now"), 429)
        self.assertEqual(refusal.extra, {"retry_after": 40})
        self.assertEqual(len(self.system.scheduled()), 1)

        self.clock.now += 40
        self.assertTrue(restarter.request("now")["accepted"])
        self.assertEqual(len(self.system.scheduled()), 2)

    def test_the_limit_outlasts_the_restart(self):
        self.restarter().request("now")
        self.clock.now += 8
        # The dashboard that came back is a new process with the same log.
        again = self.restarter(pid=5555, started=self.clock.now - 3)
        self.assertEqual(self.refused(lambda: again.request("now"), 429).extra["retry_after"], 52)

    def test_a_retry_loop_is_one_row(self):
        restarter = self.restarter()
        restarter.request("now")

        for _ in range(30):
            self.clock.now += 1
            self.refused(lambda: restarter.request("now"), 429)

        rows = self.rows()
        self.assertEqual([row["outcome"] for row in rows], ["scheduled", "refused"])
        self.assertEqual(rows[-1]["count"], 30)
        self.assertEqual(restarter.status()["last"]["outcome"], "scheduled")


class Idle(Base):
    def test_waits_until_no_chat_is_running(self):
        restarter = self.restarter(busy=[{"id": "s1", "session": "20261017_a", "title": "Plan the week", "status": "working"}])
        start = self.clock.now

        def finish(polls):
            if polls == 2:
                self.system.busy = []

        self.clock.on_wait = finish
        accepted = restarter.request("idle", 300)

        self.assertEqual(accepted["mode"], "idle")
        self.assertEqual(self.clock.waits, [restart.POLL_S, restart.POLL_S])
        self.assertEqual(self.system.scheduled(), [user_argv(int(start + 2 * restart.POLL_S))])
        (row,) = self.rows()
        self.assertEqual((row["outcome"], row["idle_at"], row["deadline"], row["idle_timeout_s"]), ("scheduled", start + 2 * restart.POLL_S, start + 300, 300))

    def test_restarts_at_once_when_nothing_runs(self):
        self.restarter(busy=[]).request("idle")
        self.assertEqual(self.clock.waits, [])
        self.assertEqual(len(self.system.scheduled()), 1)

    def test_gives_up_at_the_timeout(self):
        busy = [{"id": "s1", "status": "working"}, {"id": "s2", "status": "waiting"}]
        restarter = self.restarter(busy=busy)
        restarter.request("idle", 30)

        self.assertEqual(sum(self.clock.waits), 30)
        self.assertEqual(self.system.scheduled(), [])
        (row,) = self.rows()
        self.assertEqual((row["outcome"], row["busy_left"], row["detail"]), ("timed_out", 2, "Timed out with 2 still busy."))
        # Nothing was restarted, so nothing holds the next request back.
        self.system.busy = []
        self.assertTrue(restarter.request("now")["accepted"])

    def test_cancel(self):
        restarter = self.restarter(busy=[{"id": "s1"}], deferred=True)
        accepted = restarter.request("idle")
        self.assertEqual(self.rows()[-1]["outcome"], "waiting")
        self.assertEqual(restarter.cancel(), {"cancelled": True, "id": accepted["id"]})

        self.deferred.pop()()  # the waiting thread wakes up and finds itself cancelled
        self.assertEqual(self.system.scheduled(), [])
        self.assertEqual(self.rows()[-1]["outcome"], "cancelled")
        self.refused(restarter.cancel, 404)

    def test_cancel_while_it_waits(self):
        restarter = self.restarter(busy=[{"id": "s1"}])
        self.clock.on_wait = lambda polls: restarter.cancel() if polls == 1 else None
        restarter.request("idle")

        self.assertEqual(len(self.clock.waits), 1)
        self.assertEqual(self.system.scheduled(), [])
        self.assertEqual(self.rows()[-1]["outcome"], "cancelled")

    def test_a_new_request_replaces_the_waiting_one(self):
        restarter = self.restarter(busy=[{"id": "s1"}], deferred=True)
        first = restarter.request("idle", 900)
        second = restarter.request("idle", 60)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual([row["outcome"] for row in self.rows()], ["replaced", "waiting"])

        restarter.request("now")
        self.assertEqual([row["outcome"] for row in self.rows()], ["replaced", "replaced", "scheduled"])

        for target in self.deferred:  # the replaced threads stop without restarting anything
            target()

        self.assertEqual(len(self.system.scheduled()), 1)

    def test_needs_to_see_the_running_chats(self):
        refusal = self.refused(lambda: self.restarter(busy=None).request("idle"), 409)
        self.assertIn("cannot see which chats", refusal.detail)

    def test_losing_sight_of_the_chats_midway(self):
        restarter = self.restarter(busy=[{"id": "s1"}])

        def lose(polls):
            self.system.busy = None

        self.clock.on_wait = lose
        restarter.request("idle")
        self.assertEqual(self.system.scheduled(), [])
        self.assertEqual(self.rows()[-1]["outcome"], "failed")


class Gateway(Base):
    def test_hermes_restarts_the_gateway_first_and_the_dashboard_after_it_finished(self):
        order = []
        proc = FakeProc(code=0, pid=31337)

        def spawner(profile):
            order.append(("gateway", profile))

            return proc, False

        restarter = self.restarter(spawner=spawner)
        original_run = self.system.run

        def run(argv):
            if argv[0] == SYSTEMD_RUN:
                order.append(("systemd-run",))

            return original_run(argv)

        self.system.run = run
        restarter.request("now", include_gateway=True)

        self.assertEqual(order, [("gateway", None), ("systemd-run",)])
        self.assertEqual(proc.waited, [restart.GATEWAY_WAIT_S])
        row = self.rows()[-1]
        self.assertEqual(row["outcome"], "scheduled")
        self.assertEqual({key: row["gateway"][key] for key in ("how", "pid", "outcome", "exit_code", "reused")}, {"how": "hermes", "pid": 31337, "outcome": "exited", "exit_code": 0, "reused": False})

    def test_without_hermess_spawner_it_runs_the_cli_the_same_way(self):
        restarter = self.restarter(environ={"PATH": "/usr/bin", "HERMES_HOME": "/home/scout/.hermes"})
        restarter.request("now", include_gateway=True)

        ((argv, log_path, env),) = self.system.spawned
        self.assertEqual(argv, [sys.executable, "-m", "hermes_cli.main", "gateway", "restart"])
        self.assertEqual(log_path, self.folder / "gateway-restart.log")
        self.assertEqual(env, {"PATH": "/usr/bin", "HERMES_HOME": "/home/scout/.hermes", "HERMES_NONINTERACTIVE": "1"})
        self.assertEqual(self.system.proc.waited, [restart.GATEWAY_WAIT_S])
        self.assertEqual(self.rows()[-1]["gateway"]["how"], "cli")
        self.assertEqual(len(self.system.scheduled()), 1)

    def test_the_cli_child_never_inherits_the_gateway_marker(self):
        # Hermes drops it too: with it, `hermes gateway restart` refuses (its guard against a gateway restarting itself).
        env = restart.gateway_cli_env({"_HERMES_GATEWAY": "1", "_HERMES_GATEWAY_BREAKAWAY": "keep", "HOME": "/home/scout"})
        self.assertEqual(env, {"_HERMES_GATEWAY_BREAKAWAY": "keep", "HOME": "/home/scout", "HERMES_NONINTERACTIVE": "1"})

    def test_a_gateway_that_would_not_restart_still_lets_the_dashboard_restart(self):
        def spawner(profile):
            raise RuntimeError("gateway restart already in progress for another profile")

        self.restarter(spawner=spawner).request("now", include_gateway=True)
        row = self.rows()[-1]
        self.assertEqual((row["gateway"]["outcome"], row["outcome"]), ("failed", "scheduled"))
        self.assertIn("another profile", row["gateway"]["detail"])

    def test_a_gateway_restart_still_running_after_the_wait(self):
        proc = FakeProc(code=None)
        self.restarter(spawner=lambda profile: (proc, True)).request("now", include_gateway=True)
        row = self.rows()[-1]
        self.assertEqual((row["gateway"]["outcome"], row["gateway"]["reused"], row["outcome"]), ("still_running", True, "scheduled"))

    def test_under_way_it_cannot_be_cancelled_or_started_again(self):
        restarter = self.restarter(spawner=lambda profile: (FakeProc(), False), deferred=True)
        restarter.request("now", include_gateway=True)
        self.assertEqual(self.rows()[-1]["outcome"], "restarting_gateway")
        self.clock.now += 120
        self.assertIn("under way", self.refused(lambda: restarter.request("now"), 409).detail)
        self.refused(restarter.cancel, 409)

        self.deferred.pop()()
        self.assertEqual(self.rows()[-2]["outcome"], "scheduled")

    def test_idle_then_gateway_then_dashboard(self):
        calls = []
        restarter = self.restarter(busy=[{"id": "s1"}], spawner=lambda profile: (calls.append(profile), (FakeProc(), False))[1])
        self.clock.on_wait = lambda polls: setattr(self.system, "busy", [])
        restarter.request("idle", include_gateway=True)

        self.assertEqual(calls, [None])
        self.assertEqual(self.rows()[-1]["outcome"], "scheduled")
        self.assertEqual(self.rows()[-1]["carried_out_at"], self.rows()[-1]["idle_at"])


class Log(Base):
    def test_keeps_the_last_twenty_with_private_permissions(self):
        restarter = self.restarter()
        ids = []

        for _ in range(25):
            ids.append(restarter.request("now")["id"])
            self.clock.now += restart.RATE_LIMIT_S

        rows = self.rows()
        self.assertEqual([row["id"] for row in rows], ids[-20:])
        self.assertEqual(stat.S_IMODE(os.stat(self.folder / "restarts.json").st_mode), 0o600)

    def test_the_restarted_dashboard_says_it_was_restarted(self):
        self.restarter(pid=4242, started=1000.0).request("now")
        scheduled = self.clock.now
        self.clock.now += 9

        after = self.restarter(pid=5555, started=scheduled + 3)
        last = after.status()["last"]
        self.assertEqual((last["outcome"], last["restarted_at"]), ("done", scheduled + 3))
        self.assertNotIn("by", last)
        # Reading never rewrites the log.
        self.assertEqual(self.rows()[-1]["outcome"], "scheduled")

    def test_an_old_dashboard_still_running_is_not_a_restart(self):
        self.restarter(pid=4242).request("now")
        self.clock.now += 5
        other = self.restarter(pid=5555, started=self.clock.now)
        self.system.living.add(4242)
        self.assertEqual(other.status()["last"]["outcome"], "scheduled")

    def test_a_restart_that_never_came(self):
        restarter = self.restarter()
        restarter.request("now")
        self.clock.now += restart.NOT_RESTARTED_AFTER_S + 1
        last = restarter.status()["last"]
        self.assertEqual(last["outcome"], "not_restarted")
        self.assertIn("journalctl --user -u hermes-bot-restart-", last["detail"])

    def test_a_wait_cut_short_by_another_restart(self):
        self.restarter(busy=[{"id": "s1"}], deferred=True).request("idle")
        after = self.restarter(pid=5555, started=self.clock.now + 30)
        self.assertEqual(after.status()["last"]["outcome"], "interrupted")


class Status(Base):
    def test_says_everything_the_app_needs(self):
        busy = [{"id": "s1", "session": "20261017_a", "title": "Plan the week", "profile": "default", "status": "waiting"}]
        status = self.restarter(busy=busy, gateway_pid=99, spawner=lambda profile: None).status()

        self.assertEqual(status, {
            "supported": True, "why": None, "unit": UNIT, "scope": "user", "pid": 4242, "started_at": 1000.0,
            "now": self.clock.now, "busy": busy, "gateway": {"route": True, "restart": "hermes", "same_unit": False},
            "last": None, "retry_after": 0,
        })

    def test_unsupported_still_answers(self):
        status = self.restarter(platform="darwin", busy=None, route=False).status()
        self.assertEqual((status["supported"], status["unit"], status["busy"], status["gateway"]["route"], status["gateway"]["restart"]), (False, None, None, False, "cli"))
        self.assertIn("launchd", status["why"])


class Sessions(unittest.TestCase):
    def server(self, sessions, **helpers):
        return SimpleNamespace(_sessions=sessions, _sessions_lock=threading.RLock(), **helpers)

    def test_lists_the_chats_running_a_turn_and_changes_nothing(self):
        sessions = {
            "a1": {"running": True, "session_key": "20261017_a", "pending_title": "Plan the week"},
            "b2": {"running": False, "session_key": "20261017_b"},
            "c3": {"running": True, "_finalized": True},
            "d4": {"running": True, "session_key": "20261017_d"},
        }
        before = copy.deepcopy(sessions)
        server = self.server(
            sessions,
            _session_live_status=lambda sid, session: "waiting" if sid == "d4" else "working",
            _session_lookup_key=lambda session, fallback="": session.get("session_key") or fallback,
            _session_live_title=lambda session, key: {"20261017_d": "Inbox"}.get(key, ""),
        )

        rows = restart.live_sessions(server)
        self.assertEqual([(row["id"], row["session"], row["title"], row["status"]) for row in rows], [("a1", "20261017_a", "Plan the week", "working"), ("d4", "20261017_d", "", "waiting")])
        self.assertEqual(restart.live_sessions(server, titles=True)[1]["title"], "Inbox")
        self.assertEqual(sessions, before)

    def test_without_a_registry(self):
        self.assertIsNone(restart.live_sessions(None))
        self.assertIsNone(restart.live_sessions(SimpleNamespace()))

    def test_helpers_that_fail_do_not_hide_a_running_chat(self):
        def broken(*_, **__):
            raise RuntimeError("db locked")

        server = self.server({"a1": {"running": True}}, _session_live_status=broken, _session_lookup_key=broken, _session_live_title=broken)
        (row,) = restart.live_sessions(server, titles=True)
        self.assertEqual((row["id"], row["session"], row["status"]), ("a1", "a1", "working"))


class Routes(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.module, *_, patch = load_api(self.dir.name)
        patch.start()
        self.addCleanup(patch.stop)
        self.clock = Clock()
        self.system = FakeSystem(busy=[{"id": "s1", "session": "20261017_a", "title": "Plan the week", "status": "working"}])
        self.deferred = []
        self.use(self.system)
        app = FastAPI()
        app.include_router(self.module.router, prefix="/api/plugins/hermes-bot-mobile")
        self.client = TestClient(app)
        self.url = "/api/plugins/hermes-bot-mobile/restart"

    def use(self, system):
        restarter = self.module._restart().Restarter(Path(self.dir.name), system=system, clock=self.clock, wait=self.clock.wait, start=self.deferred.append)
        self.module._RESTARTER = restarter

    def tearDown(self):
        self.module._RESTARTER = None
        self.dir.cleanup()

        for key in [k for k in sys.modules if k.startswith("_hermes_bot_mobile_package") or k.startswith("hermes_dashboard_plugin_hermes-bot-mobile-test")]:
            del sys.modules[key]

    def test_status(self):
        body = self.client.get(self.url).json()
        self.assertEqual((body["supported"], body["unit"], body["scope"], body["started_at"]), (True, UNIT, "user", 1000.0))
        self.assertEqual([row["title"] for row in body["busy"]], ["Plan the week"])
        self.assertEqual(body["gateway"]["route"], True)
        self.assertIsNone(body["last"])

    def test_restart_now(self):
        response = self.client.post(self.url, json={"when": "now"})
        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertEqual((body["accepted"], body["mode"], body["unit"]), (True, "now", UNIT))
        self.assertEqual(len(self.system.scheduled()), 1)
        self.assertEqual(self.client.get(self.url).json()["last"]["outcome"], "scheduled")

    def test_a_second_restart_within_a_minute(self):
        self.client.post(self.url, json={"when": "now"})
        self.clock.now += 15
        response = self.client.post(self.url, json={"when": "now"})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["retry_after"], 45)
        self.assertEqual(response.headers["retry-after"], "45")
        self.assertEqual(self.client.get(self.url).json()["retry_after"], 45)

    def test_unsupported_is_a_409_with_the_reason(self):
        self.use(FakeSystem(cgroup="0::/user.slice/user-1000.slice/session-4.scope"))
        response = self.client.post(self.url, json={"when": "now"})
        self.assertEqual(response.status_code, 409)
        self.assertIn("login session", response.json()["detail"])

    def test_never_takes_a_unit_or_a_command(self):
        for body in [{"when": "now", "unit": "sshd.service"}, {"when": "now", "command": ["id"]}, {"when": "soon"}, {}, {"when": "idle", "idle_timeout_s": 5}, {"when": "idle", "idle_timeout_s": 3600}]:
            self.assertEqual(self.client.post(self.url, json=body).status_code, 422, body)

        self.assertEqual(self.system.scheduled(), [])

    def test_wait_for_idle_then_cancel(self):
        response = self.client.post(self.url, json={"when": "idle", "idle_timeout_s": 120, "include_gateway": True})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["include_gateway"], True)
        last = self.client.get(self.url).json()["last"]
        self.assertEqual((last["outcome"], last["idle_timeout_s"], last["include_gateway"]), ("waiting", 120, True))

        cancelled = self.client.delete(self.url)
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json(), {"cancelled": True, "id": response.json()["id"]})
        self.assertEqual(self.client.get(self.url).json()["last"]["outcome"], "cancelled")
        self.assertEqual(self.client.delete(self.url).status_code, 404)


if __name__ == "__main__":
    unittest.main()
