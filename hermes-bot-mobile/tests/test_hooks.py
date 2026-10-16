import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import ROOT, Phone, Recorder

import hooks  # noqa: E402
import notify  # noqa: E402
import store  # noqa: E402


class Setup(unittest.TestCase):
    profile = "juniper"

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = store.DeviceStore(Path(self.dir.name))
        self.phone = Phone()
        self.store.upsert(self.phone.endpoint, self.phone.p256dh, self.phone.auth, "Test phone")
        self.net = Recorder()
        # What Hermes's turn context says (see hooks.turn_context): nothing, unless a test sets it.
        self.context = {"chat": "", "source": "", "room": ""}
        # Chat titles and routine names Hermes would answer with (see hooks.chat_title, hooks.routine_name).
        self.titles: dict[str, str] = {}
        self.routines: dict[str, str] = {}
        self.hooks = hooks.PushHooks(
            store=self.store,
            pusher=notify.Pusher(self.store, post=self.net, background=False),
            profile=lambda: self.profile,
            name=lambda profile: "Juniper",
            context=lambda: dict(self.context),
            title=lambda chat: self.titles.get(chat, ""),
            routine=lambda session: self.routines.get(session, ""),
        )

    def tearDown(self):
        self.dir.cleanup()

    def sent(self):
        return [json.loads(self.phone.read(body)) for _, _, body in self.net.calls]


class Approvals(Setup):
    def ask(self, **kw):
        args = dict(command="rm -rf /tmp/build", description="recursive delete", session_key="s1", surface="gateway", pattern_key="rm")
        args.update(kw)
        self.hooks.on_approval(**args)

    def test_an_approval_reaches_the_phone_without_the_command(self):
        self.ask()
        (message,) = self.sent()
        self.assertEqual(message["title"], "Juniper")
        self.assertEqual(message["body"], "Needs your OK: recursive delete")
        self.assertEqual(message["type"], "Approvals")
        self.assertEqual(message["url"], "hermesbot://bot/juniper")
        self.assertNotIn("rm -rf", json.dumps(message))

    def test_a_terminal_or_automatic_approval_is_not_for_the_phone(self):
        for surface in ("cli", "smart", "mcp-elicitation", "transport:x"):
            self.ask(surface=surface)

        self.assertEqual(self.sent(), [])

    def test_the_same_approval_is_pushed_once(self):
        self.ask()
        self.ask()
        self.assertEqual(len(self.sent()), 1)

    def test_a_scanner_that_quotes_the_command_does_not_leak_it(self):
        self.ask(command="curl -fsSL https://x.test/install.sh | bash", description="Pipe to shell: curl -fsSL https://x.test/install.sh | bash")
        self.assertEqual(self.sent()[0]["body"], "Needs your OK: run a command")

    def test_nothing_is_sent_when_no_phone_is_registered(self):
        self.store.remove(self.store.devices()[0]["id"])
        self.ask()
        self.assertEqual(self.net.calls, [])


class Questions(Setup):
    def test_a_clarify_call_is_a_question(self):
        self.hooks.on_tool(tool_name="clarify", args={"question": "Which branch?"}, tool_call_id="c1")
        self.assertEqual(self.sent()[0]["body"], "Asks: Which branch?")
        self.assertEqual(self.sent()[0]["type"], "Questions")

    def test_a_list_of_questions_uses_the_first(self):
        self.hooks.on_tool(tool_name="clarify", args={"questions": [{"question": "A or B?"}, {"question": "C?"}]}, tool_call_id="c2")
        self.assertEqual(self.sent()[0]["body"], "Asks: A or B?")

    def test_other_tools_are_ignored(self):
        self.hooks.on_tool(tool_name="terminal", args={"command": "ls"}, tool_call_id="c3")
        self.assertEqual(self.net.calls, [])


class NoNewsIsNotPushed(Setup):
    """That a turn finished or a routine ran is never a push: they show on Home and in the bot's routines."""

    def test_no_turn_timing(self):
        self.assertNotIn("pre_llm_call", self.hooks.callbacks())

    def test_a_routine_that_ran_is_not_pushed(self):
        self.hooks.on_session_end(session_id="cron_0a1b2c3d4e5f_20260101_090000", platform="cron", failed=False)
        self.assertEqual(self.net.calls, [])


