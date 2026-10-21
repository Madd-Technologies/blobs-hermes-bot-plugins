"""The fields a bot gives a collection, and how each value is checked before it is stored.

A collection's fields are a list of `{key, label, type, ...}`. Values are kept per record as JSON, so fields can be added,
relabelled or retired without touching the table. Everything a bot (or the person, through the app) sends goes through
`clean_value`: text shown on a phone, never run.
"""

from __future__ import annotations

import re
from datetime import date, datetime

TYPES = (
    "text",
    "longtext",
    "number",
    "money",
    "percent",
    "date",
    "datetime",
    "checkbox",
    "status",
    "choice",
    "phone",
    "email",
    "url",
    "link",
    "rating",
    "draft",
    "list",
    "entries",
    "files",
)

#: Types whose value is a list of items, not one value: filters only ask whether they are empty, and they are not sorted
#: on or shown in a row slot or a table column.
STRUCTURED = ("list", "entries", "files")

#: How a longtext or draft field's text is written: plain words, Markdown, or HTML (an email, say). The app draws HTML in a
#: sealed view (no scripts, nothing loaded from the network).
FORMATS = ("plain", "markdown", "html")
TONES = ("good", "warn", "bad")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_ITEMS = {"list": 30, "entries": 50, "files": 20}

MAX_FIELDS = 60
MAX_OPTIONS = 30
KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
PHONE = re.compile(r"^\+?[\d\s().-]{3,30}$")
EMAIL = re.compile(r"^[^\s@<>]{1,64}@[^\s@<>]{1,190}\.[A-Za-z]{2,24}$")
LINK = re.compile(r"^(/[^\s<>]*|file:///[^\s<>]*|https://[^\s<>]+)$")
CURRENCY = re.compile(r"^[A-Z]{3}$")
LIMITS = {"text": 300, "longtext": 8000, "draft": 8000, "html": 200_000, "url": 1000, "link": 1000, "label": 40, "option": 40, "item": 80, "entry": 120, "detail": 200, "name": 120, "mime": 80}


class FieldError(ValueError):
    """Something that can't be stored; the message says what to change."""


