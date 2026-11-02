"""A chat's history cut down for the phone (`history.py`), and the routes that serve it.

    python -m unittest discover -s hermes-bot-mobile/tests
"""

import json
import unittest

import helpers  # noqa: F401  (puts the plugin folder on sys.path)
import history


def user(text, row_id, **extra):
    return {"id": row_id, "role": "user", "content": text, "api_content": "context the model saw\n" + text, **extra}


def calls(row_id, *pairs, content=""):
    return {
        "id": row_id,
        "role": "assistant",
        "content": content,
        "reasoning_details": [{"type": "reasoning.encrypted", "data": "x" * 5000}],
        "reasoning": "thinking it over",
        "tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}} for call_id, name, args in pairs],
    }


def result(row_id, call_id, content, **extra):
    return {"id": row_id, "role": "tool", "tool_call_id": call_id, "content": content, **extra}


def reply(text, row_id):
    return {"id": row_id, "role": "assistant", "content": text, "codex_reasoning_items": ["e" * 3000], "codex_message_items": [{"type": "message"}]}


class Slimming(unittest.TestCase):
    def test_drops_what_the_app_never_draws_and_keeps_what_it_does(self):
        rows = history.slim_rows([user("Hi", 1), reply("Hello", 2)])

        self.assertEqual(rows[0], {"id": 1, "role": "user", "content": "Hi"})
        self.assertEqual(rows[1], {"id": 2, "role": "assistant", "content": "Hello", "codex_message_items": [{"type": "message"}]})

    def test_a_long_output_keeps_its_small_fields_and_its_end(self):
        output = "\n".join(f"line {n}" for n in range(2000)) + "\nall done"
        rows = history.slim_rows([calls(1, ("c1", "terminal", {"command": "make"})), result(2, "c1", json.dumps({"output": output, "exit_code": 2, "error": None}))])
        slim = json.loads(rows[1]["content"])

        self.assertEqual(slim["exit_code"], 2, "a failed step still reads as failed")
        self.assertIsNone(slim["error"])
        self.assertTrue(slim["output"].endswith("all done"), "the last line the app shows survives")
        self.assertLessEqual(len(slim["output"]), history.TAIL + 1)
        self.assertEqual(rows[1]["omitted"], len(json.dumps({"output": output, "exit_code": 2, "error": None})))

    def test_a_short_output_goes_whole_and_unmarked(self):
        content = json.dumps({"output": "ok", "exit_code": 0})
        rows = history.slim_rows([calls(1, ("c1", "terminal", {"command": "true"})), result(2, "c1", content)])

        self.assertEqual(rows[1]["content"], content)
        self.assertNotIn("omitted", rows[1])

    def test_an_edit_goes_whole_so_its_diff_can_be_drawn(self):
        diff = json.dumps({"diff": "@@\n" + "+line\n" * 1000, "files_modified": ["/home/orion/a.txt"]})
        rows = history.slim_rows([calls(1, ("c1", "patch", {"path": "/home/orion/a.txt"})), result(2, "c1", diff)])

        self.assertEqual(rows[1]["content"], diff)
        self.assertNotIn("omitted", rows[1])

    def test_a_plain_text_output_keeps_its_end(self):
        text = "x" * 5000 + "\nthe last line"
        rows = history.slim_rows([calls(1, ("c1", "web_extract", {"url": "https://example.com"})), result(2, "c1", text)])

        self.assertTrue(rows[1]["content"].endswith("the last line"))
        self.assertTrue(rows[1]["content"].startswith(history.ELLIPSIS))

    def test_a_picture_tool_keeps_where_its_picture_is(self):
        content = json.dumps({"host_image": "/home/orion/.hermes/images/a.png", "prompt": "p" * 4000})
        rows = history.slim_rows([calls(1, ("c1", "image_generate", {"prompt": "a cat"})), result(2, "c1", content)])

        self.assertEqual(json.loads(rows[1]["content"])["host_image"], "/home/orion/.hermes/images/a.png")

    def test_a_big_nested_value_is_cut_to_its_end(self):
        content = json.dumps({"results": [{"title": f"r{n}", "url": f"https://example.com/{n}"} for n in range(200)], "query": "q"})
        rows = history.slim_rows([calls(1, ("c1", "web_search", {"query": "q"})), result(2, "c1", content)])
        slim = json.loads(rows[1]["content"])

        self.assertEqual(slim["query"], "q")
        self.assertIsInstance(slim["results"], str)
        self.assertLessEqual(len(slim["results"]), history.NESTED_LONG + 1)

    def test_long_arguments_keep_their_start(self):
        rows = history.slim_rows([calls(1, ("c1", "write_file", {"path": "/home/orion/notes.md", "content": "y" * 9000}))])
        arguments = json.loads(rows[0]["tool_calls"][0]["function"]["arguments"])

        self.assertEqual(arguments["path"], "/home/orion/notes.md", "the step is still named by its file")
        self.assertLessEqual(len(arguments["content"]), history.ARG_HEAD + 1)
        self.assertIn("omitted", rows[0])

    def test_a_full_row_drops_only_what_is_never_drawn(self):
        row = result(2, "c1", "z" * 9000, api_content="never drawn")

        self.assertEqual(history.full_row(row), {"id": 2, "role": "tool", "tool_call_id": "c1", "content": "z" * 9000})


class Turns(unittest.TestCase):
    def setUp(self):
        self.rows = []
        row_id = 0

        for turn in range(5):
            row_id += 1
            self.rows.append(user(f"question {turn}", row_id))

            for step in range(3):
                row_id += 1
                self.rows.append(calls(row_id, (f"c{row_id}", "terminal", {"command": "ls"})))
                row_id += 1
                self.rows.append(result(row_id, f"c{row_id - 1}", "ok"))

            row_id += 1
            self.rows.append(reply(f"answer {turn}", row_id))

    def test_a_page_is_counted_in_turns_not_rows(self):
        kept = history.cut_to_turns(self.rows, 2)

        self.assertEqual(kept[0]["content"], "question 3")
        self.assertEqual(kept[-1]["content"], "answer 4")
        self.assertEqual(history.turns_in(kept), 2)

    def test_fewer_turns_than_asked_keeps_everything(self):
        self.assertEqual(history.cut_to_turns(self.rows, 50), self.rows)

    def test_hidden_or_empty_user_rows_start_no_turn(self):
        rows = [user("real", 1), {"id": 2, "role": "user", "content": "[System: nudge]", "display_kind": "hidden"}, {"id": 3, "role": "user", "content": "  "}]

        self.assertEqual(history.turns_in(rows), 1)


if __name__ == "__main__":
    unittest.main()
