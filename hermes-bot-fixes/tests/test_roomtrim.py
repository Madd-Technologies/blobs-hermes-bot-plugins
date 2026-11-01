import sys
import types
import unittest
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import roomtrim  # noqa: E402


@dataclass
class Event:
    event_id: str
    kind: str
    payload: dict = field(default_factory=dict)


class DiscussionValidationError(ValueError):
    pass


def make_module(seen):
    """Stands in for gateway.hosted_room_discussion: refuses outcomes whose person's message is missing, as Hermes does."""
    module = types.ModuleType("hosted_room_discussion")
    module.DiscussionValidationError = DiscussionValidationError

    def _derive_member_watermarks(events):
        seen.append([event.event_id for event in events])
        users = {event.event_id for event in events if event.kind == "message.user"}

        for event in events:
            if event.kind == "turn.settled" and event.payload.get("discussion_event_id") not in users:
                raise DiscussionValidationError(roomtrim.TRIMMED)

        return {"m-orion": len(events)}

    module._derive_member_watermarks = _derive_member_watermarks

    return module


def conversation(n, *, with_user=True):
    discussion = {"discussion_event_id": f"user:{n}"}
    events = [Event(f"user:{n}", "message.user")] if with_user else []

    return events + [Event(f"member:{n}", "message.member", discussion), Event(f"settled:{n}", "turn.settled", discussion)]


class RoomTrimTest(unittest.TestCase):
    def test_plans_without_a_trimmed_conversations_leftovers(self):
        seen = []
        module = make_module(seen)
        self.assertTrue(roomtrim.patch_watermarks(module))
        events = conversation(1, with_user=False) + conversation(2) + [Event("activity", "room.activity")]

        self.assertEqual(module._derive_member_watermarks(events), {"m-orion": 4})
        self.assertEqual(seen[-1], ["user:2", "member:2", "settled:2", "activity"])

    def test_whole_conversations_plan_once_unchanged(self):
        seen = []
        module = make_module(seen)
        roomtrim.patch_watermarks(module)
        events = conversation(1) + conversation(2)

        self.assertEqual(module._derive_member_watermarks(events), {"m-orion": 6})
        self.assertEqual(len(seen), 1)

    def test_other_errors_are_raised(self):
        module = make_module([])

        def refuses(events):
            raise DiscussionValidationError("thread is unknown")

        module._derive_member_watermarks = refuses
        roomtrim.patch_watermarks(module)

        with self.assertRaisesRegex(DiscussionValidationError, "thread is unknown"):
            module._derive_member_watermarks(conversation(1))

    def test_the_error_with_nothing_to_drop_is_raised(self):
        module = make_module([])

        def refuses(events):
            raise DiscussionValidationError(roomtrim.TRIMMED)

        module._derive_member_watermarks = refuses
        roomtrim.patch_watermarks(module)

        with self.assertRaises(DiscussionValidationError):
            module._derive_member_watermarks(conversation(1))

    def test_applies_once(self):
        module = make_module([])
        roomtrim.patch_watermarks(module)
        wrapped = module._derive_member_watermarks

        self.assertTrue(roomtrim.patch_watermarks(module))
        self.assertIs(module._derive_member_watermarks, wrapped)

    def test_leaves_a_different_planner_alone(self):
        module = make_module([])
        module._derive_member_watermarks = lambda events, extra: {}
        original = module._derive_member_watermarks

        self.assertFalse(roomtrim.patch_watermarks(module))
        self.assertIs(module._derive_member_watermarks, original)

        del module.DiscussionValidationError
        self.assertFalse(roomtrim.patch_watermarks(module))

    def test_payloadless_events_are_kept(self):
        events = [Event("a", "room.activity", None), Event("b", "turn.settled", None)]

        self.assertEqual([event.event_id for event in roomtrim.without_trimmed(events)], ["a"])


if __name__ == "__main__":
    unittest.main()