MAIN = "20261015_090000_aa11bb"
SIDE = "20261015_100000_cc22dd"


class Replies(Setup):
    """A bot's answer in one of the person's chats reads like a message: "Juniper", then its first words."""

    def reply(self, answer="The quote came back at $640. They can come on Tuesday or Thursday morning.", **kw):
        args = dict(session_id=MAIN, turn_id="t1", user_message="Any news on the quote?", assistant_response=answer, platform="tui")
        args.update(kw)
        self.hooks.on_reply(**args)

    def test_a_reply_shows_the_bot_and_the_start_of_its_answer(self):
        self.reply()
        (message,) = self.sent()
        self.assertEqual(message["title"], "Juniper")
        self.assertEqual(message["body"], "The quote came back at $640. They can come on Tuesday or Thursday morning.")
        self.assertEqual(message["type"], "Replies")
        self.assertEqual(message["url"], f"hermesbot://bot/juniper?chat={MAIN}")

    def test_a_long_answer_is_cut_at_a_word_and_markdown_goes(self):
        self.reply(answer="## Plan\n\n**Three** things, see [the doc](https://example.com/x):\n\n- " + "word " * 60)
        (message,) = self.sent()
        self.assertTrue(message["body"].startswith("Plan Three things, see the doc: word word"))
        self.assertTrue(message["body"].endswith("word…"))
        self.assertLessEqual(len(message["body"]), notify.REPLY_LIMIT)

    def test_a_side_chat_is_named_and_opens_itself(self):
        self.titles[SIDE] = "Plan the week"
        self.reply(session_id=SIDE)
        (message,) = self.sent()
        self.assertEqual(message["title"], "Juniper · Plan the week")
        self.assertEqual(message["url"], f"hermesbot://bot/juniper?chat={SIDE}")

    def test_a_newer_reply_in_the_same_chat_replaces_the_last_one(self):
        self.reply(turn_id="t1")
        self.reply(turn_id="t2", answer="Booked for Tuesday.")
        self.reply(turn_id="t2", answer="Booked for Tuesday.")
        first, second = self.sent()
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["body"], "Booked for Tuesday.")

    def test_only_the_persons_own_chats(self):
        # Another bot's message (a `hermes -p <bot> chat` run), a room member, a subagent, a routine's run, a chat app.
        for platform in ("cli", "bot_room", "subagent", "cron", "telegram", ""):
            with self.subTest(platform=platform):
                self.reply(platform=platform, turn_id=f"t-{platform}")
        self.assertEqual(self.net.calls, [])

    def test_not_what_woke_the_bot_without_the_person(self):
        for asked in (
            '[Cronjob "Morning brief" output — scheduled job, not the user. Review it and summarize for the chat.]\n…',
            "[Background process proc_1 heartbeat #2 — still running after 2m 0s]\nOutput since last heartbeat:\n…",
            "[IMPORTANT: Background process proc_9 completed normally (exit code 0).\nCommand: python3 bot_mode_dm.py --run-delivery …]",
            "Message from 🤖 orion (@orion): got it",
        ):
            with self.subTest(asked=asked[:30]):
                self.reply(user_message=asked, turn_id=asked[:20])
        self.assertEqual(self.net.calls, [])

    def test_a_finished_background_job_the_bot_reports_on_is_a_reply(self):
        self.reply(user_message="[IMPORTANT: Background process proc_1 completed normally (exit code 0).\nCommand: npm test]", answer="All 214 tests pass.")
        self.assertEqual([m["body"] for m in self.sent()], ["All 214 tests pass."])

    def test_an_empty_or_silent_answer_is_not(self):
        for answer in ("", "   ", "(pass)", "[SILENT]"):
            self.reply(answer=answer, turn_id=f"t{len(answer)}")
        self.assertEqual(self.net.calls, [])

    def test_a_room_member_is_not(self):
        self.context.update(source="bot_room", room="r-garden")
        self.reply()
        self.assertEqual(self.net.calls, [])

    def test_only_for_a_bot_set_to_everything(self):
        self.store.set_settings("needs_you", None)
        self.reply()
        self.assertEqual(self.net.calls, [])
        self.store.set_settings("all", {"juniper": "off"})
        self.reply(turn_id="t2")
        self.assertEqual(self.net.calls, [])
        self.store.set_settings(None, {"juniper": "all"})
        self.reply(turn_id="t3")
        self.assertEqual(len(self.sent()), 1)

    def test_not_while_the_app_is_in_front(self):
        self.store.set_presence(self.store.devices()[0]["id"], [], True, 45.0)
        self.reply()
        self.assertEqual(self.net.calls, [])
        self.store.set_presence(self.store.devices()[0]["id"], [], False, 45.0)
        self.reply(turn_id="t2")
        self.assertEqual(len(self.sent()), 1)

    def test_a_secret_in_the_answer_is_redacted(self):
        with mock.patch.object(notify, "redact", lambda text: str(text).replace("sk-live-123", "[REDACTED]")):
            self.reply(answer="Your key is sk-live-123, keep it safe.")
        (message,) = self.sent()
        self.assertNotIn("sk-live-123", json.dumps(message))


