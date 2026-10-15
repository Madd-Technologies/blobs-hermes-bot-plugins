import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fields as F  # noqa: E402

STATUS = {"key": "status", "label": "Status", "type": "status", "stages": [{"name": "New"}, {"name": "Replied"}, {"name": "Done", "done": True}]}


class Definitions(unittest.TestCase):
    def test_a_collection_of_typed_fields(self):
        fields = F.clean_fields([{"key": "customer", "type": "text", "required": True}, STATUS, {"key": "channel", "type": "choice", "options": ["Walk-in", "Web", "Phone"]}, {"key": "quote", "type": "money"}])
        self.assertEqual([field["type"] for field in fields], ["text", "status", "choice", "money"])
        self.assertEqual(fields[0]["label"], "Customer")
        self.assertEqual(fields[3]["currency"], "USD")
        self.assertEqual(F.status_field(fields)["key"], "status")
        self.assertEqual(F.done_stages(fields[1]), {"Done"})
        self.assertEqual(F.first_stage(fields[1], done=False), "New")

    def test_a_draft_names_who_it_is_for(self):
        fields = F.clean_fields([{"key": "customer", "type": "text"}, {"key": "reply", "label": "Draft reply", "type": "draft", "to": "customer"}])
        self.assertEqual(fields[1]["to"], "customer")
        self.assertEqual(F.clean_value(fields[1], "Hi Sam,\n\nIt works."), "Hi Sam,\n\nIt works.")
        with self.assertRaises(F.FieldError):
            F.clean_fields([{"key": "reply", "type": "draft", "to": "nobody"}])

    def test_what_a_bot_gets_told_to_fix(self):
        cases = [
            ([{"key": "Customer Name", "type": "text"}], "must start with a letter"),
            ([{"key": "x", "type": "colour"}], "type must be one of"),
            ([{"key": "s", "type": "status", "stages": ["Only"]}], "2 to"),
            ([{"key": "s", "type": "status", "stages": ["A", "B"]}], "mark at least one stage done"),
            ([{"key": "s", "type": "status", "stages": [{"name": "A", "done": True}, {"name": "B", "done": True}]}], "at least one stage must be open"),
            ([{"key": "c", "type": "choice", "options": ["a", "A"]}], "different"),
            ([{"key": "a", "type": "text"}, {"key": "a", "type": "text"}], "share a key"),
            ([STATUS, {**STATUS, "key": "phase"}], "at most one status"),
            ([], "needs fields"),
        ]

        for raw, words in cases:
            with self.subTest(words=words):
                with self.assertRaises(F.FieldError) as caught:
                    F.clean_fields(raw)
                self.assertIn(words, str(caught.exception))


class Values(unittest.TestCase):
    def check(self, field, value):
        return F.clean_value(F.clean_field(field), value)

    def test_each_type(self):
        self.assertEqual(self.check({"key": "t", "type": "text"}, "  Early\ndelivery \x07"), "Early delivery")
        self.assertEqual(self.check({"key": "n", "type": "number"}, "1,200"), 1200)
        self.assertEqual(self.check({"key": "m", "type": "money"}, "$420.50"), 420.5)
        self.assertEqual(self.check({"key": "p", "type": "percent"}, "76%"), 76)
        self.assertEqual(self.check({"key": "d", "type": "date"}, "2026-10-16T00:00"), "2026-10-16")
        self.assertEqual(self.check({"key": "w", "type": "datetime"}, "2026-10-13T13:14:00Z"), 1791897240)
        self.assertEqual(self.check({"key": "w", "type": "datetime"}, 1791897240000), 1791897240)
        self.assertIs(self.check({"key": "c", "type": "checkbox"}, "yes"), True)
        self.assertEqual(self.check(STATUS, "replied"), "Replied")
        self.assertEqual(self.check({"key": "c", "type": "choice", "options": ["Walk-in"]}, "WALK-IN"), "Walk-in")
        self.assertEqual(self.check({"key": "ph", "type": "phone"}, "+1 (555) 555-0143"), "+1 (555) 555-0143")
        self.assertEqual(self.check({"key": "e", "type": "email"}, "customer@example.com"), "customer@example.com")
        self.assertEqual(self.check({"key": "u", "type": "url"}, "https://mail.google.com/x"), "https://mail.google.com/x")
        self.assertEqual(self.check({"key": "l", "type": "link"}, "/api/plugins/hermes-drive/files/abc/preview"), "/api/plugins/hermes-drive/files/abc/preview")
        self.assertEqual(self.check({"key": "r", "type": "rating"}, 4), 4)
        self.assertIsNone(self.check({"key": "t", "type": "text"}, "  "))

    def test_bad_values_say_what_to_change(self):
        cases = [
            ({"key": "n", "type": "number"}, "lots", "expected a number"),
            ({"key": "d", "type": "date"}, "2026-02-30", "not a real date"),
            ({"key": "w", "type": "datetime"}, "tomorrow", "ISO 8601"),
            (STATUS, "Archived", "must be one of New, Replied, Done"),
            ({"key": "u", "type": "url"}, "http://example.com", "https://"),
            ({"key": "l", "type": "link"}, "javascript:alert(1)", "path on this server"),
            ({"key": "r", "type": "rating"}, 7, "1 to 5"),
            ({"key": "e", "type": "email"}, "not-an-email", "email address"),
            ({"key": "t", "type": "text", "required": True}, "", "required"),
        ]

        for field, value, words in cases:
            with self.subTest(words=words):
                with self.assertRaises(F.FieldError) as caught:
                    self.check(field, value)
                self.assertIn(words, str(caught.exception))

    def test_a_set_of_values(self):
        fields = F.clean_fields([{"key": "customer", "type": "text", "required": True}, STATUS])
        self.assertEqual(F.clean_values(fields, {"customer": "Sam"}, partial=False), {"customer": "Sam"})
        with self.assertRaises(F.FieldError) as caught:
            F.clean_values(fields, {"status": "New"}, partial=False)
        self.assertIn("customer is required", str(caught.exception))
        self.assertEqual(F.clean_values(fields, {"status": "New"}, partial=True), {"status": "New"})
        with self.assertRaises(F.FieldError) as caught:
            F.clean_values(fields, {"colour": "red"}, partial=True)
        self.assertIn("no field 'colour'", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