def _clip(text: str, limit: int, single_line: bool = True) -> str:
    text = CONTROL.sub("", text)
    text = re.sub(r"\s+", " ", text).strip() if single_line else text.strip()

    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _label(value, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FieldError(f"{what} needs a label")

    return _clip(value, LIMITS["label"])


# -- field definitions -------------------------------------------------------------------------------------------


def clean_field(raw) -> dict:
    """One field definition, checked: `{key, label, type}` plus `stages` (status), `options` (choice), `currency` (money),
    `to` (draft: the key of the field naming who the draft is for), `format` (longtext and draft: plain, markdown or
    html), `readonly` (the person can't change it in the app: the bot or its script keeps it), and for a draft `approve`
    (its button is the person's approval of exactly this content, bound to a digest), `attachments` (a files field sent
    with it) and `covers` (other fields that are part of what is approved, like the subject)."""
    if not isinstance(raw, dict):
        raise FieldError("each field must be an object with key, label and type")

    key = str(raw.get("key") or "").strip().lower()

    if not KEY.match(key):
        raise FieldError(f"field key {key[:40]!r} must start with a letter and use lower-case letters, digits and _")

    kind = str(raw.get("type") or "text").strip().lower()

    if kind not in TYPES:
        raise FieldError(f"field {key}: type must be one of {', '.join(TYPES)}")

    field: dict = {"key": key, "label": _label(raw.get("label") or key.replace("_", " ").capitalize(), f"field {key}"), "type": kind}

    if raw.get("required"):
        field["required"] = True

    if raw.get("retired"):
        field["retired"] = True

    if raw.get("readonly"):
        field["readonly"] = True

    if kind in ("longtext", "draft") and raw.get("format") not in (None, "", "plain"):
        fmt = str(raw["format"]).strip().lower()

        if fmt not in FORMATS:
            raise FieldError(f"field {key}: format is one of {', '.join(FORMATS)}")

        field["format"] = fmt

    if kind == "status":
        stages = raw.get("stages")

        if not isinstance(stages, list) or not 2 <= len(stages) <= MAX_OPTIONS:
            raise FieldError(f"field {key}: a status needs 2 to {MAX_OPTIONS} stages, like [{{\"name\": \"New\"}}, {{\"name\": \"Done\", \"done\": true}}]")

        cleaned = []

        for stage in stages:
            stage = {"name": stage} if isinstance(stage, str) else stage

            if not isinstance(stage, dict):
                raise FieldError(f"field {key}: each stage is {{name, done}}")

            cleaned.append({"name": _clip(str(stage.get("name") or ""), LIMITS["option"]), "done": bool(stage.get("done"))})

        names = [stage["name"].lower() for stage in cleaned]

        if "" in names or len(set(names)) != len(names):
            raise FieldError(f"field {key}: stage names must be filled in and different")

        if not any(stage["done"] for stage in cleaned):
            raise FieldError(f"field {key}: mark at least one stage done (it closes the record)")

        if all(stage["done"] for stage in cleaned):
            raise FieldError(f"field {key}: at least one stage must be open")

        field["stages"] = cleaned

    if kind == "choice":
        options = raw.get("options")

        if not isinstance(options, list) or not 1 <= len(options) <= MAX_OPTIONS:
            raise FieldError(f"field {key}: a choice needs 1 to {MAX_OPTIONS} options")

        cleaned = [_clip(str(option), LIMITS["option"]) for option in options]

        if "" in cleaned or len({option.lower() for option in cleaned}) != len(cleaned):
            raise FieldError(f"field {key}: options must be filled in and different")

        field["options"] = cleaned

    if kind == "money":
        currency = str(raw.get("currency") or "USD").strip().upper()

        if not CURRENCY.match(currency):
            raise FieldError(f"field {key}: currency is a 3-letter code like USD")

        field["currency"] = currency

    if kind == "draft" and raw.get("to"):
        field["to"] = str(raw["to"]).strip().lower()

    if kind == "draft" and raw.get("approve"):
        field["approve"] = True

    if kind == "draft" and raw.get("attachments"):
        field["attachments"] = str(raw["attachments"]).strip().lower()

    if kind == "draft" and raw.get("covers"):
        covers = raw["covers"]

        if not isinstance(covers, list) or len(covers) > 12:
            raise FieldError(f"field {key}: covers is a list of up to 12 field keys")

        field["covers"] = [str(item).strip().lower() for item in covers]

    return field


def clean_fields(raw) -> list[dict]:
    if not isinstance(raw, list) or not raw:
        raise FieldError("a collection needs fields: [{key, label, type}, ...]")

    if len(raw) > MAX_FIELDS:
        raise FieldError(f"at most {MAX_FIELDS} fields")

    fields = [clean_field(field) for field in raw]
    keys = [field["key"] for field in fields]

    if len(set(keys)) != len(keys):
        raise FieldError("two fields share a key")

    if sum(1 for field in fields if field["type"] == "status") > 1:
        raise FieldError("a collection has at most one status field (it is what opens and closes a record)")

    by_key = {field["key"]: field for field in fields}

    for field in fields:
        if field.get("to") and (field["to"] == field["key"] or field["to"] not in keys):
            raise FieldError(f"field {field['key']}: to names another field of this collection (who the draft is for)")

        if field.get("attachments") and (by_key.get(field["attachments"]) or {}).get("type") != "files":
            raise FieldError(f"field {field['key']}: attachments names a files field of this collection")

        for other in field.get("covers") or []:
            if other == field["key"] or other not in by_key or by_key[other]["type"] in ("draft",):
                raise FieldError(f"field {field['key']}: covers names other fields of this collection (not drafts), like the subject")

    return fields


def status_field(fields: list[dict]) -> dict | None:
    return next((field for field in fields if field["type"] == "status" and not field.get("retired")), None)


def done_stages(field: dict) -> set[str]:
    return {stage["name"] for stage in field.get("stages", []) if stage["done"]}


def first_stage(field: dict, done: bool) -> str:
    return next(stage["name"] for stage in field["stages"] if stage["done"] == done)


# -- values ------------------------------------------------------------------------------------------------------


def _number(value, key: str) -> float | int:
    if isinstance(value, bool):
        raise FieldError(f"{key}: expected a number")

    if isinstance(value, (int, float)):
        number = value
    else:
        text = re.sub(r"[,\s$€£%]", "", str(value))

        try:
            number = float(text)
        except ValueError:
            raise FieldError(f"{key}: expected a number, got {str(value)[:30]!r}") from None

    if number != number or number in (float("inf"), float("-inf")):
        raise FieldError(f"{key}: expected a finite number")

    return int(number) if float(number).is_integer() and abs(number) < 2**53 else round(float(number), 6)


def parse_when(value, key: str) -> int:
    """Epoch seconds from a number (seconds or milliseconds) or an ISO 8601 time (local time when it has no offset)."""
    if isinstance(value, bool):
        raise FieldError(f"{key}: expected a time")

    if isinstance(value, (int, float)):
        return int(value / 1000 if value > 1e12 else value)

    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        raise FieldError(f"{key}: expected an ISO 8601 time like 2026-01-15T09:14:00+00:00, or epoch seconds") from None

    if parsed.tzinfo is None:
        parsed = parsed.astimezone()

    return int(parsed.timestamp())


def parse_date(value, key: str) -> str:
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(value).strip())

    if not match:
        raise FieldError(f"{key}: expected a date like 2026-10-31")

    try:
        date(int(match[1]), int(match[2]), int(match[3]))
    except ValueError:
        raise FieldError(f"{key}: {match[0]} is not a real date") from None

    return match[0]


