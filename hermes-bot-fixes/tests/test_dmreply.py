import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import dmreply  # noqa: E402

ACK = json.dumps({"status": "queued", "delivery_id": "d1"})

ROSTER = ["default", "concierge", "harbor", "juniper", "orion"]


def make_module(calls):
    """Stands in for tools.bot_mode_dm: message_agent_tool records what it was asked to send and acknowledges it."""
    module = types.ModuleType("bot_mode_dm")

    def message_agent_tool(target: str = "", message: str = "", task_id=None, agent=None) -> str:
        calls.append({"target": target, "message": message, "task_id": task_id, "agent": agent})

        return ACK

    def resolve(target, roster, root=None):
        want = target.strip().lower()

        if want == "hermes":
            return "default"

        return {"orion": "orion", "orion the planner": "orion", "juniper": "juniper", "harbor": "harbor"}.get(want)

    module.message_agent_tool = message_agent_tool
    module._agent_home = lambda agent: "/srv/hermes/profiles/juniper"
    module._resolve_local_name = resolve

    return module


def make_probe(peers=()):
    """Stands in for tools.bot_mode_probe's roster helpers."""
    probe = types.ModuleType("tools.bot_mode_probe")
    probe._hermes_root = lambda home: Path("/srv/hermes")
    probe._roster = lambda root: [(name, Path("/srv/hermes") / name) for name in ROSTER]
    probe._peers = lambda root: list(peers)

    return probe


def agent(author):
    return types.SimpleNamespace(_turn_author=author)


ORION = {"id": "bot:orion", "name": "orion", "is_bot": True}


class AskerTest(unittest.TestCase):
    def test_a_bot_on_this_install(self):
        self.assertEqual(dmreply.asker(agent(ORION)), ("orion", "orion"))

    def test_the_default_bot_goes_by_its_handle(self):
        self.assertEqual(dmreply.asker(agent({"id": "bot:default", "name": "hermes", "is_bot": True})), ("default", "hermes"))

    def test_nobody_for_a_person_a_wake_up_or_a_bot_elsewhere(self):
        for author in (
            None,
            {"id": "user", "name": "Alex", "is_bot": False},
            {"id": "bot:orion", "name": "orion", "is_bot": False},
            {"id": "bot:spark/orion", "name": "orion", "is_bot": True},
            {"id": "bot:", "name": "", "is_bot": True},
            {"id": "123456", "name": "Alex", "is_bot": True},
            "bot:orion",
        ):
            self.assertIsNone(dmreply.asker(agent(author)), author)

        self.assertIsNone(dmreply.asker(object()))


class TargetProfileTest(unittest.TestCase):
    def resolve(self, target, peers=()):
        with mock.patch.dict(sys.modules, {"tools": types.ModuleType("tools"), "tools.bot_mode_probe": make_probe(peers)}):
            return dmreply.target_profile(make_module([]), target, agent(ORION))

    def test_resolves_names_and_handles_like_message_agent(self):
        self.assertEqual(self.resolve("orion"), "orion")
        self.assertEqual(self.resolve("@Orion"), "orion")
        self.assertEqual(self.resolve("Orion the Planner"), "orion")
        self.assertEqual(self.resolve("hermes"), "default")
        self.assertIsNone(self.resolve("nobody"))
        self.assertIsNone(self.resolve(""))
        self.assertIsNone(self.resolve(None))

    def test_peers_and_other_connections_are_not_local(self):
        self.assertIsNone(self.resolve("spark/orion"))
        self.assertIsNone(self.resolve("orion@cloud-1"))
        self.assertIsNone(self.resolve("orion", peers=["orion"]))


class PatchTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.module = make_module(self.calls)
        self.assertTrue(dmreply.patch_message_agent(self.module))

    def send(self, target, author, resolved=None):
        bot = agent(author)

        with mock.patch.object(dmreply, "target_profile", return_value=resolved) as resolve:
            result = self.module.message_agent_tool(target=target, message="The report is ready.", task_id="t1", agent=bot)

        return result, resolve

    def test_an_answer_to_the_bot_that_asked_is_not_sent(self):
        result, _resolve = self.send("orion", ORION, resolved="orion")
        payload = json.loads(result)

        self.assertEqual(self.calls, [])
        self.assertEqual(payload["status"], "not_sent")
        self.assertEqual(payload["reason"], "reply_goes_back")
        self.assertIn("your reply in this chat goes back to @orion by itself", payload["error"])
        self.assertIn("as your reply instead", payload["error"])

    def test_the_default_bot_is_named_by_its_handle(self):
        payload = json.loads(self.send("hermes", {"id": "bot:default", "name": "hermes", "is_bot": True}, resolved="default")[0])

        self.assertEqual(self.calls, [])
        self.assertIn("@hermes", payload["error"])

    def test_a_message_to_another_bot_is_sent(self):
        result, _resolve = self.send("harbor", ORION, resolved="harbor")

        self.assertEqual(result, ACK)
        self.assertEqual(self.calls, [{"target": "harbor", "message": "The report is ready.", "task_id": "t1", "agent": self.calls[0]["agent"]}])

    def test_a_message_from_a_turn_no_bot_started_is_sent_without_resolving(self):
        for author in (None, {"id": "user", "name": "Alex", "is_bot": False}, {"id": "bot:spark/orion", "name": "orion", "is_bot": True}):
            result, resolve = self.send("orion", author, resolved="orion")

            self.assertEqual(result, ACK)
            resolve.assert_not_called()

        self.assertEqual(len(self.calls), 3)

    def test_a_failing_check_sends_it(self):
        with mock.patch.object(dmreply, "target_profile", side_effect=RuntimeError("roster unreadable")):
            with self.assertLogs("hermes-bot-fixes", "ERROR"):
                result = self.module.message_agent_tool(target="orion", message="hi", agent=agent(ORION))

        self.assertEqual(result, ACK)
        self.assertEqual(len(self.calls), 1)

    def test_positional_arguments_still_reach_hermes(self):
        with mock.patch.object(dmreply, "target_profile", return_value="harbor"):
            self.assertEqual(self.module.message_agent_tool("harbor", "hi", "t2", agent(ORION)), ACK)

        self.assertEqual((self.calls[0]["target"], self.calls[0]["task_id"]), ("harbor", "t2"))

    def test_applies_once(self):
        wrapped = self.module.message_agent_tool

        self.assertTrue(dmreply.patch_message_agent(self.module))
        self.assertIs(self.module.message_agent_tool, wrapped)


class ShapeTest(unittest.TestCase):
    def test_leaves_an_unexpected_tool_alone(self):
        module = make_module([])
        module.message_agent_tool = lambda target, message, agent=None: ACK
        original = module.message_agent_tool

        with self.assertLogs("hermes-bot-fixes", "WARNING"):
            self.assertFalse(dmreply.patch_message_agent(module))

        self.assertIs(module.message_agent_tool, original)

    def test_leaves_it_alone_without_the_resolver(self):
        module = make_module([])
        del module._resolve_local_name

        with self.assertLogs("hermes-bot-fixes", "WARNING"):
            self.assertFalse(dmreply.patch_message_agent(module))

    def test_no_tool_at_all(self):
        with self.assertLogs("hermes-bot-fixes", "WARNING"):
            self.assertFalse(dmreply.patch_message_agent(types.ModuleType("bot_mode_dm")))


if __name__ == "__main__":
    unittest.main()
