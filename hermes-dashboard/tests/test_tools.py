import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import store as S  # noqa: E402
import tools  # noqa: E402

FIELDS = [
    {"key": "customer", "label": "Customer", "type": "text", "required": True},
    {"key": "status", "type": "status", "stages": [{"name": "New"}, {"name": "Replied"}, {"name": "Done", "done": True}]},
    {"key": "reply_by", "type": "date"},
]


class Tools(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = S.Store(Path(self.dir.name))
        self.who = ["concierge"]
        # The acting bot comes only from the running turn; the tests stand in for the turn.
        patch = mock.patch.object(tools, "current_profile", lambda: self.who[0])
        patch.start()
        self.addCleanup(patch.stop)
        self.tools = tools.DashboardTools(store_factory=lambda: self.store)

    def tearDown(self):
        self.dir.cleanup()

    def call(self, handler, args):
        return json.loads(handler(args, task_id="t1"))

    def test_a_bot_sets_up_tracking_end_to_end(self):
        created = self.call(self.tools.collection, {"action": "create", "id": "support-emails", "title": "Support emails", "fields": FIELDS})
        self.assertIn("customer (text), status (status), reply_by (date)", created["message"])

        added = self.call(self.tools.record, {"action": "upsert", "collection": "support-emails", "records": [{"source_key": "m1", "values": {"customer": "Sam", "reply_by": "2026-10-14"}}, {"source_key": "m2", "values": {"customer": "Lee"}}]})
        self.assertEqual(added["message"], "2 added, 0 updated, 0 unchanged.")

        view = self.call(self.tools.view, {"action": "set", "id": "inbox", "title": "Support emails", "kind": "list", "collection": "support-emails", "config": {"show": {"title": "customer", "meta": "reply_by"}, "needs_you": {"field": "status", "op": "eq", "value": "New"}}})
        self.assertIn("list panel 'Support emails'", view["message"])

        answer = self.call(self.tools.query, {"collection": "support-emails", "filter": {"op": "is_open"}, "aggregate": {"op": "count"}})
        self.assertEqual(answer["aggregate"], 2)

        record_id = added["results"][0]["id"]
        self.assertEqual(self.call(self.tools.record, {"action": "close", "record_id": record_id})["message"], "Closed.")
        self.assertEqual(self.store.summary()["concierge"]["needs_you"], 1)

    def test_the_persons_change_is_reported_as_kept(self):
        self.call(self.tools.collection, {"action": "create", "id": "support-emails", "title": "Support emails", "fields": FIELDS})
        added = self.call(self.tools.record, {"action": "upsert", "collection": "support-emails", "records": [{"source_key": "m1", "values": {"customer": "Sam"}}]})
        self.store.update(S.Actor.person(), added["results"][0]["id"], {"status": "Replied"})
        again = self.call(self.tools.record, {"action": "upsert", "collection": "support-emails", "records": [{"source_key": "m1", "values": {"status": "New"}}]})
        self.assertIn("Kept your person's changes to status", again["message"])

    def test_mistakes_come_back_as_errors_to_fix(self):
        self.assertIn("needs fields", self.call(self.tools.collection, {"action": "create", "title": "Empty"})["error"])
        self.assertIn("no collection 'nope'", self.call(self.tools.record, {"action": "upsert", "collection": "nope", "records": [{"values": {}}]})["error"])
        self.assertIn("action must be", self.call(self.tools.view, {"action": "draw"})["error"])

    def test_each_bot_works_in_its_own_collections(self):
        self.call(self.tools.collection, {"action": "create", "id": "support-emails", "title": "Support emails", "fields": FIELDS})
        self.who[0] = "assistant"
        self.assertIn("error", self.call(self.tools.query, {"collection": "support-emails"}))

    def test_the_schemas_name_the_tools_the_manifest_declares(self):
        manifest = (ROOT / "plugin.yaml").read_text()

        for name, schema, _handler in self.tools.registrations():
            self.assertEqual(schema["name"], name)
            self.assertIn(f"- {name}", manifest)


class LoadedLikeHermes(unittest.TestCase):
    def test_register_adds_the_tools_the_skill_and_the_hint(self):
        name = "hermes_plugins_hermes_dashboard_test"
        spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
        module = importlib.util.module_from_spec(spec)
        module.__package__ = name
        module.__path__ = [str(ROOT)]
        sys.modules[name] = module

        try:
            spec.loader.exec_module(module)
            seen = {"tools": [], "skills": [], "sections": []}

            class Context:
                def register_tool(self, name, toolset, schema, handler, **kw):
                    seen["tools"].append((name, toolset))

                def register_skill(self, name, path, description):
                    seen["skills"].append((name, Path(path).is_file()))

                def register_system_prompt_section(self, name, text, max_chars=None):
                    seen["sections"].append(name)

            module.register(Context())
            self.assertEqual([tool for tool, _ in seen["tools"]], ["dashboard_collection", "dashboard_record", "dashboard_query", "dashboard_view"])
            self.assertEqual({toolset for _, toolset in seen["tools"]}, {"dashboard"})
            self.assertEqual(seen["skills"], [("dashboard", True)])
            self.assertEqual(seen["sections"], ["hermes-dashboard.hint"])
        finally:
            for key in [k for k in sys.modules if k == name or k.startswith(name + ".")]:
                del sys.modules[key]


if __name__ == "__main__":
    unittest.main()
