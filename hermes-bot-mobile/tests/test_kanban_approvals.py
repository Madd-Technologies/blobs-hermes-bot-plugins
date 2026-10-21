import json
import os
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

import helpers  # noqa: F401  (puts the plugin folder on the path)

import kanban_approvals as ka
import needs_you

WORKER_ENV = {ka.TASK_ENV: "t_1a2b3c4d", "HERMES_PROFILE": "orion", "HERMES_KANBAN_RUN_ID": "41"}


def fake_hermes(single_query_mode="deny", timeout=300):
    """Stand-ins for `tools.approval` and `tools.approval_context`, with the parts the relay uses, behaving like Hermes
    0.21.6: a worker has nobody present, and the queue resolves by session key and request id."""
    approval = types.SimpleNamespace(_lock=threading.Lock(), _gateway_notify_cbs={}, queues={}, settles={})

    def presence(approval_callback=None):
        return approval_callback, False, False, False

    def resolve_gateway_approval(session_key, choice, resolve_all=False, reason=None, request_id=None):
        queue = approval.queues.get(session_key, {})

        if request_id not in queue:
            return 0

        queue[request_id] = choice
        settle = approval.settles.pop(request_id, None)

        if settle:
            settle("resolved")

        return 1

    def register_gateway_settle(session_key, request_id, settle):
        if request_id not in approval.queues.get(session_key, {}):
            return False

        approval.settles[request_id] = settle

        return True

    approval._presence = presence
    approval.resolve_gateway_approval = resolve_gateway_approval
    approval.register_gateway_settle = register_gateway_settle
    context = types.SimpleNamespace(_get_approval_timeout=lambda: timeout, _get_single_query_approval_mode=lambda: single_query_mode)

    return approval, context


def queue(approval, session_key, request_id):
    approval.queues.setdefault(session_key, {})[request_id] = None


class WhereToAsk(unittest.TestCase):
    def test_the_chat_the_card_came_from_when_the_same_bot_made_it(self):
        task = {"created_by": "orion", "session_id": "20261020_101500_0dc184"}
        self.assertEqual(ka.where_to_ask(task, "orion"), "20261020_101500_0dc184")

    def test_the_working_bots_main_chat_when_another_bot_asked(self):
        # Juniper asked Orion for it: Juniper's chat can't answer for Orion.
        task = {"created_by": "juniper", "session_id": "20261020_101500_0dc184"}
        self.assertEqual(ka.where_to_ask(task, "orion"), "")

    def test_the_main_chat_without_a_chat_of_origin(self):
        self.assertEqual(ka.where_to_ask({"created_by": "dashboard", "session_id": ""}, "orion"), "")
        self.assertEqual(ka.where_to_ask({"created_by": "orion", "session_id": "cron_ab12_20261020_101500"}, "orion"), "")
        self.assertEqual(ka.where_to_ask(None, "orion"), "")

    def test_the_context_is_read_once_per_card(self):
        ka._CONTEXT = None
        reads = []

        def read(task_id, board):
            reads.append(task_id)

            return {"title": "Rotate the backups", "created_by": "orion", "session_id": "20261020_101500_0dc184", "assignee": "orion"}

        first = ka.worker_context(WORKER_ENV, read)
        second = ka.worker_context(WORKER_ENV, read)

        self.assertEqual(first, second)
        self.assertEqual(reads, ["t_1a2b3c4d"])
        self.assertEqual((first["profile"], first["chat"], first["title"], first["run"]), ("orion", "20261020_101500_0dc184", "Rotate the backups", "41"))
        self.assertIsNone(ka.worker_context({}, read))
        ka._CONTEXT = None