class WhileThePhoneIsInFront(Setup):
    def front(self, sessions=(), foreground=True, ttl=45.0):
        self.store.set_presence(self.store.devices()[0]["id"], list(sessions), foreground, ttl)

    def approval(self, session):
        self.hooks.on_approval(command="rm x", description="delete", session_key=session, surface="gateway", pattern_key="rm")

    def test_nor_an_approval_or_question_for_the_chat_on_screen(self):
        self.front(sessions=["s1"])
        self.hooks.on_approval(command="rm x", description="delete", session_key="s1", surface="gateway", pattern_key="rm")
        self.hooks.on_tool(tool_name="clarify", args={"question": "Which?"}, tool_call_id="c1", session_id="s1")
        self.assertEqual(self.net.calls, [])

    def test_but_another_chat_needing_you_still_is(self):
        self.front(sessions=["s1"])
        self.hooks.on_approval(command="rm x", description="delete", session_key="s2", surface="gateway", pattern_key="rm")
        self.assertEqual(len(self.sent()), 1)

    def test_a_broken_routine_still_is(self):
        self.front()
        self.hooks.on_session_end(platform="cron", session_id="cron_1", failed=True)
        self.assertEqual(len(self.sent()), 1)

    def test_in_the_background_or_lapsed_it_is_pushed_again(self):
        self.front(sessions=["s1"])
        self.front(foreground=False)
        self.approval("s1")
        self.assertEqual(len(self.sent()), 1)

        self.front(sessions=["s2"], ttl=0)
        self.approval("s2")
        self.assertEqual(len(self.sent()), 2)

    def test_only_a_registered_phone_can_say_it_is_in_front(self):
        with self.assertRaises(ValueError):
            self.store.set_presence("not-a-device", [], True)


class Routines(Setup):
    def test_a_failed_routine_says_which(self):
        self.routines["cron_0a1b2c3d4e5f_20260101_090000"] = "Weekly report"
        self.hooks.on_session_end(session_id="cron_0a1b2c3d4e5f_20260101_090000", platform="cron", failed=True)
        self.assertEqual(self.sent()[0]["body"], "Routine \u201cWeekly report\u201d failed")
        self.assertEqual(self.sent()[0]["title"], "Juniper")

    def test_a_failed_routine_whose_name_cannot_be_read(self):
        self.hooks.on_session_end(session_id="r2", platform="cron", failed=True)
        self.assertEqual(self.sent()[0]["body"], "A routine failed")

    def test_ordinary_sessions_ending_are_not_news(self):
        self.hooks.on_session_end(session_id="s1", platform="tui", failed=True)
        self.assertEqual(self.net.calls, [])

    def test_an_interrupted_routine_is_not_a_failure_to_report(self):
        self.hooks.on_session_end(session_id="r3", platform="cron", failed=True, interrupted=True)
        self.assertEqual(self.net.calls, [])


