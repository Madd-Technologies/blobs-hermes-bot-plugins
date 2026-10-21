import json
import os
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import inbox  # noqa: E402
import inbox_turn  # noqa: E402

ACK = json.dumps({"status": "queued", "delivery_id": "d1"})

CLI = "/srv/hermes/hermes-agent/.hermes/bin/hermes"
HOME = Path("/srv/hermes/profiles/juniper")


def make_modules(calls, live_home=None):
    """Stand in for tools.bot_mode_dm (its _start_delivery records what it was asked to run) and tools.bot_relay."""
    module = types.ModuleType("bot_mode_dm")
    module.__file__ = "/srv/hermes/hermes-agent/tools/bot_mode_dm.py"

    def _start_delivery(argv, content, label, *, stdin_file, task_id, agent, profile_home=None, author=None):
        calls.append(dict(argv=argv, content=content, label=label, stdin_file=stdin_file, task_id=task_id, agent=agent,
                          profile_home=profile_home, author=author))

        return ACK

    module._start_delivery = _start_delivery
    module._default_home = lambda: "/srv/hermes"
    # Hermes's own test: a hermes turn for a bot on the roster, nothing else.
    module._local_delivery_home = lambda argv: (live_home if live_home is not None else (
        HOME if len(argv) >= 3 and argv[0].endswith("hermes") and argv[1] == "-p" else None))

    relay = types.ModuleType("bot_relay")
    relay.BOT_CHAT_TURN_ARGS = ("chat", "--in", "~", "-c", "Bot Chat", "--create-if-missing", "-Q")
    relay.RESUME_UNANSWERED_TURN_ENV = "HERMES_RESUME_UNANSWERED_TURN"
    relay.acquire_turn_lock = lambda root, profile, timeout_seconds=None: None
    relay.delivery_env = lambda author, profile_home=None: {}
    relay.TurnBusyError = RuntimeError

    return module, relay


def teammate_argv(name="juniper", cli=CLI):
    return [cli, "-p", name, "chat", "--in", "~", "-c", "Bot Chat", "--create-if-missing", "-Q"]


ORION = {"id": "bot:orion", "name": "orion", "is_bot": True}


class SwitchTest(unittest.TestCase):
    def test_on_unless_switched_off(self):
        self.assertTrue(inbox.enabled({}))
        self.assertTrue(inbox.enabled({inbox.SWITCH: ""}))
        self.assertTrue(inbox.enabled({inbox.SWITCH: "on"}))
        self.assertTrue(inbox.enabled({inbox.SWITCH: "1"}))

        for value in ("0", "off", "OFF", " false ", "no"):
            self.assertFalse(inbox.enabled({inbox.SWITCH: value}), value)


class TeammateTest(unittest.TestCase):
    def test_a_local_teammate_delivery(self):
        self.assertEqual(inbox.teammate(teammate_argv(), False), "juniper")
        self.assertEqual(inbox.teammate(teammate_argv(cli="hermes"), False), "juniper")
        self.assertEqual(inbox.teammate(teammate_argv(cli="C:\\Hermes\\hermes.exe"), False), "juniper")

    def test_anything_else_is_not(self):
        for argv, stdin_file in (
            (teammate_argv(), True),
            ([CLI, "-p", "orion", "peer", "dm", "spark/harbor"], True),
            ([CLI, "-p", "orion", "peer", "dm", "spark/harbor"], False),
            (teammate_argv(cli="/usr/bin/python3"), False),
            (teammate_argv()[:-1], False),
            ([CLI, "-p", "juniper", "chat", "--in", "~", "-c", "Notes", "--create-if-missing", "-Q"], False),
            (["hermes", "--profile", "juniper", *inbox.BOT_CHAT_TURN_ARGS], False),
            ([CLI, "-p", "", *inbox.BOT_CHAT_TURN_ARGS], False),
            ("hermes -p juniper chat", False),
            (None, False),
        ):
            self.assertIsNone(inbox.teammate(argv, stdin_file), argv)