class Relay(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="kanban-approvals-"))
        self.approval, self.context = fake_hermes()
        self.where = {"task": "t_1a2b3c4d", "title": "Rotate the backups", "board": "", "created_by": "juniper", "profile": "orion", "chat": ""}
        self.relay = ka.WorkerRelay(folder=self.folder, context=lambda: self.where, approval=self.approval, approval_context=self.context, env=WORKER_ENV, pid=os.getpid(), poll_s=0.01)

    def test_only_a_worker_is_taken_over(self):
        relay = ka.WorkerRelay(folder=self.folder, approval=self.approval, approval_context=self.context, env={})
        self.assertFalse(relay.install())
        self.assertEqual(self.approval._presence()[3], False)

    def test_a_worker_counts_as_someone_to_ask_and_waits_longer(self):
        self.assertTrue(self.relay.install())
        self.assertEqual(self.approval._presence()[1:], (False, False, True))
        self.assertEqual(self.context._get_approval_timeout(), ka.WAIT_S)
        # Installing twice wraps nothing twice.
        self.assertTrue(self.relay.install())
        self.assertTrue(getattr(self.approval._presence, "hermes_bot_mobile"))

    def test_approve_for_single_query_runs_still_approves_without_asking(self):
        approval, context = fake_hermes(single_query_mode="approve")
        relay = ka.WorkerRelay(folder=self.folder, approval=approval, approval_context=context, env=WORKER_ENV)
        relay.install()
        self.assertEqual(approval._presence()[3], False)

    def test_a_longer_configured_wait_stands(self):
        approval, context = fake_hermes(timeout=7200)
        ka.WorkerRelay(folder=self.folder, approval=approval, approval_context=context, env=WORKER_ENV).install()
        self.assertEqual(context._get_approval_timeout(), 7200)

    def test_a_session_without_a_notify_callback_falls_back_to_the_relay(self):
        self.relay.install()
        self.assertTrue(callable(self.approval._gateway_notify_cbs.get("default")))

    def test_on_hermes_0_22_the_gateways_callback_and_the_relay_both_get_it(self):
        # The gateway's managed worker registers its own callback for the card's session; the app must still see it.
        self.relay.install()
        seen = []
        self.approval._gateway_notify_cbs["card"] = lambda data: seen.append(data["request_id"])
        notified = []
        self.relay.notify = lambda session_key, data: notified.append((session_key, data["request_id"]))

        self.approval._gateway_notify_cbs.get("card")({"request_id": "abc123"})
        self.assertEqual(seen, ["abc123"])
        self.assertEqual(notified, [("card", "abc123")])

    def test_a_relay_failure_leaves_the_gateways_copy_standing(self):
        self.relay.install()
        seen = []
        self.approval._gateway_notify_cbs["card"] = lambda data: seen.append(data["request_id"])

        def broken(session_key, data):
            raise OSError("disk full")

        self.relay.notify = broken
        self.approval._gateway_notify_cbs.get("card")({"request_id": "abc123"})
        self.assertEqual(seen, ["abc123"])

    def test_the_approval_is_written_down_answered_and_cleared(self):
        self.relay.install()
        queue(self.approval, "default", "abc123")
        self.approval._gateway_notify_cbs.get("default")({"request_id": "abc123", "command": "rm -rf /tmp/build", "description": "recursive delete", "allow_permanent": False})

        record = json.loads((self.folder / "abc123.json").read_text())
        self.assertEqual((record["profile"], record["chat"], record["task"]["title"], record["task"]["created_by"]), ("orion", "", "Rotate the backups", "juniper"))
        self.assertEqual((record["command"], record["allow_session"], record["allow_permanent"]), ("rm -rf /tmp/build", True, False))
        self.assertEqual(oct((self.folder / "abc123.json").stat().st_mode & 0o777), "0o600")

        dashboard = ka.KanbanApprovals(folder=self.folder)
        self.assertEqual([entry["id"] for entry in dashboard.pending()], ["abc123"])
        self.assertNotIn("pid", dashboard.pending()[0])
        self.assertEqual(dashboard.answer("abc123", "once"), {"answered": True, "id": "abc123", "choice": "once"})
        # Answered: off the list while the worker takes it.
        self.assertEqual(dashboard.pending(), [])

        deadline = time.time() + 2

        while (self.folder / "abc123.json").exists() and time.time() < deadline:
            time.sleep(0.01)

        self.assertEqual(self.approval.queues["default"]["abc123"], "once")
        self.assertEqual(list(self.folder.glob("*.json")), [])

    def test_an_approval_hermes_no_longer_waits_on_is_cleared(self):
        self.relay.install()
        # Not in Hermes's queue (already timed out): the settle can't be attached.
        self.relay.notify("default", {"request_id": "gone1", "command": "x"})
        self.assertEqual(list(self.folder.glob("*.json")), [])

    def test_a_bad_request_id_makes_hermes_refuse(self):
        self.relay.install()

        with self.assertRaises(ValueError):
            self.relay.notify("default", {"request_id": "../../etc", "command": "x"})


