import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fields as F  # noqa: E402
import query as Q  # noqa: E402

FIELDS = F.clean_fields(
    [
        {"key": "customer", "type": "text"},
        {"key": "status", "type": "status", "stages": [{"name": "New"}, {"name": "Replied"}, {"name": "Waiting"}, {"name": "Done", "done": True}]},
        {"key": "channel", "type": "choice", "options": ["Walk-in", "Web", "Phone"]},
        {"key": "reply_by", "type": "date"},
        {"key": "received", "type": "datetime"},
        {"key": "units", "type": "number"},
        {"key": "quote", "type": "money"},
    ]
)

#: Tuesday 13 October 2026, 10:00 local.
NOW = datetime(2026, 10, 13, 10).timestamp()
DAY = 86_400


def rec(rid, closed=None, created=None, **values):
    return {"id": rid, "values": values, "created_at": created or NOW - DAY, "updated_at": created or NOW - DAY, "closed_at": closed}


RECORDS = [
    rec("a", customer="Jane", status="New", channel="Walk-in", reply_by="2026-10-13", received=NOW - 3600, units=3, quote=600),
    rec("b", customer="Lee", status="Replied", channel="Web", reply_by="2026-10-20", received=NOW - 2 * DAY, units=5, quote=1000),
    rec("c", customer="Sam", status="Waiting", channel="Phone", received=NOW - 9 * DAY, units=2),
    rec("d", customer="Ana", status="Done", channel="Walk-in", received=NOW - 20 * DAY, units=4, quote=800, closed=NOW - 2 * DAY),
]


def ids(spec):
    spec = Q.clean_filter(spec, FIELDS)
    return [record["id"] for record in RECORDS if Q.matches(spec, record, FIELDS, NOW)]


class Filters(unittest.TestCase):
    def test_conditions(self):
        self.assertEqual(ids({"field": "channel", "op": "eq", "value": "walk-in"}), ["a", "d"])
        self.assertEqual(ids({"field": "channel", "op": "in", "value": ["Web", "Phone"]}), ["b", "c"])
        self.assertEqual(ids({"op": "is_open"}), ["a", "b", "c"])
        self.assertEqual(ids({"op": "is_done"}), ["d"])
        self.assertEqual(ids({"field": "reply_by", "op": "within_days", "value": 0}), ["a"])
        self.assertEqual(ids({"field": "reply_by", "op": "within_days", "value": 7}), ["a", "b"])
        self.assertEqual(ids({"field": "received", "op": "in_last_days", "value": 7}), ["a", "b"])
        self.assertEqual(ids({"field": "received", "op": "older_than_days", "value": 7}), ["c", "d"])
        self.assertEqual(ids({"field": "quote", "op": "empty"}), ["c"])
        self.assertEqual(ids({"field": "customer", "op": "contains", "value": "an"}), ["a", "d"])
        self.assertEqual(ids({"field": "units", "op": "gte", "value": 4}), ["b", "d"])

    def test_statuses_compare_by_stage_order(self):
        self.assertEqual(ids({"field": "status", "op": "lt", "value": "Waiting"}), ["a", "b"])

    def test_groups(self):
        self.assertEqual(ids({"all": [{"op": "is_open"}, {"any": [{"field": "status", "op": "eq", "value": "New"}, {"field": "units", "op": "gt", "value": 4}]}]}), ["a", "b"])
        self.assertEqual(ids({"not": {"field": "channel", "op": "eq", "value": "Walk-in"}}), ["b", "c"])

    def test_a_bot_is_told_what_is_wrong(self):
        cases = [
            ({"field": "colour", "op": "eq", "value": 1}, "no field 'colour'"),
            ({"field": "customer", "op": "near", "value": 1}, "op must be one of"),
            ({"field": "customer", "op": "within_days", "value": 3}, "date or datetime"),
            ({"field": "customer", "op": "gt", "value": "x"}, "numbers, dates and statuses"),
            ({"field": "status", "op": "eq", "value": "Archived"}, "must be one of"),
            ({"field": "reply_by", "op": "within_days", "value": "soon"}, "whole number of days"),
        ]

        for spec, words in cases:
            with self.subTest(words=words):
                with self.assertRaises((Q.QueryError, F.FieldError)) as caught:
                    Q.clean_filter(spec, FIELDS)
                self.assertIn(words, str(caught.exception))


class Sorting(unittest.TestCase):
    def test_sorting_puts_empty_values_last(self):
        order = Q.clean_sort([{"field": "reply_by", "dir": "asc"}], FIELDS)
        self.assertEqual([record["id"] for record in Q.sort_records(RECORDS, order, FIELDS)], ["a", "b", "c", "d"])
        order = Q.clean_sort([{"field": "status", "dir": "desc"}], FIELDS)
        self.assertEqual([record["id"] for record in Q.sort_records(RECORDS, order, FIELDS)], ["d", "c", "b", "a"])


