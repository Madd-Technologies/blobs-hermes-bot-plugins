import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import handoff  # noqa: E402

NOTE = "[ASYNC DELEGATION COMPLETE — deleg_1]\nyou can act on the result or re-dispatch if things have changed"


def make_module():
    """Stands in for tools.process_registry_notifications: the formatter returns Hermes's note unchanged."""
    module = types.ModuleType("process_registry_notifications")
    module._format_async_delegation = lambda evt: NOTE

    return module


def single(**fields):
    return {"type": "async_delegation", "delegation_id": "deleg_1", "goal": "Add a status column to the weekly report", **fields}


def batch(*results):
    return {"type": "async_delegation", "delegation_id": "deleg_2", "is_batch": True, "goals": ["one", "two", "three"], "results": list(results)}


TRUNCATED = {"status": "completed", "exit_reason": "max_iterations", "truncated": True, "summary": "Did half."}
DONE = {"status": "completed", "exit_reason": "completed", "summary": "All done."}


class HandoffTest(unittest.TestCase):
    def format(self, evt, profile="concierge"):
        module = make_module()
        self.assertTrue(handoff.patch_notifications(module))

        with mock.patch.object(handoff, "profile_name", return_value=profile):
            return module._format_async_delegation(evt)

    def test_truncated_task_gets_a_handoff_naming_its_profile(self):
        text = self.format(single(**TRUNCATED))
        self.assertTrue(text.startswith(NOTE))
        self.assertIn(handoff.HEADER, text)
        self.assertIn("The background task did not finish", text)
        self.assertIn('assignee "concierge"', text)
        self.assertIn('idempotency_key "leftover-deleg_1"', text)
        self.assertIn("delegate_task", text)

    def test_finished_task_is_left_alone(self):
        self.assertEqual(self.format(single(**DONE)), NOTE)

    def test_batch_names_only_the_unfinished_tasks(self):
        text = self.format(batch({**DONE, "task_index": 0}, {**TRUNCATED, "task_index": 1}))
        self.assertIn("Task 2 of this batch did not finish", text)
        self.assertIn("create one card,", text)
        self.assertIn('idempotency_key "leftover-deleg_2-<task number>"', text)

    def test_batch_with_several_unfinished_asks_for_a_card_each(self):
        text = self.format(batch({**TRUNCATED, "task_index": 2}, {**DONE, "task_index": 1}, {**TRUNCATED, "task_index": 0}))
        self.assertIn("Tasks 1, 3 of this batch did not finish", text)
        self.assertIn("one card per unfinished task", text)

    def test_batch_all_finished_is_left_alone(self):
        self.assertEqual(self.format(batch({**DONE, "task_index": 0}, {**DONE, "task_index": 1})), NOTE)

    def test_early_failure_notice_waits_for_the_batch(self):
        evt = {**batch({**TRUNCATED, "task_index": 0}), "task_failure_notice": True}
        self.assertEqual(self.format(evt), NOTE)

    def test_unknown_profile_asks_for_the_exact_name(self):
        text = self.format(single(**TRUNCATED), profile=None)
        self.assertIn("assignee: your own profile name, exactly", text)

    def test_a_failure_in_the_handoff_leaves_hermes_note(self):
        module = make_module()
        handoff.patch_notifications(module)

        with mock.patch.object(handoff, "leftovers", side_effect=RuntimeError("boom")), self.assertLogs("hermes-bot-fixes", "ERROR"):
            self.assertEqual(module._format_async_delegation(single(**TRUNCATED)), NOTE)

    def test_wrapping_twice_wraps_once(self):
        module = make_module()
        self.assertTrue(handoff.patch_notifications(module))
        wrapped = module._format_async_delegation
        self.assertTrue(handoff.patch_notifications(module))
        self.assertIs(module._format_async_delegation, wrapped)

    def test_a_formatter_of_another_shape_is_left_alone(self):
        module = make_module()
        original = module._format_async_delegation = lambda evt, extra: NOTE

        with self.assertLogs("hermes-bot-fixes", "WARNING"):
            self.assertFalse(handoff.patch_notifications(module))

        self.assertIs(module._format_async_delegation, original)

    def test_no_formatter_is_left_alone(self):
        with self.assertLogs("hermes-bot-fixes", "WARNING"):
            self.assertFalse(handoff.patch_notifications(types.ModuleType("empty")))


class UnfinishedTest(unittest.TestCase):
    def test_step_cap_is_unfinished(self):
        self.assertTrue(handoff.unfinished(TRUNCATED))
        self.assertTrue(handoff.unfinished({"status": "completed", "exit_reason": "max_iterations"}))

    def test_completed_is_finished(self):
        self.assertFalse(handoff.unfinished(DONE))
        self.assertFalse(handoff.unfinished({"status": "success"}))

    def test_out_of_time_stalled_or_cut_off_by_a_restart_is_unfinished(self):
        for status in ("timeout", "stalled", "unknown"):
            self.assertTrue(handoff.unfinished({"status": status}), status)

    def test_a_stop_an_error_a_cancel_or_a_refusal_stays_with_hermes(self):
        for status in ("interrupted", "error", "failed", "cancelled", "rejected"):
            self.assertFalse(handoff.unfinished({"status": status, "error": "x"}), status)

    def test_a_server_restart_before_any_task_reported_hands_off_the_whole_call(self):
        evt = {"type": "async_delegation", "delegation_id": "d", "is_batch": True, "goals": ["a", "b"], "results": [], "status": "unknown", "error": "owner died"}
        self.assertEqual(handoff.leftovers(evt), [(None, "a; b")])


if __name__ == "__main__":
    unittest.main()
