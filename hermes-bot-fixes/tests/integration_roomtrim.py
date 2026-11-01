"""Runs a two-bot group room through Hermes's own room log, policy checkpoint and planner for 30 questions, the way its
room service plans each turn, before and after the room trim fix, with no model call (each answer is published as the
room service would publish a finished turn).

Not part of the unit tests (it needs a Hermes checkout at the version being fixed). Run it with a Hermes venv:

    ~/.hermes/hermes-agent/venv/bin/python hermes-bot-fixes/tests/integration_roomtrim.py <path to a Hermes checkout>

The room lives in a throwaway SQLite file, so the real `~/.hermes` is never read or written. Without the fix, Hermes
0.22.4 refuses to plan the 13th question (its checkpoint trimmed the 1st question but kept its answer); with it, all 30
are answered.
"""

import inspect
import sys
import tempfile
import time
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else str(Path.home() / ".hermes" / "hermes-agent"))
sys.path.insert(0, str(PLUGIN))

from gateway import hosted_room_discussion as discussion, hosted_rooms  # noqa: E402
from gateway.hosted_room_policy_checkpoint import HostedRoomPolicyCheckpoint  # noqa: E402

import roomtrim  # noqa: E402

ROUNDS = 30
GATEWAY = "gateway-a"
PROFILES = ("orion", "juniper")
#: 0.22 plans from the input the turn was given (0.21 has no such option).
FROZEN = {"freeze_input_context": True} if "freeze_input_context" in inspect.signature(discussion.plan_next_task).parameters else {}
MEMBERS = [{"member_id": f"m-{profile}", "profile": profile, "handle": profile, "display_name": profile.title()} for profile in PROFILES]


def run(name: str) -> int:
    """Asks the room ROUNDS questions; the number answered before planning failed."""
    db = Path(tempfile.mkdtemp(prefix="hermes-bot-fixes-roomtrim-")) / "state.db"
    room_id = f"room-{name}"
    room = hosted_rooms.create_room(db, room_id=room_id, name="Two bots", members=MEMBERS, authority_gateway_id=GATEWAY, now=1)
    checkpoint = HostedRoomPolicyCheckpoint(db)

    def events():
        return hosted_rooms.read_events(db, room_id=room_id, since_seq=0, limit=hosted_rooms.MAX_LOG_LIMIT)["events"]

    for n in range(ROUNDS):
        hosted_rooms.append_event(
            db, room_id=room_id, event_id=f"user:{n}", kind="message.user", actor={"kind": "user", "id": "you"}, authority_gateway_id=GATEWAY,
            authority_epoch=1, payload={"text": f"@orion question {n}", "thread_id": "t-main"}, now=time.time(),
        )

        # As HostedRoomService.prepare_room plans: from the checkpoint, with the members' watermarks kept beside it.
        state = hosted_rooms.room_state(db, room_id=room_id)
        snapshot = checkpoint.snapshot(room_id=room_id, latest_seq=int(state["latest_seq"]))

        try:
            decision = discussion.plan_next_task(state, list(snapshot.events), local_profiles=PROFILES, initial_watermarks=snapshot.watermarks, **FROZEN)
        except discussion.DiscussionValidationError as error:
            print(f"{name}: question {n + 1} could not be planned: {error}")

            return n

        assert decision.status == "task", decision
        assert decision.task.discussion_event_id == f"user:{n}", decision.task
        publication = discussion.plan_publication(room, events(), decision.task, status="settled", result={"text": f"answer {n}"}, local_profiles=PROFILES)

        for event in publication.events:
            hosted_rooms.append_event(db, **event.append_kwargs(room_id), now=time.time())

    print(f"{name}: all {ROUNDS} questions answered")

    return ROUNDS


before = run("unfixed")
assert roomtrim.apply(), "the fix did not apply to this Hermes"
after = run("fixed")

assert before < ROUNDS, "this Hermes answered every question without the fix: check whether it still trims part-way"
assert after == ROUNDS, f"the fixed room stopped after {after} questions"
print("ok")
