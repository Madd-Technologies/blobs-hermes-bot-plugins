"""Filters, sorting, numbers and charts over a collection's records, and views computed for the app.

A bot never sends SQL: it sends a small structured filter, checked against the collection's fields here, and the server
works out every view (rows, columns, a figure, a series), so the app only draws what it is given. Collections are small
(thousands of records at most), so this runs in Python over the records.

Times: `datetime` fields and the record times are epoch seconds; `date` fields are `YYYY-MM-DD` in the server's own time
zone (a home server's is its person's). Weeks start on Monday.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

try:
    from . import fields as F
except ImportError:  # the tests import the modules flat
    import fields as F  # type: ignore[no-redef]

#: Record times a filter, a sort or a chart can use besides the collection's own fields.
SPECIAL = {"_created": "created_at", "_updated": "updated_at", "_closed": "closed_at"}

OPS = ("eq", "ne", "in", "nin", "lt", "lte", "gt", "gte", "empty", "not_empty", "contains", "within_days", "in_last_days", "older_than_days", "is_open", "is_done")
NO_VALUE = ("empty", "not_empty", "is_open", "is_done")
DAYS_OPS = ("within_days", "in_last_days", "older_than_days")

KINDS = ("list", "checklist", "board", "table", "number", "chart", "summary", "stat")
AGGREGATES = ("count", "sum", "avg", "min", "max")
BUCKETS = ("day", "week", "month")
MAX_DEPTH = 4
MAX_CONDITIONS = 20
MAX_ROWS = 200
MAX_COLUMNS = 8
MAX_BUCKETS = 26
DONE_SHOWN = 3


class QueryError(ValueError):
    """A filter or view a bot sent that can't be used; the message says what to change."""


# -- fields a query may name ---------------------------------------------------------------------------------------


def _field_type(key: str, by_key: dict) -> str:
    if key in SPECIAL:
        return "datetime"

    field = by_key.get(key)

    if field is None:
        raise QueryError(f"no field {key!r} (fields: {', '.join(by_key)}; or _created, _updated, _closed)")

    return field["type"]


def value_of(record: dict, key: str):
    if key in SPECIAL:
        return record.get(SPECIAL[key])

    return (record.get("values") or {}).get(key)


# -- filters --------------------------------------------------------------------------------------------------------


def clean_filter(raw, fields: list[dict], depth: int = 0, count: list | None = None) -> dict | None:
    """A filter, checked against the fields. `{all: [...]}`, `{any: [...]}`, `{not: ...}` or `{field, op, value}`."""
    if raw in (None, {}, []):
        return None

    count = count if count is not None else [0]
    by_key = {field["key"]: field for field in fields}

    if depth > MAX_DEPTH:
        raise QueryError("a filter nests too deep")

    if not isinstance(raw, dict):
        raise QueryError("a filter is an object: {all: [...]}, {any: [...]}, {not: ...} or {field, op, value}")

    for group in ("all", "any"):
        if group in raw:
            parts = raw[group]

            if not isinstance(parts, list) or not parts:
                raise QueryError(f"{group} needs a list of conditions")

            return {group: [part for part in (clean_filter(item, fields, depth + 1, count) for item in parts) if part]}

    if "not" in raw:
        inner = clean_filter(raw["not"], fields, depth + 1, count)

        return {"not": inner} if inner else None

    count[0] += 1

    if count[0] > MAX_CONDITIONS:
        raise QueryError(f"at most {MAX_CONDITIONS} conditions in a filter")

    op = str(raw.get("op") or "eq").strip().lower()

    if op not in OPS:
        raise QueryError(f"op must be one of {', '.join(OPS)}")

    if op in ("is_open", "is_done"):
        return {"op": op}

    key = str(raw.get("field") or "").strip()
    kind = _field_type(key, by_key)
    condition = {"field": key, "op": op}

    if op in NO_VALUE:
        return condition

    value = raw.get("value")

    if op in DAYS_OPS:
        if kind not in ("date", "datetime"):
            raise QueryError(f"{op} works on a date or datetime field, not {key} ({kind})")

        try:
            days = int(value)
        except (TypeError, ValueError):
            raise QueryError(f"{op} needs a whole number of days") from None

        if not 0 <= days <= 3650:
            raise QueryError(f"{op}: days must be 0 to 3650")

        return {**condition, "value": days}

    if op in ("in", "nin"):
        if not isinstance(value, list) or not value:
            raise QueryError(f"{op} needs a list of values")

        return {**condition, "value": [_comparable(by_key, key, kind, item) for item in value[:50]]}

    if op == "contains":
        if kind not in ("text", "longtext", "draft", "email", "phone", "url", "link"):
            raise QueryError(f"contains works on text fields, not {key} ({kind})")

        return {**condition, "value": str(value or "").lower()[:100]}

    if op in ("lt", "lte", "gt", "gte") and kind in ("text", "longtext", "draft", "checkbox", "choice", "phone", "email", "url", "link"):
        raise QueryError(f"{op} works on numbers, dates and statuses, not {key} ({kind})")

    return {**condition, "value": _comparable(by_key, key, kind, value)}


