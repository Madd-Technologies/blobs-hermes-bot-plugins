import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import compaction  # noqa: E402

HEADER = "[STILL IN PROGRESS — this is the active request, restated after the compaction boundary because it was not finished yet. Continue it; do not start over.]"


def make_compressor():
    """Stands in for Hermes's ContextCompressor: the newest row without a display_kind, unfinished unless the chat ends on a
    plain reply; the restatement appends the task again under the header."""

    class Compressor:
        quiet_mode = True

        @classmethod
        def _find_inflight_user_task(cls, messages):
            index = next((i for i in range(len(messages) - 1, -1, -1) if messages[i].get("role") == "user" and not messages[i].get("display_kind")), -1)

            if index < 0:
                return None

            last = messages[-1]

            if last.get("role") == "assistant" and not last.get("tool_calls") and (last.get("content") or "").strip():
                return None

            return messages[index]

        def _reappend_inflight_user_task(self, compressed, inflight):
            if inflight is None:
                return compressed

            return [*compressed, {**inflight, "content": HEADER + "\n" + inflight["content"]}]

    return Compressor


def request():
    return {"role": "user", "content": "Add a status column to the weekly report"}


def delegated(ask):
    return [
        ask,
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "function": {"name": "delegate_task", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": '{"status": "dispatched"}'},
        {"role": "assistant", "content": "Implementation is running in the background."},
    ]


def woke(kind="async_delegation_complete"):
    return {"role": "user", "display_kind": kind, "content": "[ASYNC DELEGATION BATCH COMPLETE — deleg_1] TASK 1/1 TRUNCATED: hit max_iterations"}


def working():
    return [
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c2", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c2", "content": "..."},
    ]


class Finder(unittest.TestCase):
    def setUp(self):
        self.cls = make_compressor()
        self.assertTrue(compaction.patch_compressor(self.cls))

    def test_an_answered_request_is_not_the_task_once_a_background_result_woke_the_bot(self):
        ask, wake = request(), woke()
        messages = [{"role": "system", "content": "s"}, *delegated(ask), wake, *working()]
        self.assertIs(self.cls._find_inflight_user_task(messages), wake)

    def test_a_background_process_wake_up_counts_too(self):
        ask, wake = request(), woke("process_complete")
        self.assertIs(self.cls._find_inflight_user_task([*delegated(ask), wake, *working()]), wake)

    def test_the_newest_wake_up_wins(self):
        ask, first, second = request(), woke(), woke()
        messages = [*delegated(ask), first, {"role": "assistant", "content": "Checked it."}, second, *working()]
        self.assertIs(self.cls._find_inflight_user_task(messages), second)

    def test_a_request_still_being_worked_on_stays_the_task(self):
        ask = request()
        messages = [ask, *working()]
        self.assertIs(self.cls._find_inflight_user_task(messages), ask)

    def test_a_wake_up_before_any_reply_leaves_hermes_choice_alone(self):
        ask, wake = request(), woke()
        messages = [ask, *working(), wake, *working()]
        self.assertIs(self.cls._find_inflight_user_task(messages), ask)

    def test_a_finished_chat_still_has_no_task(self):
        ask = request()
        self.assertIsNone(self.cls._find_inflight_user_task([*delegated(ask), woke(), {"role": "assistant", "content": "Done."}]))

    def test_patching_twice_is_harmless(self):
        self.assertTrue(compaction.patch_compressor(self.cls))
        ask, wake = request(), woke()
        self.assertIs(self.cls._find_inflight_user_task([*delegated(ask), wake, *working()]), wake)


class Restatement(unittest.TestCase):
    def setUp(self):
        self.cls = make_compressor()
        compaction.patch_compressor(self.cls)
        self.compressor = self.cls()
        self.carrier = {"role": "user", "content": "summary", "_compressed_summary": True}

    def test_nothing_is_restated_when_the_result_survived_the_compaction(self):
        wake = woke()
        compressed = [self.carrier, dict(wake), *working()]
        self.assertEqual(self.compressor._reappend_inflight_user_task(compressed, wake), compressed)

    def test_the_result_is_restated_when_it_was_summarised_away_and_keeps_its_kind(self):
        wake = woke()
        result = self.compressor._reappend_inflight_user_task([self.carrier, *working()], wake)
        self.assertEqual(result[-1]["display_kind"], "async_delegation_complete")
        self.assertTrue(result[-1]["content"].startswith(HEADER))

    def test_a_person_s_request_is_restated_as_before(self):
        ask = request()
        result = self.compressor._reappend_inflight_user_task([self.carrier, *working()], ask)
        self.assertTrue(result[-1]["content"].startswith(HEADER))


class Shape(unittest.TestCase):
    def test_methods_of_another_shape_are_left_alone(self):
        class Other:
            def _find_inflight_user_task(self, messages):  # not a classmethod
                return None

            def _reappend_inflight_user_task(self, compressed, inflight):
                return compressed

        self.assertFalse(compaction.patch_compressor(Other))
        self.assertFalse(getattr(Other._find_inflight_user_task, compaction.MARK, False))

    def test_other_arguments_are_left_alone(self):
        class Other:
            @classmethod
            def _find_inflight_user_task(cls, messages, extra):
                return None

            def _reappend_inflight_user_task(self, compressed, inflight):
                return compressed

        self.assertFalse(compaction.patch_compressor(Other))


if __name__ == "__main__":
    unittest.main()
