import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import files as F  # noqa: E402


class Plan(unittest.TestCase):
    def test_own_files_then_each_parents_under_their_own_folder(self):
        items = F.plan("t_1", [{"filename": "brief.pdf", "stored_path": "/a/brief.pdf", "size": 3}],
                       [({"id": "t_0", "title": "Research"}, [{"filename": "notes.md", "stored_path": "/a/notes.md", "size": 2}])])

        self.assertEqual([(i["rel"], i["label"]) for i in items], [
            (".kanban-files/t_1/brief.pdf", ""),
            (".kanban-files/t_1/from-t_0/notes.md", 'from card t_0 "Research"'),
        ])

    def test_names_are_made_safe_and_unique(self):
        items = F.plan("t_1", [
            {"filename": "../../etc/passwd", "stored_path": "/a/1"},
            {"filename": "a.txt", "stored_path": "/a/2"},
            {"filename": "a.txt", "stored_path": "/a/3"},
            {"filename": "README", "stored_path": "/a/4"},
            {"filename": "README", "stored_path": "/a/5"},
            {"filename": "..", "stored_path": "/a/6"},
        ], [])

        self.assertEqual([i["rel"].split("/", 2)[2] for i in items], ["passwd", "a.txt", "a-2.txt", "README", "README-2", "file"])


class CopyInto(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.store = self.base / "attachments"
        self.store.mkdir()
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def attachment(self, name, data=b"hello"):
        path = self.store / name
        path.write_bytes(data)

        return {"filename": name, "stored_path": str(path), "size": len(data)}

    def test_copies_and_leaves_the_original(self):
        items = F.plan("t_1", [self.attachment("brief.pdf")], [])
        copied, left = F.copy_into(self.workspace, items)

        self.assertEqual((len(copied), left), (1, []))
        self.assertEqual((self.workspace / ".kanban-files/t_1/brief.pdf").read_bytes(), b"hello")
        self.assertTrue((self.store / "brief.pdf").exists())

        # A second claim (the card was rerun) finds it there and copies nothing again.
        with mock.patch.object(F.shutil, "copy2") as copy:
            copied, left = F.copy_into(self.workspace, items)

        copy.assert_not_called()
        self.assertEqual(len(copied), 1)

    def test_missing_and_too_big_files_are_left(self):
        items = F.plan("t_1", [self.attachment("big.bin", b"x" * 20), {"filename": "gone.txt", "stored_path": str(self.store / "gone.txt")}], [])

        with mock.patch.object(F, "MAX_FILE_BYTES", 10):
            copied, left = F.copy_into(self.workspace, items)

        self.assertEqual(copied, [])
        self.assertEqual(sorted(item["why"] for item in left), ["missing", "too big to copy"])

    def test_note_lists_paths_from_the_workspace(self):
        copied = [{"rel": ".kanban-files/t_1/brief.pdf", "label": ""}, {"rel": ".kanban-files/t_1/from-t_0/n.md", "label": "from card t_0"}]
        text = F.note(copied, [{"source": "/srv/big.bin", "why": "too big to copy", "label": ""}])

        self.assertTrue(text.startswith(F.MARK))
        self.assertIn("(in Docker that is /workspace)", text)
        self.assertIn("- .kanban-files/t_1/from-t_0/n.md (from card t_0)", text)
        self.assertIn("- /srv/big.bin (too big to copy)", text)


class OnClaimed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.workspace = base / "ws"
        brief = base / "brief.pdf"
        brief.write_bytes(b"pdf")
        notes = base / "notes.md"
        notes.write_bytes(b"md")
        self.comments = []
        self.events = []
        files = {"t_1": [{"filename": "brief.pdf", "stored_path": str(brief), "size": 3}],
                 "t_0": [{"filename": "notes.md", "stored_path": str(notes), "size": 2}]}
        self.api = SimpleNamespace(
            AUTHOR="hermes-kanban",
            attachments=lambda conn, task_id: files.get(task_id, []),
            parents=lambda conn, task_id: [{"id": "t_0", "title": "Research"}] if task_id == "t_1" else [],
            workspace=lambda conn, task_id, board: self.workspace,
            recent_comments=lambda conn, task_id, limit: [{"author": "hermes-kanban", "body": b} for b in self.comments],
            comment=lambda conn, task_id, body: self.comments.append(body),
        )

        @contextmanager
        def open_board(name):
            yield "conn"

        self.stager = F.Stager(emit=lambda name, **kw: self.events.append((name, kw)), open_board=open_board, board_api=self.api)

    def tearDown(self):
        self.tmp.cleanup()

    def test_stages_own_and_parent_files_and_comments_once(self):
        self.stager.on_claimed(task_id="t_1", board=None)
        self.stager.on_claimed(task_id="t_1", board=None)

        self.assertTrue((self.workspace / ".kanban-files/t_1/brief.pdf").exists())
        self.assertTrue((self.workspace / ".kanban-files/t_1/from-t_0/notes.md").exists())
        self.assertEqual(len(self.comments), 1)
        self.assertEqual([name for name, _ in self.events], ["kanban_card_files_staged"] * 2)
        self.assertEqual(self.events[0][1]["files"], [".kanban-files/t_1/brief.pdf", ".kanban-files/t_1/from-t_0/notes.md"])

    def test_a_card_without_files_or_a_worktree_is_left_alone(self):
        self.assertIsNone(self.stager.on_claimed(task_id="t_0x", board=None))
        self.api.workspace = lambda conn, task_id, board: None
        self.assertIsNone(self.stager.on_claimed(task_id="t_1", board=None))
        self.assertEqual((self.comments, self.events), ([], []))


if __name__ == "__main__":
    unittest.main()