def _comparable(by_key: dict, key: str, kind: str, value):
    """A filter's value in the same form as stored values, so they compare."""
    if key in SPECIAL:
        return F.parse_when(value, key)

    try:
        return F.clean_value({**by_key[key], "required": False}, value)
    except F.FieldError as error:
        raise QueryError(str(error)) from None


def _today(now: float) -> date:
    return datetime.fromtimestamp(now).date()


def _rank(by_key: dict, key: str, value):
    """Statuses compare by stage order; everything else by value."""
    field = by_key.get(key)

    if field and field["type"] == "status" and value is not None:
        names = [stage["name"] for stage in field["stages"]]

        return names.index(value) if value in names else -1

    return value


def matches(spec: dict | None, record: dict, fields: list[dict], now: float) -> bool:
    if not spec:
        return True

    if "all" in spec:
        return all(matches(part, record, fields, now) for part in spec["all"])

    if "any" in spec:
        return any(matches(part, record, fields, now) for part in spec["any"])

    if "not" in spec:
        return not matches(spec["not"], record, fields, now)

    op = spec["op"]

    if op == "is_open":
        return not record.get("closed_at")

    if op == "is_done":
        return bool(record.get("closed_at"))

    by_key = {field["key"]: field for field in fields}
    key = spec["field"]
    actual = value_of(record, key)

    if op == "empty":
        return actual in (None, "")

    if op == "not_empty":
        return actual not in (None, "")

    if actual is None:
        return op in ("ne", "nin")

    kind = _field_type(key, by_key)

    if op in DAYS_OPS:
        days = spec["value"]

        if kind == "date":
            when = date.fromisoformat(actual)
            today = _today(now)

            if op == "within_days":
                return today <= when <= today + timedelta(days=days)

            if op == "in_last_days":
                return today - timedelta(days=days) <= when <= today

            return when < today - timedelta(days=days)

        if op == "within_days":
            return now <= actual <= now + days * 86_400

        if op == "in_last_days":
            return now - days * 86_400 <= actual <= now

        return actual < now - days * 86_400

    expected = spec["value"]

    if op == "contains":
        return expected in str(actual).lower()

    if op == "in":
        return actual in expected

    if op == "nin":
        return actual not in expected

    if op == "eq":
        return actual == expected

    if op == "ne":
        return actual != expected

    left, right = _rank(by_key, key, actual), _rank(by_key, key, expected)

    try:
        return {"lt": left < right, "lte": left <= right, "gt": left > right, "gte": left >= right}[op]
    except TypeError:
        return False


# -- sorting --------------------------------------------------------------------------------------------------------


def clean_sort(raw, fields: list[dict]) -> list[dict]:
    if raw in (None, []):
        return [{"field": "_updated", "dir": "desc"}]

    raw = raw if isinstance(raw, list) else [raw]
    by_key = {field["key"]: field for field in fields}
    order = []

    for item in raw[:3]:
        item = {"field": item} if isinstance(item, str) else item

        if not isinstance(item, dict):
            raise QueryError("sort is a list of {field, dir}")

        key = str(item.get("field") or "")
        _field_type(key, by_key)
        direction = str(item.get("dir") or "asc").lower()

        if direction not in ("asc", "desc"):
            raise QueryError("sort dir is asc or desc")

        order.append({"field": key, "dir": direction})

    return order


def sort_records(records: list[dict], order: list[dict], fields: list[dict]) -> list[dict]:
    by_key = {field["key"]: field for field in fields}
    result = list(records)

    # Stable sorts, last key first; empty values always go last.
    for item in reversed(order):
        key, reverse = item["field"], item["dir"] == "desc"
        present = [record for record in result if value_of(record, key) not in (None, "")]
        empty = [record for record in result if value_of(record, key) in (None, "")]
        present.sort(key=lambda record: _sortable(_rank(by_key, key, value_of(record, key))), reverse=reverse)
        result = present + empty

    return result