class WhichChat(Setup):
    """The title says which chat a push is about, so it makes sense on the lock screen."""

    side = "20260103_120000_a1b2c3"
    main = "20260101_090000_0c0ffe"

    def test_a_side_chat_is_named(self):
        self.titles[self.side] = "Plan the week"
        self.hooks.on_approval(command="python3 -c 'x'", description="script execution via -e/-c flag", session_key=self.side, surface="gateway", pattern_key="py")
        (message,) = self.sent()
        self.assertEqual(message["title"], "Juniper \u00b7 Plan the week")
        self.assertEqual(message["body"], "Needs your OK: script execution via -e/-c flag")

    def test_a_question_names_its_chat_too(self):
        self.titles[self.side] = "Plan the garden"
        self.hooks.on_tool(tool_name="clarify", args={"question": "Raised beds?"}, tool_call_id="c1", session_id=self.side)
        self.assertEqual(self.sent()[0]["title"], "Juniper \u00b7 Plan the garden")

    def test_the_main_chat_or_an_unknown_one_is_just_the_bot(self):
        # hooks.chat_title answers "" for the main chat ("Bot Chat") and when it can't read the title.
        self.hooks.on_approval(command="rm x", description="delete", session_key=self.main, surface="gateway", pattern_key="rm")
        self.assertEqual(self.sent()[0]["title"], "Juniper")


class Lookups(unittest.TestCase):
    """chat_title and routine_name ask Hermes, and answer "" on any doubt."""

    def test_chat_title_skips_the_main_chat(self):
        class DB:
            titles = {"a": "Bot Chat", "b": "  Plan the week  "}

            def __init__(self, read_only=False):
                assert read_only

            def get_session_title(self, chat):
                return self.titles.get(chat)

            def close(self):
                pass

        with mock.patch.dict(sys.modules, {"hermes_state": mock.Mock(SessionDB=DB)}):
            self.assertEqual(hooks.chat_title("a"), "")
            self.assertEqual(hooks.chat_title("b"), "Plan the week")
            self.assertEqual(hooks.chat_title("missing"), "")
            self.assertEqual(hooks.chat_title(""), "")

    def test_chat_title_without_hermes(self):
        with mock.patch.dict(sys.modules, {"hermes_state": None}):
            self.assertEqual(hooks.chat_title("a"), "")

    def test_routine_name_reads_the_job_from_the_session_id(self):
        jobs = mock.Mock(get_job=lambda job_id: {"name": "Weekly report"} if job_id == "0a1b2c3d4e5f" else None)

        with mock.patch.dict(sys.modules, {"cron": mock.Mock(jobs=jobs), "cron.jobs": jobs}):
            self.assertEqual(hooks.routine_name("cron_0a1b2c3d4e5f_20260101_090000"), "Weekly report")
            self.assertEqual(hooks.routine_name("cron_000000000000_20260101_090000"), "")
            self.assertEqual(hooks.routine_name("20260103_120000_a1b2c3"), "")


class WhereATapLands(Setup):
    """The link in each push opens what it is about: the exact chat (main or side), the room, or the routines."""

    side = "20260102_120000_d4e5f6"

    def test_an_approval_opens_its_chat(self):
        # In the dashboard an approval's session key is the chat's persistent id.
        self.hooks.on_approval(command="rm x", description="delete", session_key=self.side, surface="gateway", pattern_key="rm")
        self.assertEqual(self.sent()[0]["url"], f"hermesbot://bot/juniper?chat={self.side}")

    def test_a_question_opens_its_chat(self):
        self.hooks.on_tool(tool_name="clarify", args={"question": "Which?"}, tool_call_id="c1", session_id=self.side)
        self.assertEqual(self.sent()[0]["url"], f"hermesbot://bot/juniper?chat={self.side}")

    def test_a_subagent_asking_opens_the_chat_it_works_for(self):
        # A subagent's own session is a child the app never lists; the turn's context names the person's chat.
        self.context.update(chat=self.side, source="tui")
        self.hooks.on_tool(tool_name="clarify", args={"question": "Which?"}, tool_call_id="c2", session_id="20260102_120100_000aaa")
        self.assertEqual(self.sent()[0]["url"], f"hermesbot://bot/juniper?chat={self.side}")

    def test_a_room_members_approval_opens_the_room(self):
        self.context.update(chat="20260102_120200_77aa00", source="bot_room", room="r-garden")
        self.hooks.on_approval(command="rm x", description="delete", session_key="20260102_120200_77aa00", surface="gateway", pattern_key="rm")
        self.assertEqual(self.sent()[0]["url"], "hermesbot://room/r-garden")

    def test_a_room_member_whose_room_cannot_be_told_opens_needs_you_not_its_hidden_session(self):
        self.context.update(source="bot_room")
        self.hooks.on_tool(tool_name="clarify", args={"question": "Which?"}, tool_call_id="c3", session_id="20260102_120200_77aa00")
        self.assertEqual(self.sent()[0]["url"], "hermesbot://inbox")

    def test_a_gateway_session_key_is_not_a_chat(self):
        self.hooks.on_approval(command="rm x", description="delete", session_key="agent:main:telegram:dm:123", surface="gateway", pattern_key="rm")
        self.assertEqual(self.sent()[0]["url"], "hermesbot://bot/juniper")

    def test_a_routine_opens_the_bots_routines(self):
        self.hooks.on_session_end(session_id="cron_0a1b2c3d4e5f_20260102_090000", platform="cron", failed=True)
        self.assertEqual(self.sent()[0]["url"], "hermesbot://bot/juniper/routines")

    def test_a_repeat_of_a_waiting_approval_is_not_pushed(self):
        self.hooks.on_approval(command="rm x", description="delete", session_key=self.side, surface="gateway", pattern_key="rm2", coalesced=True)
        self.assertEqual(self.net.calls, [])