def _pick(value, allowed: list[str], key: str) -> str:
    text = str(value).strip().lower()

    for option in allowed:
        if option.lower() == text:
            return option

    raise FieldError(f"{key}: must be one of {', '.join(allowed)}")


def clean_value(field: dict, value):
    """The value as it is stored, or None to clear it. Raises FieldError with what to change."""
    key = field["key"]

    if value is None or (isinstance(value, str) and not value.strip()):
        if field.get("required"):
            raise FieldError(f"{key} is required")

        return None

    kind = field["type"]

    if kind in STRUCTURED:
        return _clean_items(field, value)

    if kind in ("text", "longtext", "draft"):
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            raise FieldError(f"{key}: expected text")

        if field.get("format") == "html":
            # Stored exactly as given (less control characters), never clipped: cutting HTML breaks it, and an approval
            # covers the exact text.
            text = CONTROL.sub("", str(value))

            if len(text) > LIMITS["html"]:
                raise FieldError(f"{key}: HTML is limited to {LIMITS['html']} characters")

            return text

        return _clip(str(value), LIMITS[kind], single_line=kind == "text")

    if kind in ("number", "money", "percent"):
        return _number(value, key)

    if kind == "rating":
        number = _number(value, key)

        if not (isinstance(number, int) and 1 <= number <= 5):
            raise FieldError(f"{key}: a rating is a whole number from 1 to 5")

        return number

    if kind == "date":
        return parse_date(value, key)

    if kind == "datetime":
        return parse_when(value, key)

    if kind == "checkbox":
        if isinstance(value, bool):
            return value

        text = str(value).strip().lower()

        if text in ("true", "yes", "1", "on"):
            return True

        if text in ("false", "no", "0", "off"):
            return False

        raise FieldError(f"{key}: expected true or false")

    if kind == "status":
        return _pick(value, [stage["name"] for stage in field["stages"]], key)

    if kind == "choice":
        return _pick(value, field["options"], key)

    text = str(value).strip()

    if kind == "phone":
        if not PHONE.match(text):
            raise FieldError(f"{key}: expected a phone number")

        return text

    if kind == "email":
        if not EMAIL.match(text):
            raise FieldError(f"{key}: expected an email address")

        return text

    if kind == "url":
        if not text.startswith("https://") or len(text) > LIMITS["url"] or re.search(r"[\s<>]", text):
            raise FieldError(f"{key}: expected an https:// address")

        return text

    if kind == "link":
        if not LINK.match(text) or len(text) > LIMITS["link"]:
            raise FieldError(f"{key}: expected a path on this server (/home/…, /api/plugins/…) or an https:// page")

        return text

    raise FieldError(f"{key}: unknown type {kind}")  # pragma: no cover


