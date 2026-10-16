import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import helpers  # noqa: F401  (puts the plugin on sys.path)
import runs_on
from test_api import load_api


def ran(returncode=0, stdout="", stderr=""):
    """A fake `subprocess.run` that records the command it was given."""
    calls = []

    def run(command, **options):
        calls.append((command, options))

        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    run.calls = calls

    return run


class Targets(unittest.TestCase):
    def test_takes_host_names_addresses_and_users(self):
        self.assertEqual(runs_on.check_target("vps-1.example.net", "scout", 22), ("vps-1.example.net", "scout", 22))
        self.assertEqual(runs_on.check_target(" 100.64.0.7 ", "ubuntu", 2222), ("100.64.0.7", "ubuntu", 2222))
        self.assertEqual(runs_on.check_target("[fd7a:115c::1]", "root", 22), ("fd7a:115c::1", "root", 22))

    def test_refuses_anything_ssh_could_read_as_an_option_or_a_second_target(self):
        for host in ["-oProxyCommand=touch /tmp/x", "a b", "host;id", "user@host", "", "a..b", "$(id)"]:
            with self.assertRaises(runs_on.Invalid, msg=host):
                runs_on.check_target(host, "scout", 22)

        for user in ["-l", "a b", "root@x", "", "x" * 40]:
            with self.assertRaises(runs_on.Invalid, msg=user):
                runs_on.check_target("vps", user, 22)

        for port in [0, 65536, "22", True]:
            with self.assertRaises(runs_on.Invalid, msg=repr(port)):
                runs_on.check_target("vps", "scout", port)

    def test_profile_names_follow_hermes(self):
        self.assertEqual(runs_on.check_profile("zz-scout_2"), "zz-scout_2")

        for bad in ["", "Scout", "../default", "-x", "a" * 65]:
            with self.assertRaises(runs_on.Invalid, msg=bad):
                runs_on.check_profile(bad)