class NumbersAndCharts(unittest.TestCase):
    def test_aggregates(self):
        self.assertEqual(Q.aggregate(RECORDS, {"op": "count"}), 4)
        self.assertEqual(Q.aggregate(RECORDS, {"op": "sum", "field": "quote"}), 2400)
        self.assertEqual(Q.aggregate(RECORDS, {"op": "avg", "field": "units"}), 3.5)
        with self.assertRaises(Q.QueryError):
            Q.clean_aggregate({"op": "sum", "field": "customer"}, FIELDS)

    def test_a_weekly_series_ends_with_this_week(self):
        points = Q.series(RECORDS, {"field": "received", "bucket": "week", "count": 4, "aggregate": {"op": "count"}}, FIELDS, NOW)
        self.assertEqual([point["start"] for point in points], ["2026-09-21", "2026-09-28", "2026-10-05", "2026-10-12"])
        # Received Sep 23, Sun Oct 4, Sun Oct 11 and today: one in each Monday-to-Sunday week.
        self.assertEqual([point["value"] for point in points], [1, 1, 1, 1])

    def test_a_number_against_last_week(self):
        view = {"id": "v", "title": "Inquiries", "kind": "number", "config": Q.clean_view_config("number", {"aggregate": "count", "period": {"field": "received", "unit": "week"}}, FIELDS)}
        result = Q.compute_view(view, FIELDS, RECORDS, NOW)
        self.assertEqual((result["value"], result["previous"]), (1, 1))


class Views(unittest.TestCase):
    def view(self, kind, config):
        return {"id": "v", "title": "V", "kind": kind, "collection": "concierge/support-emails", "config": Q.clean_view_config(kind, config, FIELDS), "updated_at": NOW}

    def test_a_list_with_a_needs_you_rule(self):
        result = Q.compute_view(self.view("list", {"show": {"title": "customer", "subtitle": "channel", "meta": "reply_by"}, "sort": [{"field": "reply_by"}], "needs_you": {"field": "reply_by", "op": "within_days", "value": 0}}), FIELDS, RECORDS, NOW)
        self.assertEqual([row["id"] for row in result["rows"]], ["a", "b", "c"])
        self.assertEqual(result["rows"][0]["needs_you"], True)
        self.assertEqual(result["rows"][0]["meta"], {"key": "reply_by", "type": "date", "value": "2026-10-13"})
        self.assertEqual((result["open"], result["needs_you"]), (3, 1))

    def test_a_board_groups_by_stage_and_keeps_recently_closed(self):
        result = Q.compute_view(self.view("board", {"group_by": "status", "show": {"title": "customer"}}), FIELDS, RECORDS, NOW)
        self.assertEqual([(column["name"], column["count"]) for column in result["columns"]], [("New", 1), ("Replied", 1), ("Waiting", 1), ("Done", 1)])
        older = Q.compute_view(self.view("board", {"group_by": "status", "show": {"title": "customer"}, "closed_within_days": 1}), FIELDS, RECORDS, NOW)
        self.assertEqual(older["columns"][3]["count"], 0)

    def test_a_table(self):
        result = Q.compute_view(self.view("table", {"columns": ["customer", "units", "quote"]}), FIELDS, RECORDS, NOW)
        self.assertEqual([column["key"] for column in result["columns"]], ["customer", "units", "quote"])
        self.assertEqual(result["rows"][0]["cells"][2], {"key": "quote", "type": "money", "value": 600, "currency": "USD"})

    def test_a_chart(self):
        result = Q.compute_view(self.view("chart", {"aggregate": "count", "field": "received", "bucket": "week", "count": 2}), FIELDS, RECORDS, NOW)
        self.assertEqual([point["value"] for point in result["points"]], [1, 1])

    def test_views_that_hold_their_own_content(self):
        self.assertEqual(Q.clean_view_config("summary", {"text": "**All quiet.**"}, None), {"text": "**All quiet.**"})
        self.assertEqual(Q.clean_view_config("stat", {"metrics": [{"label": "Resolved", "value": "76%", "change": "+4"}]}, None)["metrics"][0]["value"], "76%")
        with self.assertRaises(Q.QueryError):
            Q.clean_view_config("list", {"show": {}}, FIELDS)
        with self.assertRaises(Q.QueryError):
            Q.clean_view_config("board", {"group_by": "customer", "show": {"title": "customer"}}, FIELDS)


if __name__ == "__main__":
    unittest.main()
