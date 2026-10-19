import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import helpers  # noqa: F401  (puts the plugin on sys.path)
import taildrop
from test_api import load_api

# The CLI's own output formats (tailscale v1.104.1, cmd/tailscale/cli/file.go, whois.go and cli.go), with made-up devices.
TARGETS = (
    "100.64.0.13\tpixel-9\n"
    "100.64.0.14\tworkstation\toffline; last seen 3h12m0s ago\n"
    "fd7a:115c:a1e0::5\tlab-box\tunknown-status\n"
)
WHOIS = json.dumps({
    "Node": {
        "ID": 1234, "StableID": "nExample1CNTRL", "Name": "pixel-9.example-tailnet.ts.net.", "ComputedName": "pixel-9",
        "Addresses": ["100.64.0.13/32", "fd7a:115c:a1e0::3/128"], "Hostinfo": {"OS": "android", "Hostname": "Pixel 9"},
    },
    "UserProfile": {"ID": 99, "LoginName": "person@example.com", "DisplayName": "Person"},
    "CapMap": {},
}, indent=2)
STATUS = json.dumps({"BackendState": "Running", "Self": {"HostName": "server", "TailscaleIPs": ["100.64.0.10"], "NoFileSharingReason": ""}})
DENIED = (
    "Access denied: file access denied\n\n"
    "Use 'sudo tailscale file cp --name=report.pdf /dev/fd/7 100.64.0.13:'.\n"
    "To not require root, use 'sudo tailscale set --operator=$USER' once.\n"
)
DAEMON_DOWN = (
    "Failed to connect to local Tailscale daemon for /localapi/v0/status; systemd tailscaled.service not running. "
    "Error: dial unix /var/run/tailscale/tailscaled.sock: connect: no such file or directory\n"
)


def progress_line(name, received, rate, percent=None, eta="ETA 00:00:02"):
    """One repaint of the CLI's progress line, laid out as its progressPrinter does."""
    line = "\r\x1b[K" + name.ljust(36)[:36] + "    " + received.rjust(10) + "    " + rate.rjust(12)

    return line + ("    " + f"{percent:.2f}%".rjust(7) + "    " + eta if percent is not None else "")