class Levels(Setup):
    def everything(self):
        self.hooks.on_approval(command="x", description="d", session_key="s", surface="gateway", pattern_key="p")
        self.hooks.on_tool(tool_name="clarify", args={"question": "q"}, tool_call_id="c")
        self.hooks.on_session_end(session_id="ok", platform="cron", failed=False)
        self.hooks.on_session_end(session_id="bad", platform="cron", failed=True)

        return [m["type"] for m in self.sent()]

    def test_all_is_only_what_needs_you_now(self):
        self.assertEqual(self.everything(), ["Approvals", "Questions", "Routines"])

    def test_needs_you_keeps_only_what_blocks_the_bot_and_broken_routines(self):
        self.store.set_settings(None, {"juniper": "needs_you"})
        self.assertEqual(self.everything(), ["Approvals", "Questions", "Routines"])

    def test_off_is_silence(self):
        self.store.set_settings(None, {"juniper": "off"})
        self.assertEqual(self.everything(), [])

    def test_levels_belong_to_one_bot(self):
        self.store.set_settings(None, {"someone-else": "off"})
        self.assertEqual(len(self.everything()), 3)


class Safety(Setup):
    def test_a_broken_hook_never_reaches_the_bot(self):
        self.hooks.store = None  # makes every callback raise
        callbacks = self.hooks.callbacks()

        with self.assertLogs("hermes-bot-mobile", level="ERROR"):  # the failures are logged, never raised
            for name, callback in callbacks.items():
                with self.subTest(hook=name):
                    self.assertIsNone(callback(command="x", description="d", session_key="s", surface="gateway", pattern_key="p", tool_name="clarify", args={}, session_id="s", platform="cron"))

    def test_callbacks_ignore_fields_added_by_newer_hermes(self):
        callbacks = self.hooks.callbacks()
        callbacks["pre_approval_request"](command="x", description="d", session_key="s", surface="gateway", pattern_key="p", brand_new_field=1)
        self.assertEqual(len(self.sent()), 1)

    def test_the_hooks_are_the_ones_the_manifest_declares(self):
        manifest = (ROOT / "plugin.yaml").read_text()

        for name in self.hooks.callbacks():
            self.assertIn(f"- {name}", manifest)


class LoadedLikeHermes(unittest.TestCase):
    """Hermes loads the folder as a package under another name; relative imports must work there."""

    def test_register_wires_every_hook(self):
        name = "hermes_plugins_hermes_bot_mobile_test"
        spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
        module = importlib.util.module_from_spec(spec)
        module.__package__ = name
        module.__path__ = [str(ROOT)]
        sys.modules[name] = module

        try:
            spec.loader.exec_module(module)
            registered = {}

            class Context:
                def register_hook(self, hook, callback):
                    registered[hook] = callback

            with tempfile.TemporaryDirectory() as folder:
                store_module = importlib.import_module(f"{name}.store")

                with mock.patch.object(store_module, "default_dir", lambda: Path(folder)):
                    module.register(Context())

            self.assertEqual(
                set(registered),
                {"pre_approval_request", "pre_tool_call", "post_llm_call", "on_session_end", "on_room_member_activity"},
            )
        finally:
            for key in [k for k in sys.modules if k == name or k.startswith(name + ".")]:
                del sys.modules[key]


if __name__ == "__main__":
    unittest.main()