def _sortable(value):
    return (0, value) if isinstance(value, (int, float)) else (1, str(value).lower())


# -- numbers and charts -------------------------------------------------------------------------------------------


def clean_aggregate(raw, fields: list[dict]) -> dict:
    raw = raw or {"op": "count"}

    if isinstance(raw, str):
        raw = {"op": raw}

    op = str(raw.get("op") or "count").lower()

    if op not in AGGREGATES:
        raise QueryError(f"aggregate op must be one of {', '.join(AGGREGATES)}")

    if op == "count":
        return {"op": "count"}

    key = str(raw.get("field") or "")
    kind = _field_type(key, {field["key"]: field for field in fields})

    if kind not in ("number", "money", "percent", "rating"):
        raise QueryError(f"{op} needs a number field, not {key} ({kind})")

    return {"op": op, "field": key}


def aggregate(records: list[dict], spec: dict):
    if spec["op"] == "count":
        return len(records)

    numbers = [value_of(record, spec["field"]) for record in records]
    numbers = [number for number in numbers if isinstance(number, (int, float))]

    if not numbers:
        return None

    if spec["op"] == "sum":
        total = sum(numbers)
    elif spec["op"] == "avg":
        total = sum(numbers) / len(numbers)
    elif spec["op"] == "min":
        total = min(numbers)
    else:
        total = max(numbers)

    return int(total) if float(total).is_integer() else round(total, 2)


def _as_date(value, kind: str) -> date | None:
    if value is None:
        return None

    return date.fromisoformat(value) if kind == "date" else datetime.fromtimestamp(value).date()


def bucket_start(day: date, unit: str) -> date:
    if unit == "day":
        return day

    if unit == "week":
        return day - timedelta(days=day.weekday())

    return day.replace(day=1)