class Check(unittest.TestCase):
    def test_connects_with_the_same_options_as_hermes_and_reports_the_system(self):
        run = ran(stdout="Linux 6.8.0-45-generic x86_64\n")
        result = runs_on.check_ssh("vps-1.example.net", "scout", 2222, "", run=run)

        self.assertEqual(result, {"ok": True, "system": "Linux 6.8.0-45-generic x86_64"})
        command, options = run.calls[0]
        self.assertEqual(command[0], "ssh")
        self.assertIn("BatchMode=yes", command)
        self.assertIn("StrictHostKeyChecking=accept-new", command)
        self.assertEqual(command[-3:], ["--", "scout@vps-1.example.net", "uname -srm"])
        self.assertEqual(command[command.index("-p") + 1], "2222")
        self.assertNotIn("-i", command)
        self.assertIs(options["stdin"], subprocess.DEVNULL)

    def test_passes_the_bots_key(self):
        with tempfile.NamedTemporaryFile() as key:
            run = ran(stdout="Linux")
            runs_on.check_ssh("vps", "scout", 22, key.name, run=run)
            command = run.calls[0][0]
            self.assertEqual(command[command.index("-i") + 1], key.name)

    def test_a_missing_or_relative_key(self):
        self.assertEqual(runs_on.check_ssh("vps", "scout", 22, "/nowhere/id_ed25519", run=ran())["reason"], "key_missing")

        with self.assertRaises(runs_on.Invalid):
            runs_on.check_ssh("vps", "scout", 22, "id_ed25519", run=ran())

        with self.assertRaises(runs_on.Invalid):
            runs_on.check_ssh("vps", "scout", 22, "-oProxyCommand=x", run=ran())

    def test_says_why_it_failed(self):
        cases = {
            "scout@vps: Permission denied (publickey).": "auth",
            "Received disconnect from 10.0.0.2 port 22:2: Too many authentication failures": "too_many_keys",
            "Host key verification failed.": "host_key",
            "ssh: Could not resolve hostname vps: nodename nor servname provided, or not known": "unknown_host",
            "ssh: connect to host vps port 22: Connection refused": "refused",
            "ssh: connect to host vps port 22: Operation timed out": "timeout",
            "ssh: connect to host vps port 22: No route to host": "unreachable",
            "kex_exchange_identification: read: Connection reset by peer": "other",
        }

        for stderr, reason in cases.items():
            result = runs_on.check_ssh("vps", "scout", 22, run=ran(returncode=255, stderr=f"Warning: noise\n{stderr}\n"))
            self.assertEqual((result["ok"], result["reason"], result["detail"]), (False, reason, stderr), stderr)

    def test_no_ssh_and_no_answer(self):
        def missing(*_, **__):
            raise FileNotFoundError("ssh")

        def slow(*_, **__):
            raise subprocess.TimeoutExpired("ssh", 25)

        self.assertEqual(runs_on.check_ssh("vps", "scout", 22, run=missing)["reason"], "no_ssh")
        self.assertEqual(runs_on.check_ssh("vps", "scout", 22, run=slow)["reason"], "timeout")

    def test_docker(self):
        self.assertEqual(runs_on.check_docker(run=ran(stdout="27.3.1\n")), {"ok": True, "system": "Docker 27.3.1"})
        down = runs_on.check_docker(run=ran(returncode=1, stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock."))
        self.assertEqual(down["reason"], "docker_down")

        def missing(*_, **__):
            raise FileNotFoundError("docker")

        self.assertEqual(runs_on.check_docker(run=missing)["reason"], "no_docker")


@unittest.skipUnless(shutil.which("ssh-keygen"), "needs ssh-keygen")
class KeyPairs(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.keys = runs_on.Keys(Path(self.dir.name) / "ssh")

    def tearDown(self):
        self.dir.cleanup()

    def test_makes_one_private_key_per_bot_and_hands_out_only_the_public_line(self):
        self.assertIsNone(self.keys.public("zz-scout"))
        made = self.keys.make("zz-scout")

        self.assertTrue(made["created"])
        self.assertTrue(made["public"].startswith("ssh-ed25519 "))
        self.assertTrue(made["public"].endswith(" blobs-zz-scout"))
        self.assertNotIn("PRIVATE", made["public"])
        private = Path(made["path"])
        self.assertEqual(private, Path(self.dir.name) / "ssh" / "zz-scout" / "id_ed25519")
        self.assertEqual(stat.S_IMODE(os.stat(private).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(private.parent).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(private.parent.parent).st_mode), 0o700)
        self.assertEqual(sorted(p.name for p in private.parent.iterdir()), ["id_ed25519", "id_ed25519.pub"])

    def test_making_again_keeps_the_same_key(self):
        first = self.keys.make("zz-scout")
        second = self.keys.make("zz-scout")

        self.assertFalse(second["created"])
        self.assertEqual(first["public"], second["public"])
        self.assertNotEqual(self.keys.make("zz-other")["public"], first["public"])


class Routes(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.module, *_, patch = load_api(self.dir.name)
        patch.start()
        self.addCleanup(patch.stop)
        bots = mock.patch.object(self.module, "_profile_exists", lambda name: name in {"default", "zz-scout"})
        bots.start()
        self.addCleanup(bots.stop)
        app = FastAPI()
        app.include_router(self.module.router, prefix="/api/plugins/hermes-bot-mobile")
        self.client = TestClient(app)
        self.base = "/api/plugins/hermes-bot-mobile/runs-on"

    def tearDown(self):
        self.dir.cleanup()

    @unittest.skipUnless(shutil.which("ssh-keygen"), "needs ssh-keygen")
    def test_key_before_and_after_it_is_made(self):
        before = self.client.get(f"{self.base}/key", params={"profile": "zz-scout"})
        self.assertEqual(before.status_code, 200)
        self.assertIsNone(before.json()["public"])

        made = self.client.post(f"{self.base}/key", json={"profile": "zz-scout"}).json()
        self.assertTrue(made["created"])
        self.assertTrue(made["path"].startswith(self.dir.name))
        self.assertEqual(self.client.get(f"{self.base}/key", params={"profile": "zz-scout"}).json()["public"], made["public"])

    def test_only_for_bots_the_server_has(self):
        self.assertEqual(self.client.get(f"{self.base}/key", params={"profile": "zz-nobody"}).status_code, 404)
        self.assertEqual(self.client.post(f"{self.base}/key", json={"profile": "../default"}).status_code, 422)
        self.assertEqual(self.client.post(f"{self.base}/check", json={"profile": "zz-nobody", "host": "vps", "user": "scout"}).status_code, 404)

    def test_check_refuses_options_and_unknown_backends(self):
        bad = self.client.post(f"{self.base}/check", json={"profile": "zz-scout", "host": "-oProxyCommand=id", "user": "scout"})
        self.assertEqual(bad.status_code, 422)
        other = self.client.post(f"{self.base}/check", json={"profile": "zz-scout", "backend": "modal"})
        self.assertEqual(other.status_code, 422)

    def test_check_reports_what_ssh_said(self):
        runs = self.module._runs_on()

        with mock.patch.object(runs.subprocess, "run", ran(returncode=255, stderr="scout@vps: Permission denied (publickey).")):
            body = self.client.post(f"{self.base}/check", json={"profile": "zz-scout", "host": "vps", "user": "scout", "port": 22}).json()

        self.assertEqual((body["ok"], body["reason"]), (False, "auth"))


if __name__ == "__main__":
    unittest.main()