def done(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def wait_for(get, until, timeout=5.0):
    deadline = time.time() + timeout
    value = get()

    while not until(value) and time.time() < deadline:
        time.sleep(0.01)
        value = get()

    return value


class FakeProc:
    def __init__(self, code=0, blocking=False):
        self.code = code
        self.pid = 4321
        self.terminated = threading.Event()
        self.blocking = blocking
        self.killed = False

    def wait(self, timeout=None):
        if self.blocking:
            self.terminated.wait(5)
            return -15

        return self.code

    def poll(self):
        return None if self.blocking and not self.terminated.is_set() else self.code

    def terminate(self):
        self.terminated.set()

    def kill(self):
        self.killed = True


class FakeTailscale(taildrop.System):
    """A server with the tailscale CLI, connected, offering three devices."""

    def __init__(self, *, installed=True, status=None, targets=TARGETS, whois=WHOIS, user="hermes", environ=None, stderr=b"", code=0, blocking=False):
        self.environ = environ or {}
        self._installed, self._user = installed, user
        self.answers = {
            ("status", "--json"): status or done(stdout=STATUS),
            ("file", "cp", "--targets"): targets if isinstance(targets, SimpleNamespace) else done(stdout=targets),
        }
        self._whois = whois
        self.stderr, self.code, self.blocking = stderr, code, blocking
        self.ran, self.started = [], []
        self.on_popen = None

    def which(self, name):
        return "/usr/bin/tailscale" if self._installed and name == "tailscale" else None

    def run(self, argv):
        self.ran.append(argv)

        if argv[1:3] == ["whois", "--json"]:
            return done(stdout=self._whois) if self._whois else done(1, stderr="peer not found\n")

        return self.answers[tuple(argv[1:])]

    def user(self):
        return self._user

    def openpty(self):
        return os.pipe()

    def popen(self, argv, *, stderr, pass_fds):
        (fd,) = pass_fds
        content = os.pread(fd, os.fstat(fd).st_size + 1, 0)
        self.started.append({"argv": argv, "content": content})

        if self.on_popen:
            self.on_popen(argv)

        os.write(stderr, self.stderr)
        self.proc = FakeProc(self.code, self.blocking)

        return self.proc


class FakeFiles:
    """Stands in for hermes_cli.web_routers.files: records what it was asked and refuses what it is told to."""

    def __init__(self, refuse=None, sensitive=None):
        self.asked = []
        self.refuse = refuse or {}
        self.sensitive = sensitive

    async def _fs_download_path(self, path, profile, session_id):
        self.asked.append((path, profile, session_id))

        if path in self.refuse:
            raise HTTPException(status_code=self.refuse[path][0], detail=self.refuse[path][1])

        return Path(path).resolve()

    def _fs_regular_file(self, path):
        return path, os.stat(path)

    def _is_sensitive_path(self, path):
        return bool(self.sensitive and self.sensitive(path))


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.folder = Path(self.dir.name) / "data"
        self.files_dir = Path(self.dir.name) / "files"
        self.files_dir.mkdir()
        self.report = self.files_dir / "report.pdf"
        self.report.write_bytes(b"%PDF-1.7 example\n" * 64)
        self.deferred = []

    def sender(self, system=None, files=None, deferred=False, **options):
        self.system = system or FakeTailscale(**options)
        self.files = files or FakeFiles()
        start = self.deferred.append if deferred else (lambda target: target())

        return taildrop.Taildrop(self.folder, system=self.system, files=lambda: self.files, start=start)

    def checked(self, sender, path=None):
        return asyncio.run(sender.check(str(path or self.report)))

    def refused(self, call, status):
        with self.assertRaises(taildrop.Refused) as caught:
            call()

        self.assertEqual(caught.exception.status, status, caught.exception.detail)

        return caught.exception

    def rows(self):
        return json.loads((self.folder / "taildrop.json").read_text())


class Parsers(unittest.TestCase):
    def test_targets(self):
        self.assertEqual(taildrop.parse_targets(TARGETS), [
            {"name": "pixel-9", "ip": "100.64.0.13", "online": True, "detail": None},
            {"name": "workstation", "ip": "100.64.0.14", "online": False, "detail": "offline; last seen 3h12m0s ago"},
            {"name": "lab-box", "ip": "fd7a:115c:a1e0::5", "online": None, "detail": "unknown-status"},
        ])

    def test_targets_it_does_not_understand_are_skipped(self):
        text = "\nnot-an-ip\tbox\n100.64.0.9\n100.64.0.10 spaced-name\r\n# 100.64.0.11\tcomment\n"
        self.assertEqual([t["name"] for t in taildrop.parse_targets(text)], ["spaced-name"])
        self.assertEqual(taildrop.parse_targets(""), [])

    def test_whois(self):
        self.assertEqual(taildrop.parse_whois(WHOIS), {"name": "pixel-9", "os": "android", "addresses": ["100.64.0.13", "fd7a:115c:a1e0::3"]})
        # No ComputedName or OS (a node's own Hostinfo can be that short): the first label of its DNS name.
        bare = json.dumps({"Node": {"Name": "lab-box.example-tailnet.ts.net.", "Hostinfo": {"Hostname": "lab"}}})
        self.assertEqual(taildrop.parse_whois(bare), {"name": "lab-box", "os": None, "addresses": []})

        for text in ["", "peer not found", "[]", json.dumps({"Node": None}), json.dumps({"Node": {}})]:
            self.assertIsNone(taildrop.parse_whois(text), text)

    def test_progress(self):
        self.assertEqual(taildrop.parse_progress(progress_line("report.pdf", "1.50MiB", "512.00KiB/s", 37.5)), (37.5, int(1.5 * 2**20)))
        self.assertEqual(taildrop.parse_progress(progress_line("stdin", "900.00B", "0.00B/s")), (None, 900))
        self.assertEqual(taildrop.parse_progress(progress_line("x" * 50, "2.00GiB", "1.00MiB/s", 100.0, "ETA -")), (100.0, 2 * 2**30))
        self.assertEqual(taildrop.parse_progress("# warning: pixel-9 is not replying; trying anyway"), (None, None))

    def test_the_operator_advice(self):
        self.assertEqual(taildrop.operator_setup(DENIED, "hermes"), {"needed": "operator", "user": "hermes", "command": "sudo tailscale set --operator=hermes"})
        # Denied without the advice (its wording changed): still the operator, without a command to copy.
        self.assertEqual(taildrop.operator_setup("Access denied: file access denied", "hermes"), {"needed": "operator", "user": "hermes", "command": None})

        for text in ["", DAEMON_DOWN, "can't send to pixel-9: cannot send files: peer is owned by a different user"]:
            self.assertIsNone(taildrop.operator_setup(text, "hermes"), text)

    def test_names(self):
        self.assertEqual(taildrop.clean_name("../../etc/report.pdf", "file"), "report.pdf")
        self.assertEqual(taildrop.clean_name(".hidden", "file"), "hidden")
        self.assertEqual(taildrop.clean_name("--name=x", "file"), "name=x")
        self.assertEqual(taildrop.clean_name("a\x00b\nc.txt", "file"), "abc.txt")
        self.assertEqual(taildrop.clean_name("", "changes"), "changes")
        long = taildrop.clean_name("x" * 300 + ".pdf", "file")
        self.assertEqual((len(long), long[-4:]), (128, ".pdf"))

    def test_the_cli_call(self):
        self.assertEqual(taildrop.cp_argv("/usr/bin/tailscale", 7, "report.pdf", "100.64.0.13"), ["/usr/bin/tailscale", "file", "cp", "--name=report.pdf", "/dev/fd/7", "100.64.0.13:"])
        self.assertEqual(taildrop.target_arg("fd7a:115c:a1e0::5"), "[fd7a:115c:a1e0::5]:")

    def test_the_size_cap(self):
        self.assertEqual(taildrop.max_bytes({}), 2 * 1024 ** 3)
        self.assertEqual(taildrop.max_bytes({"HERMES_BOT_TAILDROP_MAX_BYTES": "1048576"}), 1048576)

        for bad in ["lots", "-5", "0"]:
            self.assertEqual(taildrop.max_bytes({"HERMES_BOT_TAILDROP_MAX_BYTES": bad}), 2 * 1024 ** 3)


class Status(Base):
    def test_lists_the_devices_and_says_who_is_asking(self):
        status = self.sender().status("100.64.0.13")
        self.assertEqual((status["supported"], status["why"], status["setup"], status["sending"]), (True, None, None, []))
        self.assertEqual([(t["name"], t["you"]) for t in status["targets"]], [("pixel-9", True), ("workstation", False), ("lab-box", False)])
        self.assertEqual(status["you"], {"name": "pixel-9", "os": "android", "ip": "100.64.0.13"})
        self.assertIn(["/usr/bin/tailscale", "whois", "--json", "100.64.0.13"], self.system.ran)

    def test_a_dual_stack_address_is_asked_about_as_ipv4(self):
        self.sender().status("::ffff:100.64.0.13")
        self.assertIn(["/usr/bin/tailscale", "whois", "--json", "100.64.0.13"], self.system.ran)

    def test_someone_not_on_the_tailnet(self):
        status = self.sender(whois=None).status("127.0.0.1")
        self.assertIsNone(status["you"])
        self.assertFalse(any(t["you"] for t in status["targets"]))
        # Not an address at all (a test client, a unix socket): not even asked.
        self.sender().status("testclient")
        self.assertFalse(any(argv[1] == "whois" for argv in self.system.ran))

    def test_without_tailscale(self):
        status = self.sender(installed=False).status("100.64.0.13")
        self.assertEqual((status["supported"], status["targets"]), (False, []))
        self.assertIn("not installed", status["why"])

    def test_tailscaled_down_or_not_connected(self):
        down = self.sender(status=done(1, stderr=DAEMON_DOWN)).status()
        self.assertFalse(down["supported"])
        self.assertIn("tailscaled.service not running", down["why"])

        stopped = self.sender(status=done(stdout=json.dumps({"BackendState": "NeedsLogin"}))).status()
        self.assertIn("NeedsLogin", stopped["why"])

        off = self.sender(status=done(stdout=json.dumps({"BackendState": "Running", "Self": {"NoFileSharingReason": "Taildrop disabled by the admin"}}))).status()
        self.assertIn("Taildrop disabled by the admin", off["why"])

    def test_a_target_list_it_cannot_read(self):
        status = self.sender(targets=done(1, stderr="Access denied: access denied\n")).status()
        self.assertIn("Access denied", status["why"])


class PathCheck(Base):
    def test_asks_hermess_own_download_check_with_the_same_arguments(self):
        sender = self.sender()
        asked = f"{self.files_dir}/notes/../report.pdf"
        target, info = asyncio.run(sender.check(asked, "zz-scout", "20261017_a"))
        self.assertEqual(self.files.asked, [(asked, "zz-scout", "20261017_a")])
        self.assertEqual((target, info.st_ino), (self.report.resolve(), self.report.stat().st_ino))

    def test_refuses_what_hermes_refuses_in_its_words(self):
        files = FakeFiles(refuse={"/srv/.env": (403, "Access to sensitive files is not allowed"), "/nope": (404, "File not found")})
        sender = self.sender(files=files)
        self.assertEqual(self.refused(lambda: asyncio.run(sender.check("/srv/.env")), 403).detail, "Access to sensitive files is not allowed")
        self.assertEqual(self.refused(lambda: asyncio.run(sender.check("/nope")), 404).detail, "File not found")

    def test_hermess_sensitive_check_can_only_narrow(self):
        sender = self.sender(files=FakeFiles(sensitive=lambda path: path.name == "report.pdf"))
        self.refused(lambda: self.checked(sender), 403)

    def test_a_hermes_without_the_check_sends_nothing(self):
        sender = taildrop.Taildrop(self.folder, system=FakeTailscale(), files=lambda: None)
        refusal = self.refused(lambda: asyncio.run(sender.check(str(self.report))), 409)
        self.assertIn("0.21.5", refusal.detail)
        self.assertIsNone(taildrop.hermes_files())  # no Hermes in this process

    def test_a_link_swapped_in_after_the_check(self):
        sender = self.sender()
        target, info = self.checked(sender)
        other = self.files_dir / "other.txt"
        other.write_text("someone else's")
        os.unlink(target)
        os.symlink(other, target)
        self.assertIn("link", self.refused(lambda: sender.send("pixel-9", source=(target, info)), 409).detail)
        self.assertEqual(self.system.started, [])

    def test_a_different_file_swapped_in_after_the_check(self):
        sender = self.sender()
        target, info = self.checked(sender)
        os.unlink(target)
        target.write_bytes(b"replaced")
        self.assertIn("changed", self.refused(lambda: sender.send("pixel-9", source=(target, info)), 409).detail)


class Sending(Base):
    def test_sends_the_checked_file_itself_to_the_listed_address(self):
        sender = self.sender(stderr=(progress_line("report.pdf", "512.00B", "1.00KiB/s", 50.0) + progress_line("report.pdf", "1.06KiB", "1.00KiB/s", 100.0)).encode())
        started = sender.send("pixel-9", source=self.checked(sender))

        ((call,),) = [self.system.started]
        argv = call["argv"]
        self.assertEqual(argv[:4], ["/usr/bin/tailscale", "file", "cp", "--name=report.pdf"])
        self.assertRegex(argv[4], r"^/dev/fd/\d+$")
        self.assertEqual(argv[5], "100.64.0.13:")
        self.assertEqual(call["content"], self.report.read_bytes())
        self.assertEqual(started["state"], "running")

        view = sender.progress(started["id"])
        size = self.report.stat().st_size
        self.assertEqual((view["state"], view["sent_bytes"], view["total_bytes"], view["error"]), ("done", size, size, None))

    def test_follows_the_progress_while_it_runs(self):
        # A real background thread, and a CLI that has painted 25% and keeps running until it is stopped.
        system = FakeTailscale(stderr=progress_line("report.pdf", "272.00B", "100.00B/s", 25.0).encode(), blocking=True)
        sender = taildrop.Taildrop(self.folder, system=system, files=lambda: FakeFiles())
        started = sender.send("pixel-9", source=asyncio.run(sender.check(str(self.report))))
        size = self.report.stat().st_size

        view = wait_for(lambda: sender.progress(started["id"]), lambda view: view["sent_bytes"] > 0)
        self.assertEqual((view["state"], view["sent_bytes"], view["total_bytes"]), ("running", round(size * 0.25), size))

        sender.cancel(started["id"])
        self.assertEqual(wait_for(lambda: sender.progress(started["id"]), lambda view: view["state"] != "running")["state"], "cancelled")

    def test_the_target_must_be_one_taildrop_offers(self):
        sender = self.sender()

        for target in ["", "printer", "100.101.102.199", "pixel-9; id", "pixel-9:"]:
            self.refused(lambda: sender.send(target, source=self.checked(sender)), 422)

        self.assertEqual(self.system.started, [])
        # By address or by name, any case; the CLI only ever gets the listed address.
        sender.send("Lab-Box", source=self.checked(sender))
        self.assertEqual(self.system.started[-1]["argv"][-1], "[fd7a:115c:a1e0::5]:")

    def test_the_list_is_read_again_for_every_send(self):
        sender = self.sender()
        sender.send("pixel-9", source=self.checked(sender))
        self.system.answers[("file", "cp", "--targets")] = done(stdout="100.64.0.14\tworkstation\n")
        self.refused(lambda: sender.send("pixel-9", source=self.checked(sender)), 422)

    def test_a_patch_goes_as_a_temporary_file_that_is_deleted_after(self):
        sender = self.sender()
        seen = {}

        def look(argv):
            seen["folder"] = next(iter(sender._sends.values())).cleanup
            seen["exists"] = seen["folder"].is_dir()
            seen["listing"] = sorted(path.name for path in seen["folder"].iterdir())

        self.system.on_popen = look
        diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-old\n+new\n"
        started = sender.send("pixel-9", diff=diff, name="fix-login")

        call = self.system.started[-1]
        self.assertEqual(call["argv"][3], "--name=fix-login.patch")
        self.assertEqual(call["content"], diff.encode())
        self.assertEqual((seen["exists"], seen["listing"]), (True, ["fix-login.patch"]))
        self.assertFalse(seen["folder"].exists())
        self.assertEqual(sender.progress(started["id"])["kind"], "diff")

    def test_a_failed_patch_send_still_deletes_it(self):
        sender = self.sender(stderr=DENIED.encode(), code=1)
        sender.send("pixel-9", diff="+x\n", name="changes.patch")
        self.assertEqual(self.system.started[-1]["argv"][3], "--name=changes.patch")
        self.assertFalse(next(iter(sender._sends.values())).cleanup.exists())

    def test_a_file_or_a_patch(self):
        sender = self.sender()
        self.refused(lambda: sender.send("pixel-9"), 422)
        self.refused(lambda: sender.send("pixel-9", source=self.checked(sender), diff="x"), 422)

    def test_the_size_cap(self):
        sender = self.sender(environ={"HERMES_BOT_TAILDROP_MAX_BYTES": "100"})
        self.refused(lambda: sender.send("pixel-9", source=self.checked(sender)), 413)
        self.refused(lambda: sender.send("pixel-9", diff="x" * 101), 413)
        self.assertEqual(self.system.started, [])

    def test_a_denied_send_says_how_to_set_it_up(self):
        sender = self.sender(stderr=DENIED.encode(), code=1)
        started = sender.send("pixel-9", source=self.checked(sender))
        view = sender.progress(started["id"])
        self.assertEqual(view["state"], "failed")
        self.assertTrue(view["error"].startswith("Access denied: file access denied"))
        self.assertEqual(view["setup"]["command"], "sudo tailscale set --operator=hermes")
        self.assertEqual(sender.status()["setup"]["needed"], "operator")

        # Once a send gets through, the setup is no longer asked for.
        self.system.stderr, self.system.code = b"", 0
        sender.send("pixel-9", source=self.checked(sender))
        self.assertIsNone(sender.status()["setup"])

    def test_keeps_the_clis_words_and_its_warnings(self):
        stderr = "\r\x1b[K# warning: pixel-9 is reportedly offline; trying anyway\ncan't send to pixel-9: cannot send files: target is not advertising a file sharing API\n"
        sender = self.sender(stderr=stderr.encode(), code=1)
        view = sender.progress(sender.send("pixel-9", source=self.checked(sender))["id"])
        self.assertEqual(view["note"], "pixel-9 is reportedly offline; trying anyway")
        self.assertEqual(view["error"], "can't send to pixel-9: cannot send files: target is not advertising a file sharing API")
        self.assertIsNone(view["setup"])


class Limits(Base):
    FOUR = TARGETS + "100.101.102.105\ttablet\n"

    def test_one_per_device_and_three_at_once(self):
        sender = self.sender(deferred=True, targets=self.FOUR)
        sender.send("pixel-9", source=self.checked(sender))
        self.assertIn("already on its way to pixel-9", self.refused(lambda: sender.send("pixel-9", source=self.checked(sender)), 409).detail)
        sender.send("workstation", source=self.checked(sender))
        sender.send("lab-box", source=self.checked(sender))
        self.refused(lambda: sender.send("tablet", source=self.checked(sender)), 429)
        self.assertEqual(len(sender.status()["sending"]), 3)

        self.deferred.pop(0)()  # the first finishes
        sender.send("tablet", source=self.checked(sender))

    def test_cancel_before_the_cli_starts(self):
        sender = self.sender(deferred=True)
        started = sender.send("pixel-9", source=self.checked(sender))
        self.assertEqual(sender.cancel(started["id"]), {"cancelled": True, "id": started["id"]})
        self.deferred.pop()()
        self.assertEqual(self.system.started, [])
        self.assertEqual(sender.progress(started["id"])["state"], "cancelled")
        self.assertEqual(self.refused(lambda: sender.cancel(started["id"]), 409).detail, "That send has already cancelled.")
        self.refused(lambda: sender.cancel("0123456789abcdef"), 404)
        self.refused(lambda: sender.cancel("../etc"), 404)

    def test_cancel_terminates_the_cli(self):
        system = FakeTailscale(blocking=True)
        sender = taildrop.Taildrop(self.folder, system=system, files=lambda: FakeFiles())
        started = sender.send("pixel-9", source=asyncio.run(sender.check(str(self.report))))
        wait_for(lambda: getattr(system, "proc", None), lambda proc: proc is not None)

        self.assertEqual(sender.progress(started["id"])["state"], "running")
        sender.cancel(started["id"])
        self.assertTrue(system.proc.terminated.is_set())
        self.assertEqual(wait_for(lambda: sender.progress(started["id"]), lambda view: view["state"] != "running")["state"], "cancelled")

    def test_the_log_keeps_the_last_twenty_and_no_paths(self):
        sender = self.sender()
        ids = [sender.send("pixel-9", source=self.checked(sender))["id"] for _ in range(25)]
        rows = self.rows()
        self.assertEqual([row["id"] for row in rows], ids[-20:])
        self.assertEqual(set(rows[-1]), {"id", "requested_at", "finished_at", "target", "file", "kind", "bytes", "state", "error", "setup"})
        self.assertEqual((rows[-1]["file"], rows[-1]["target"], rows[-1]["state"]), ("report.pdf", "pixel-9", "done"))
        self.assertNotIn(str(self.files_dir), json.dumps(rows))
        self.assertEqual(oct(os.stat(self.folder / "taildrop.json").st_mode & 0o777), "0o600")
        # An older send is read back from the log, and memory keeps no more than the log does.
        self.assertLessEqual(len(sender._sends), 20)
        self.assertEqual(sender.progress(ids[-1])["state"], "done")

    def test_a_send_cut_short_by_a_restart(self):
        sender = self.sender(deferred=True)
        started = sender.send("pixel-9", source=self.checked(sender))
        later = taildrop.Taildrop(self.folder, system=FakeTailscale(), files=lambda: FakeFiles())
        view = later.progress(started["id"])
        self.assertEqual(view["state"], "failed")
        self.assertIn("restarted", view["error"])
        self.refused(lambda: later.progress("ffffffffffffffff"), 404)


class Routes(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.module, *_, patch = load_api(self.dir.name)
        patch.start()
        self.addCleanup(patch.stop)
        self.files_dir = Path(self.dir.name) / "files"
        self.files_dir.mkdir()
        self.report = self.files_dir / "report.pdf"
        self.report.write_bytes(b"%PDF-1.7 example\n")
        self.system = FakeTailscale()
        self.files = FakeFiles(refuse={str(self.files_dir / ".env"): (403, "Access to sensitive files is not allowed")})
        self.module._TAILDROP = self.module._taildrop().Taildrop(Path(self.dir.name) / "data", system=self.system, files=lambda: self.files, start=lambda target: target())
        app = FastAPI()
        app.include_router(self.module.router, prefix="/api/plugins/hermes-bot-mobile")
        self.client = TestClient(app, client=("100.64.0.13", 50000))
        self.url = "/api/plugins/hermes-bot-mobile/taildrop"

    def tearDown(self):
        self.module._TAILDROP = None
        self.dir.cleanup()

        for key in [k for k in sys.modules if k.startswith("_hermes_bot_mobile_package") or k.startswith("hermes_dashboard_plugin_hermes-bot-mobile-test")]:
            del sys.modules[key]

    def test_status_knows_the_device_asking(self):
        body = self.client.get(self.url).json()
        self.assertEqual((body["supported"], body["you"]["name"], body["max_bytes"]), (True, "pixel-9", 2 * 1024 ** 3))
        self.assertEqual([t["name"] for t in body["targets"] if t["you"]], ["pixel-9"])

    def test_send_follow_and_finish(self):
        response = self.client.post(self.url, json={"target": "pixel-9", "path": str(self.report), "profile": "zz-scout"})
        self.assertEqual(response.status_code, 202)
        send_id = response.json()["id"]
        self.assertEqual(self.files.asked, [(str(self.report), "zz-scout", None)])
        body = self.client.get(f"{self.url}/{send_id}").json()
        self.assertEqual((body["state"], body["name"], body["total_bytes"]), ("done", "report.pdf", self.report.stat().st_size))
        self.assertEqual(self.client.delete(f"{self.url}/{send_id}").status_code, 409)

    def test_send_a_patch(self):
        response = self.client.post(self.url, json={"target": "100.64.0.14", "diff": "+x\n", "name": "fix"})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.system.started[-1]["argv"][3], "--name=fix.patch")

    def test_refusals(self):
        sensitive = self.client.post(self.url, json={"target": "pixel-9", "path": str(self.files_dir / ".env")})
        self.assertEqual((sensitive.status_code, sensitive.json()["detail"]), (403, "Access to sensitive files is not allowed"))
        self.assertEqual(self.client.post(self.url, json={"target": "printer", "path": str(self.report)}).status_code, 422)
        self.assertEqual(self.client.post(self.url, json={"target": "pixel-9"}).status_code, 422)
        self.assertEqual(self.client.post(self.url, json={"target": "pixel-9", "path": str(self.report), "diff": "x"}).status_code, 422)

        for extra in [{"command": "id"}, {"argv": ["x"]}, {"device": "x"}]:
            self.assertEqual(self.client.post(self.url, json={"target": "pixel-9", "path": str(self.report), **extra}).status_code, 422, extra)

        self.assertEqual(self.system.started, [])

    def test_unknown_sends(self):
        self.assertEqual(self.client.get(f"{self.url}/0123456789abcdef").status_code, 404)
        self.assertEqual(self.client.get(f"{self.url}/nope").status_code, 404)
        self.assertEqual(self.client.delete(f"{self.url}/0123456789abcdef").status_code, 404)


def hermes_tree() -> Path | None:
    """A Hermes checkout with the download check this plugin reuses (0.21.5+): $HERMES_AGENT, else ~/.hermes/hermes-agent."""
    for candidate in [os.environ.get("HERMES_AGENT"), str(Path.home() / ".hermes" / "hermes-agent")]:
        if candidate and (Path(candidate) / "hermes_cli" / "web_routers" / "files.py").is_file():
            return Path(candidate)

    return None


@unittest.skipUnless(hermes_tree(), "needs a Hermes 0.21.5+ checkout (set HERMES_AGENT)")
class HermesParity(unittest.TestCase):
    """Against Hermes's real download route: the plugin's check refuses exactly what GET /api/fs/download refuses, with
    the same status and words, and allows exactly what it allows (Hermes's route has no root folder: it serves any
    readable regular file that is not a credential file, after resolving links). Runs in its own process so this one
    never imports Hermes."""

    def test_same_answers_as_hermess_download_route(self):
        script = Path(__file__).resolve().parent / "parity_taildrop.py"
        result = subprocess.run([sys.executable, str(script), str(hermes_tree())], capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        cases = json.loads(result.stdout.strip().splitlines()[-1])

        for case in cases:
            self.assertEqual(case["plugin"], case["hermes"], case["case"])

        answers = {case["case"]: case["hermes"] for case in cases}
        self.assertEqual(answers["a plain file"], {"ok": "ok.txt"})
        self.assertEqual(answers["traversal back to a plain file"], {"ok": "ok.txt"})
        self.assertEqual(answers["a link out of the folder"], {"ok": "elsewhere.txt"})
        self.assertEqual(answers["a known profile"], {"ok": "ok.txt"})

        for refused in ["an unknown profile", "an unknown chat"]:
            self.assertEqual(answers[refused]["status"], 404, refused)

        for refused in ["traversal to .env", ".env", "a link to .env", "a link into mcp-tokens", "config.yaml", "auth.json"]:
            self.assertEqual(answers[refused]["status"], 403, refused)

        for refused in ["a folder", "a fifo", "empty", "a NUL byte"]:
            self.assertEqual(answers[refused]["status"], 400, refused)

        self.assertEqual(answers["missing"]["status"], 404)


if __name__ == "__main__":
    unittest.main()