class Dashboard(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="kanban-approvals-"))
        self.alive = {111}
        self.now = 1_000_000.0
        self.approvals = ka.KanbanApprovals(folder=self.folder, alive=lambda pid: pid in self.alive, clock=lambda: self.now)

    def write(self, request_id, pid=111, created_at=None, **extra):
        record = {"v": 1, "id": request_id, "pid": pid, "created_at": self.now if created_at is None else created_at, "profile": "orion", "chat": "", "task": {"id": "t_1"}, "command": "ls", "description": "", "allow_session": True, "allow_permanent": True, **extra}
        (self.folder / f"{request_id}.json").write_text(json.dumps(record))

    def test_a_record_whose_worker_is_gone_is_cleared(self):
        self.write("live1")
        self.write("dead1", pid=222)
        self.write("old1", created_at=self.now - ka.STALE_S - 1)

        self.assertEqual([entry["id"] for entry in self.approvals.pending()], ["live1"])
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()), ["live1.json"])

    def test_answers_are_checked(self):
        self.write("a1", allow_session=False)

        with self.assertRaises(ka.Refusal) as caught:
            self.approvals.answer("a1", "session")

        self.assertEqual(caught.exception.status, 400)
        self.approvals.answer("a1", "deny")

        with self.assertRaises(ka.Refusal) as caught:
            self.approvals.answer("a1", "once")

        self.assertEqual(caught.exception.status, 409)

        for missing in ("nope", "../a1"):
            with self.assertRaises(ka.Refusal) as caught:
                self.approvals.answer(missing, "once")

            self.assertEqual(caught.exception.status, 404)

    def test_the_folder_signature_changes_with_it(self):
        empty = self.approvals.signature()
        self.write("s1")
        self.assertNotEqual(self.approvals.signature(), empty)


class NeedsYouEvent(unittest.TestCase):
    def test_kanban_approvals_go_out_with_the_rest(self):
        folder = Path(tempfile.mkdtemp(prefix="kanban-approvals-"))
        approvals = ka.KanbanApprovals(folder=folder, alive=lambda pid: True)
        module = types.SimpleNamespace(_lock=threading.Lock(), _open={})
        sent = []
        watch = needs_you.NeedsYou(requests=lambda: module, broadcast=sent.append, session_of=lambda sid: {}, coalesce_s=0, kanban=approvals, kanban_poll_s=0.02)
        self.assertTrue(watch.install())

        record = {"v": 1, "id": "k1", "pid": 1, "created_at": time.time(), "profile": "orion", "chat": "", "task": {"id": "t_1", "title": "Rotate the backups"}, "command": "ls", "description": "", "allow_session": True, "allow_permanent": True}
        (folder / "k1.json").write_text(json.dumps(record))
        deadline = time.time() + 2

        while not any(payload["kanban"] for payload in sent) and time.time() < deadline:
            time.sleep(0.01)

        last = sent[-1]
        self.assertEqual([entry["id"] for entry in last["kanban"]], ["k1"])
        self.assertEqual(last["kanban"][0]["task"]["title"], "Rotate the backups")
        self.assertEqual(watch.snapshot()["kanban"][0]["id"], "k1")

        approvals.answer("k1", "once")
        watch.poke()
        deadline = time.time() + 2

        while sent[-1]["kanban"] and time.time() < deadline:
            time.sleep(0.01)

        self.assertEqual(sent[-1]["kanban"], [])


if __name__ == "__main__":
    unittest.main()