def _shift(start: date, unit: str, steps: int) -> date:
    if unit == "day":
        return start + timedelta(days=steps)

    if unit == "week":
        return start + timedelta(weeks=steps)

    month = start.month - 1 + steps

    return date(start.year + month // 12, month % 12 + 1, 1)


def _period_field(raw, fields: list[dict]) -> tuple[str, str]:
    key = str(raw or "_created")
    kind = _field_type(key, {field["key"]: field for field in fields})

    if kind not in ("date", "datetime"):
        raise QueryError(f"a period or chart needs a date or datetime field, not {key} ({kind})")

    return key, kind


def series(records: list[dict], spec: dict, fields: list[dict], now: float) -> list[dict]:
    """`count` buckets of `unit` ending with the current one: `[{start, value}]`, oldest first."""
    kind = _field_type(spec["field"], {field["key"]: field for field in fields})
    current = bucket_start(_today(now), spec["bucket"])
    starts = [_shift(current, spec["bucket"], -steps) for steps in range(spec["count"] - 1, -1, -1)]
    grouped: dict[date, list] = {start: [] for start in starts}

    for record in records:
        day = _as_date(value_of(record, spec["field"]), kind)

        if day is not None:
            start = bucket_start(day, spec["bucket"])

            if start in grouped:
                grouped[start].append(record)

    return [{"start": start.isoformat(), "value": aggregate(grouped[start], spec["aggregate"]) or 0} for start in starts]


# -- views ----------------------------------------------------------------------------------------------------------


def _key(raw, fields: list[dict], what: str, types: tuple | None = None) -> str:
    key = str(raw or "").strip()
    kind = _field_type(key, {field["key"]: field for field in fields})

    if types and kind not in types:
        raise QueryError(f"{what} needs a {' or '.join(types)} field, not {key} ({kind})")

    return key


def clean_view_config(kind: str, raw, fields: list[dict] | None) -> dict:
    """A view's config for its kind, checked against its collection's fields (None for summary and stat)."""
    if kind not in KINDS:
        raise QueryError(f"kind must be one of {', '.join(KINDS)}")

    raw = raw or {}

    if not isinstance(raw, dict):
        raise QueryError("config must be an object")

    if kind == "summary":
        text = F.clean_value({"key": "text", "type": "longtext", "required": True}, raw.get("text"))

        return {"text": text}

    if kind == "stat":
        metrics = raw.get("metrics")

        if not isinstance(metrics, list) or not 1 <= len(metrics) <= 6:
            raise QueryError("a stat view needs 1 to 6 metrics: [{label, value, change}]")

        cleaned = []

        for metric in metrics:
            if not isinstance(metric, dict):
                raise QueryError("each metric is {label, value, change}")

            entry = {"label": F.clean_value({"key": "label", "type": "text", "required": True}, metric.get("label"))[:30], "value": F.clean_value({"key": "value", "type": "text", "required": True}, metric.get("value"))[:16]}
            change = F.clean_value({"key": "change", "type": "text"}, metric.get("change"))

            if change:
                entry["change"] = change[:16]

            cleaned.append(entry)

        return {"metrics": cleaned}

    if fields is None:
        raise QueryError(f"a {kind} view needs a collection")

    config: dict = {}
    config["filter"] = clean_filter(raw.get("filter"), fields)
    config["needs_you"] = clean_filter(raw.get("needs_you"), fields)

    if kind in ("list", "checklist", "board", "table"):
        config["sort"] = clean_sort(raw.get("sort"), fields)
        config["limit"] = max(1, min(int(raw.get("limit") or 50), MAX_ROWS))
        config["include_closed"] = bool(raw.get("include_closed"))

    if kind in ("list", "checklist", "board"):
        show = raw.get("show") or {}

        if not isinstance(show, dict) or not show.get("title"):
            raise QueryError("show needs at least a title field: {title, subtitle, meta}")

        config["show"] = {slot: _key(show[slot], fields, f"show.{slot}") for slot in ("title", "subtitle", "meta", "link") if show.get(slot)}

    if kind == "board":
        group = _key(raw.get("group_by"), fields, "group_by", ("status", "choice"))
        config["group_by"] = group
        config["closed_within_days"] = max(0, min(int(raw.get("closed_within_days", 7)), 90))

    if kind == "table":
        columns = raw.get("columns")

        if not isinstance(columns, list) or not 1 <= len(columns) <= MAX_COLUMNS:
            raise QueryError(f"a table needs 1 to {MAX_COLUMNS} columns (field keys)")

        config["columns"] = [_key(column, fields, "columns") for column in columns]

    if kind in ("number", "chart"):
        config["aggregate"] = clean_aggregate(raw.get("aggregate"), fields)

    if kind == "number":
        period = raw.get("period")

        if period:
            if not isinstance(period, dict):
                raise QueryError("period is {field, unit, compare}")

            key, _ = _period_field(period.get("field"), fields)
            unit = str(period.get("unit") or "week").lower()

            if unit not in BUCKETS:
                raise QueryError(f"period unit must be one of {', '.join(BUCKETS)}")

            config["period"] = {"field": key, "unit": unit, "compare": bool(period.get("compare", True))}

    if kind == "chart":
        key, _ = _period_field(raw.get("field"), fields)
        bucket = str(raw.get("bucket") or "week").lower()

        if bucket not in BUCKETS:
            raise QueryError(f"bucket must be one of {', '.join(BUCKETS)}")

        config.update({"field": key, "bucket": bucket, "count": max(2, min(int(raw.get("count") or 8), MAX_BUCKETS)), "style": "line" if raw.get("style") == "line" else "bar"})

    return {key: value for key, value in config.items() if value is not None}


def _typed(field_by_key: dict, key: str | None, record: dict) -> dict | None:
    if not key:
        return None

    value = value_of(record, key)

    if value in (None, ""):
        return None

    kind = "datetime" if key in SPECIAL else field_by_key[key]["type"]
    typed = {"key": key, "type": kind, "value": value}

    if kind == "money":
        typed["currency"] = field_by_key[key].get("currency", "USD")

    return typed


def _row(record: dict, config: dict, fields: list[dict], now: float) -> dict:
    by_key = {field["key"]: field for field in fields}
    show = config.get("show") or {}
    row = {"id": record["id"], "done": bool(record.get("closed_at")), "needs_you": needs_you(record, config, fields, now)}

    if muted(record, now):
        row["muted_until"] = record["muted_until"]

    for slot in ("title", "subtitle", "meta", "link"):
        typed = _typed(by_key, show.get(slot), record)

        if typed:
            row[slot] = typed

    return row


# A button's job, once pressed, has this long to come back before the record can need the person again.
RUNNING_S = 30 * 60


def running(record: dict, now: float) -> dict | None:
    """The button job a record is waiting on, if it was pressed less than RUNNING_S ago and the bot has not written since."""
    job = record.get("running")

    return job if job and now - float(job.get("at") or 0) < RUNNING_S else None


def muted(record: dict, now: float) -> bool:
    """Whether the person muted this record and the time they picked has not come yet."""
    until = record.get("muted_until")

    return bool(until) and float(until) > now


def needs_you(record: dict, config: dict, fields: list[dict], now: float) -> bool:
    rule = config.get("needs_you")

    return bool(rule) and not record.get("closed_at") and not running(record, now) and not muted(record, now) and matches(rule, record, fields, now)


def _muted_last(records: list[dict], now: float) -> list[dict]:
    """A muted record waits at the bottom of its panel; the order is otherwise kept."""
    return [record for record in records if not muted(record, now)] + [record for record in records if muted(record, now)]


def compute_view(view: dict, fields: list[dict] | None, records: list[dict], now: float) -> dict:
    """What the app draws for one view: rows, columns, a figure or a series, plus its counts."""
    kind, config = view["kind"], view.get("config") or {}
    result = {"id": view["id"], "title": view["title"], "kind": kind, "collection": view.get("collection"), "updated": view.get("updated_at")}

    if kind == "summary":
        return {**result, "text": config.get("text", "")}

    if kind == "stat":
        return {**result, "metrics": config.get("metrics", [])}

    fields = fields or []
    chosen = [record for record in records if matches(config.get("filter"), record, fields, now)]
    open_ = [record for record in chosen if not record.get("closed_at")]
    flagged = [record for record in open_ if needs_you(record, config, fields, now)]
    result.update({"open": len(open_), "needs_you": len(flagged)})

    if kind in ("list", "checklist", "table"):
        shown = chosen if config.get("include_closed") else open_
        shown = _muted_last(sort_records(shown, config.get("sort") or [], fields)[: config.get("limit", 50)], now)

        if kind == "checklist" and not config.get("include_closed"):
            # Its last few done items stay under the open ones, so a tick can be undone.
            done = sorted((record for record in chosen if record.get("closed_at")), key=lambda record: record["closed_at"], reverse=True)[:DONE_SHOWN]
            shown = shown + done

        if kind == "table":
            by_key = {field["key"]: field for field in fields}
            columns = config.get("columns", [])
            result["columns"] = [{"key": key, "label": by_key[key]["label"] if key in by_key else key.strip("_").capitalize(), "type": "datetime" if key in SPECIAL else by_key[key]["type"]} for key in columns]
            result["rows"] = [{"id": record["id"], "done": bool(record.get("closed_at")), "needs_you": needs_you(record, config, fields, now), **({"muted_until": record["muted_until"]} if muted(record, now) else {}), "cells": [_typed(by_key, key, record) for key in columns]} for record in shown]
        else:
            result["rows"] = [_row(record, config, fields, now) for record in shown]

        return result

    if kind == "board":
        by_key = {field["key"]: field for field in fields}
        group = by_key[config["group_by"]]
        names = [stage["name"] for stage in group["stages"]] if group["type"] == "status" else list(group["options"])
        cutoff = now - config.get("closed_within_days", 7) * 86_400
        visible = [record for record in chosen if not record.get("closed_at") or record["closed_at"] >= cutoff]
        ordered = _muted_last(sort_records(visible, config.get("sort") or [], fields), now)
        result["columns"] = [{"name": name, "rows": [_row(record, config, fields, now) for record in ordered if value_of(record, group["key"]) == name][: config.get("limit", 50)]} for name in names]

        for column in result["columns"]:
            column["count"] = len(column["rows"])

        return result

    if kind == "number":
        period = config.get("period")

        if not period:
            return {**result, "value": aggregate(open_, config["aggregate"])}

        kind_of_field = _field_type(period["field"], {field["key"]: field for field in fields})
        current = bucket_start(_today(now), period["unit"])
        previous = _shift(current, period["unit"], -1)

        def within(record, start):
            day = _as_date(value_of(record, period["field"]), kind_of_field)
            return day is not None and bucket_start(day, period["unit"]) == start

        value = aggregate([record for record in chosen if within(record, current)], config["aggregate"])
        result.update({"value": value, "period": period["unit"]})

        if period.get("compare"):
            before = aggregate([record for record in chosen if within(record, previous)], config["aggregate"])
            result["previous"] = before

        return result

    points = series(chosen, {"field": config["field"], "bucket": config["bucket"], "count": config["count"], "aggregate": config["aggregate"]}, fields, now)

    return {**result, "bucket": config["bucket"], "style": config.get("style", "bar"), "points": points}