class InboxArgvTest(unittest.TestCase):
    def test_the_same_turn_in_the_inbox_through_the_runner(self):
        argv = inbox.inbox_argv(teammate_argv(), Path("/srv/hermes/hermes-agent"), HOME)

        self.assertEqual(argv[:2], [sys.executable, str(ROOT / "inbox_turn.py")])
        self.assertEqual(argv[2:], [
            "--hermes-agent", "/srv/hermes/hermes-agent", "--profile-home", str(HOME),
            CLI, "-p", "juniper", "chat", "--in", "~", "-c", "Bot Inbox", "--create-if-missing", "-Q",
        ])
        self.assertEqual(inbox_turn.split(argv[2:]), ("/srv/hermes/hermes-agent", str(HOME), argv[6:]))


class PatchTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.module, self.relay = make_modules(self.calls)
        self.assertTrue(inbox.patch_start_delivery(self.module, self.relay))

    def deliver(self, argv, *, stdin_file=False, profile_home=HOME, environ=None):
        with mock.patch.dict(os.environ, environ or {}, clear=False):
            if environ is None:
                os.environ.pop(inbox.SWITCH, None)

            return self.module._start_delivery(argv, "Message from 🤖 Orion (@orion): hi", "@juniper", stdin_file=stdin_file,
                                               task_id="t1", agent="agent", profile_home=profile_home, author=ORION)

    def unchanged(self, argv, *, stdin_file=False, profile_home=HOME):
        return dict(argv=argv, content="Message from 🤖 Orion (@orion): hi", label="@juniper", stdin_file=stdin_file,
                    task_id="t1", agent="agent", profile_home=profile_home, author=ORION)

    def test_a_teammate_message_runs_in_the_inbox(self):
        with self.assertLogs("hermes-bot-fixes", "INFO"):
            self.assertEqual(self.deliver(teammate_argv()), ACK)

        [call] = self.calls
        self.assertEqual(call["argv"], inbox.inbox_argv(teammate_argv(), Path("/srv/hermes/hermes-agent"), HOME))
        # No live admission into the main chat's mailbox, here or in the runner.
        self.assertIsNone(call["profile_home"])
        self.assertIsNone(self.module._local_delivery_home(call["argv"]))
        self.assertEqual({key: call[key] for key in ("content", "label", "stdin_file", "task_id", "agent", "author")},
                         {"content": "Message from 🤖 Orion (@orion): hi", "label": "@juniper", "stdin_file": False,
                          "task_id": "t1", "agent": "agent", "author": ORION})

    def test_switched_off_it_is_delivered_exactly_as_hermes_does(self):
        self.deliver(teammate_argv(), environ={inbox.SWITCH: "off"})

        self.assertEqual(self.calls, [self.unchanged(teammate_argv())])

    def test_peers_and_other_transports_are_left_alone(self):
        peer = [CLI, "-p", "orion", "peer", "dm", "spark/harbor"]
        self.deliver(peer, stdin_file=True, profile_home=None)
        self.deliver(teammate_argv(), profile_home=None)

        self.assertEqual(self.calls, [self.unchanged(peer, stdin_file=True, profile_home=None),
                                      self.unchanged(teammate_argv(), profile_home=None)])

    def test_a_runner_that_would_still_reach_the_main_chat_gets_hermess_delivery(self):
        calls = []
        module, relay = make_modules(calls, live_home=HOME)
        inbox.patch_start_delivery(module, relay)

        with self.assertLogs("hermes-bot-fixes", "WARNING"):
            module._start_delivery(teammate_argv(), "hi", "@juniper", stdin_file=False, task_id=None, agent=None,
                                   profile_home=HOME, author=ORION)

        self.assertEqual(calls[0]["argv"], teammate_argv())
        self.assertEqual(calls[0]["profile_home"], HOME)

    def test_a_failing_check_delivers_as_hermes_does(self):
        with mock.patch.object(inbox, "inbox_argv", side_effect=RuntimeError("no path")):
            with self.assertLogs("hermes-bot-fixes", "ERROR"):
                self.deliver(teammate_argv())

        self.assertEqual(self.calls, [self.unchanged(teammate_argv())])

    def test_applies_once(self):
        wrapped = self.module._start_delivery

        self.assertTrue(inbox.patch_start_delivery(self.module, self.relay))
        self.assertIs(self.module._start_delivery, wrapped)