def _clean_items(field: dict, value) -> list:
    """A list, entries or files value: a list of small items, each checked."""
    key, kind = field["key"], field["type"]

    if isinstance(value, str) and kind == "list":
        value = [part for part in re.split(r"\s*[|\n]\s*", value) if part.strip()]

    if not isinstance(value, list):
        raise FieldError(f"{key}: expected a list")

    if len(value) > MAX_ITEMS[kind]:
        raise FieldError(f"{key}: at most {MAX_ITEMS[kind]} items")

    items = []

    for item in value:
        if kind == "list":
            if not isinstance(item, (str, int, float)) or isinstance(item, bool) or not str(item).strip():
                raise FieldError(f"{key}: each item is a short text")

            items.append(_clip(str(item), LIMITS["item"]))
            continue

        if not isinstance(item, dict):
            raise FieldError(f"{key}: each item is an object" + (" {label, detail?, at?, tone?}" if kind == "entries" else " {name, path or url, mime?, size?, sha256?}"))

        if kind == "entries":
            label = item.get("label")

            if not isinstance(label, (str, int, float)) or not str(label).strip():
                raise FieldError(f"{key}: each entry needs a label")

            entry: dict = {"label": _clip(str(label), LIMITS["entry"])}

            if item.get("detail") not in (None, ""):
                entry["detail"] = _clip(str(item["detail"]), LIMITS["detail"])

            if item.get("at") not in (None, ""):
                entry["at"] = parse_when(item["at"], f"{key}.at")

            if item.get("tone") not in (None, ""):
                if item["tone"] not in TONES:
                    raise FieldError(f"{key}: an entry's tone is good, warn or bad")

                entry["tone"] = item["tone"]

            items.append(entry)
            continue

        name = item.get("name")

        if not isinstance(name, str) or not name.strip():
            raise FieldError(f"{key}: each file needs a name")

        entry = {"name": _clip(name, LIMITS["name"])}
        path, url = str(item.get("path") or "").strip(), str(item.get("url") or "").strip()

        if path:
            if not LINK.match(path) or len(path) > LIMITS["link"] or path.startswith("https://"):
                raise FieldError(f"{key}: a file's path is on this server (/home/…, /api/plugins/…)")

            entry["path"] = path
        elif url:
            if not url.startswith("https://") or len(url) > LIMITS["url"] or re.search(r"[\s<>]", url):
                raise FieldError(f"{key}: a file's url is an https:// address")

            entry["url"] = url
        else:
            raise FieldError(f"{key}: each file needs a path on this server or an https:// url")

        if item.get("mime"):
            entry["mime"] = _clip(str(item["mime"]), LIMITS["mime"])

        if item.get("size") not in (None, ""):
            size = _number(item["size"], f"{key}.size")

            if not isinstance(size, int) or size < 0:
                raise FieldError(f"{key}: a file's size is a whole number of bytes")

            entry["size"] = size

        if item.get("sha256"):
            digest = str(item["sha256"]).strip().lower()

            if not SHA256.match(digest):
                raise FieldError(f"{key}: a file's sha256 is 64 hex characters")

            entry["sha256"] = digest

        items.append(entry)

    return items or None


def clean_values(fields: list[dict], raw, *, partial: bool) -> dict:
    """Values keyed by field, checked. `partial`: only the keys given (an update); otherwise required fields must be there."""
    if not isinstance(raw, dict):
        raise FieldError("values must be an object of field key → value")

    by_key = {field["key"]: field for field in fields}
    unknown = [key for key in raw if key not in by_key]

    if unknown:
        raise FieldError(f"no field {unknown[0]!r} in this collection (fields: {', '.join(by_key)})")

    retired = [key for key in raw if by_key[key].get("retired")]

    if retired:
        raise FieldError(f"field {retired[0]!r} is retired")

    values = {key: clean_value(by_key[key], value) for key, value in raw.items()}

    if not partial:
        missing = [field["key"] for field in fields if field.get("required") and not field.get("retired") and values.get(field["key"]) is None]

        if missing:
            raise FieldError(f"{missing[0]} is required")

    return values