class ShapeTest(unittest.TestCase):
    def assert_left_alone(self, module, relay):
        original = getattr(module, "_start_delivery", None)

        with self.assertLogs("hermes-bot-fixes", "WARNING"):
            self.assertFalse(inbox.patch_start_delivery(module, relay))

        self.assertIs(getattr(module, "_start_delivery", None), original)

    def test_other_parameters(self):
        module, relay = make_modules([])
        module._start_delivery = lambda argv, content, label, *, stdin_file, task_id, agent, profile_home=None: ACK
        self.assert_left_alone(module, relay)

    def test_another_main_chat_turn(self):
        module, relay = make_modules([])
        relay.BOT_CHAT_TURN_ARGS = ("chat", "-c", "Bot Chat", "-Q")
        self.assert_left_alone(module, relay)

    def test_missing_helpers(self):
        module, relay = make_modules([])
        del module._local_delivery_home
        self.assert_left_alone(module, relay)

        module, relay = make_modules([])
        del relay.acquire_turn_lock
        self.assert_left_alone(module, relay)

    def test_no_delivery_at_all(self):
        self.assert_left_alone(types.ModuleType("bot_mode_dm"), make_modules([])[1])


#: A Hermes checkout as far as inbox_turn.py reads it. delivery_env records what it was asked for, the lock is busy
#: while a "busy" file exists, and the "hermes" turn prints what it was run with.
FAKE_HERMES = {
    # This plugin's folder is never on the path when Hermes's modules load.
    "hermes_bootstrap.py": """
        import sys
        from pathlib import Path

        assert all(Path(entry or ".").resolve() != Path(PLUGIN) for entry in sys.path), sys.path
        assert Path(sys.path[0]).name == "hermes-agent", sys.path
        """,
    "agent/__init__.py": "",
    "agent/turn_author.py": """
        import json, os

        def turn_author_from_env(environ=os.environ):
            raw = environ.get("HERMES_TURN_AUTHOR")
            return json.loads(raw) if raw else None
        """,
    "tools/__init__.py": "",
    "tools/bot_failure_reasons.py": """
        def delivery_failure_reason(error):
            return getattr(error, "reason", "unknown")
        """,
    "tools/bot_mode_dm.py": """
        import os

        def _default_home():
            return os.environ["HERMES_HOME"]
        """,
    "tools/bot_mode_probe.py": """
        from pathlib import Path

        def _hermes_root(home):
            return Path(home)
        """,
    "tools/bot_relay.py": """
        import contextlib, json, os
        from pathlib import Path

        RESUME_UNANSWERED_TURN_ENV = "HERMES_RESUME_UNANSWERED_TURN"

        class TurnBusyError(RuntimeError):
            reason = "target_busy"

        @contextlib.contextmanager
        def acquire_turn_lock(root, profile, timeout_seconds=None):
            if (Path(root) / "busy").exists():
                raise TurnBusyError(f"target_busy: another delivery turn is already running for profile '{profile}'")
            (Path(root) / "locked").write_text(profile)
            yield
            (Path(root) / "locked").unlink()

        def delivery_env(author, profile_home=None):
            env = {key: value for key, value in os.environ.items() if key not in ("HERMES_TURN_AUTHOR", "SENDER_SECRET")}
            env["HERMES_HOME"] = str(profile_home)
            if author:
                env["HERMES_TURN_AUTHOR"] = json.dumps(author)
            return env
        """,
}

FAKE_TURN = """
import json, os, sys
from pathlib import Path

root = Path(os.environ["FAKE_ROOT"])
print(json.dumps({"argv": sys.argv[1:], "home": os.environ.get("HERMES_HOME"), "author": os.environ.get("HERMES_TURN_AUTHOR"),
                  "resume": os.environ.get("HERMES_RESUME_UNANSWERED_TURN"), "secret": os.environ.get("SENDER_SECRET"),
                  "locked": (root / "locked").read_text() if (root / "locked").exists() else None}))
print("turn stderr", file=sys.stderr)
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
"""


class InboxTurnTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="hermes-bot-fixes-inbox-"))
        self.agent_dir = self.root / "hermes-agent"

        for name, text in FAKE_HERMES.items():
            path = self.agent_dir / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(text).replace("PLUGIN", repr(str(ROOT))), encoding="utf-8")

        self.hermes = self.root / "hermes"
        self.hermes.write_text(f"#!{sys.executable}\n{FAKE_TURN}", encoding="utf-8")
        self.hermes.chmod(0o755)

    def run_turn(self, *, args=None, **env):
        args = args or ["--hermes-agent", str(self.agent_dir), "--profile-home", str(self.root / "profiles" / "juniper"),
                        str(self.hermes), "-p", "juniper", *inbox.INBOX_TURN_ARGS, "--query-file", "/tmp/dm"]
        base = {key: value for key, value in os.environ.items() if not key.startswith("HERMES_")}
        base.update(HERMES_HOME=str(self.root), FAKE_ROOT=str(self.root), SENDER_SECRET="sender's key",
                    HERMES_TURN_AUTHOR=json.dumps(ORION), **env)

        return subprocess.run([sys.executable, str(ROOT / "inbox_turn.py"), *args], capture_output=True, text=True,
                              env=base, cwd=str(ROOT), check=False)

    def test_runs_the_inbox_turn_under_the_bots_lock_with_its_own_environment(self):
        proc = self.run_turn()

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, "turn stderr\n")
        seen = json.loads(proc.stdout)
        self.assertEqual(seen["argv"], ["-p", "juniper", *inbox.INBOX_TURN_ARGS, "--query-file", "/tmp/dm"])
        self.assertEqual(seen["home"], str(self.root / "profiles" / "juniper"))
        self.assertEqual(json.loads(seen["author"]), ORION)
        self.assertIsNone(seen["secret"])
        self.assertIsNone(seen["resume"])
        self.assertEqual(seen["locked"], "juniper")
        self.assertFalse((self.root / "locked").exists())

    def test_the_turns_exit_code_and_the_retry_marker_pass_through(self):
        proc = self.run_turn(FAKE_EXIT="3", HERMES_RESUME_UNANSWERED_TURN="1")

        self.assertEqual(proc.returncode, 3)
        self.assertEqual(json.loads(proc.stdout)["resume"], "1")

    def test_a_busy_bot_is_refused_as_the_runner_refuses_it(self):
        (self.root / "busy").write_text("")
        proc = self.run_turn()

        self.assertEqual(proc.returncode, 1)
        self.assertEqual(json.loads(proc.stdout), {
            "error": "target_busy: another delivery turn is already running for profile 'juniper'", "reason": "target_busy"})

    def test_arguments_it_does_not_understand(self):
        proc = self.run_turn(args=["--profile-home", "/x", "hermes", "-p", "juniper", "chat"])

        self.assertEqual(proc.returncode, 2)
        self.assertIn("NOT delivered", json.loads(proc.stdout)["error"])

    def test_split(self):
        self.assertIsNone(inbox_turn.split([]))
        self.assertIsNone(inbox_turn.split(["--hermes-agent", "a", "--profile-home", "h", "hermes", "-p", ""]))
        self.assertIsNone(inbox_turn.split(["--hermes-agent", "a", "--profile-home", "h", "hermes", "--profile", "x"]))
        self.assertEqual(inbox_turn.split(["--hermes-agent", "a", "--profile-home", "h", "hermes", "-p", "x"]),
                         ("a", "h", ["hermes", "-p", "x"]))


def make_022_modules(calls):
    """Hermes 0.22: the same _start_delivery, but bot_relay has no one-shot Bot Chat turn any more."""
    module, _ = make_modules(calls)
    relay = types.ModuleType("bot_relay")

    return module, relay


class ThroughTheGateway(unittest.TestCase):
    """On Hermes 0.22 a teammate's message goes to the recipient's inbox through the gateway's bot-to-bot door."""

    def setUp(self):
        self.calls = []
        self.module, self.relay = make_022_modules(self.calls)
        patcher = mock.patch.object(inbox, "a2a_available", lambda: True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertTrue(inbox.patch_start_delivery(self.module, self.relay))

    def send(self, argv, **kw):
        args = dict(stdin_file=False, task_id="t1", agent=None, profile_home=HOME, author=ORION)
        args.update(kw)

        return self.module._start_delivery(argv, "Message from 🤖 orion (@orion): is the build green?", "juniper", **args)

    def test_a_local_teammate_message_runs_in_the_inbox(self):
        self.send([CLI, "-p", "juniper"])
        (call,) = self.calls
        self.assertTrue(call["stdin_file"])
        self.assertIsNone(call["profile_home"])
        self.assertEqual(call["argv"][1], str(inbox.A2A_TURN))
        self.assertEqual(call["argv"][call["argv"].index("--recipient") + 1], "juniper")
        self.assertEqual(call["argv"][call["argv"].index("--peer") + 1], "bot:orion")
        self.assertEqual(call["argv"][call["argv"].index("--profile-home") + 1], str(HOME.resolve()))
        self.assertEqual(call["author"], ORION)

    def test_peers_stdin_transports_and_the_switch_are_left_alone(self):
        self.send(["ssh", "peer", "hermes"], stdin_file=True, profile_home=None)
        self.send([CLI, "-p", "juniper"], profile_home=None)

        with mock.patch.dict(os.environ, {inbox.SWITCH: "off"}):
            self.send([CLI, "-p", "juniper"])

        self.assertEqual([c["argv"][0] for c in self.calls], ["ssh", CLI, CLI])
        self.assertEqual([c["profile_home"] for c in self.calls], [None, None, HOME])

    def test_a_hermes_without_either_way_is_left_alone(self):
        calls = []
        module, relay = make_022_modules(calls)

        with mock.patch.object(inbox, "a2a_available", lambda: False):
            self.assertFalse(inbox.patch_start_delivery(module, relay))


class InboxTransport(unittest.TestCase):
    """inbox_a2a.py: one message in, the recipient's reply out."""

    def setUp(self):
        import inbox_a2a

        self.inbox_a2a = inbox_a2a
        self.forwarded = []
        gateway = types.ModuleType("gateway")
        a2a = types.ModuleType("gateway.session_a2a")

        async def forward_to_owner(home, **kw):
            self.forwarded.append(dict(home=home, **kw))

            return {"status": "terminal", "result": {"final_response": "Yes, all green."}}

        a2a.forward_to_owner = forward_to_owner
        patcher = mock.patch.dict(sys.modules, {"gateway": gateway, "gateway.session_a2a": a2a})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_it(self, text):
        import io
        from contextlib import redirect_stdout

        out = io.StringIO()

        with redirect_stdout(out):
            code = self.inbox_a2a.main(["--hermes-agent", "/srv/hermes/hermes-agent", "--profile-home", str(HOME), "--recipient", "juniper", "--peer", "bot:orion"], io.StringIO(text))

        return code, out.getvalue().strip()

    def test_it_forwards_into_the_senders_inbox_and_prints_the_reply(self):
        self.assertEqual(self.run_it("is the build green?"), (0, "Yes, all green."))
        (sent,) = self.forwarded
        self.assertEqual((sent["agent"], sent["peer"], sent["context_id"], sent["tenant"]), ("juniper", "bot:orion", "bot-inbox", ""))
        self.assertEqual(sent["text"], "is the build green?")

    def test_an_empty_message_is_not_sent(self):
        self.assertEqual(self.run_it("  ")[0], 1)
        self.assertEqual(self.forwarded, [])

    def test_a_reply_less_turn_says_so(self):
        self.assertIn("without a reply", self.inbox_a2a.reply_of({"result": {}}))


if __name__ == "__main__":
    unittest.main()
