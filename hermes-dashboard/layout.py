"""How a collection's record screen is laid out: building blocks the bot picks, drawn by the app in its own design.

A bot changes the structure of a record screen, never its style: `{blocks: [...], tray: {...}}`, each block one of the kinds
below, naming fields of the collection. A collection without a layout keeps the app's standard record screen.

- `header` `{meta?, byline?}`: the bot's face and name (with "asked by" a byline field naming another bot), the title, and
  a meta line (a field, often the status). First, once.
- `ask` `{field, choices?, answer?, when?}`: a question that rises while the record needs the person (or while `when`
  matches), with one-tap choices from a list field; the answer is a note (the default, which answers a kanban card) or
  is written to the field `answer` names. Once.
- `text` `{field, label?, format?, fold?}`: a text field in plain words, Markdown or HTML, folded to `fold` lines.
- `fields` `{fields, label?}`: a group of label and value rows.
- `files` `{field, label?}`: file cards from a files or link field.
- `draft` `{field}`: a draft, in its format, with its attachments and, for one the person approves, the digest line.
- `entries` `{field, label?}`: a short list of dated lines, like a task's attempts.
- `notes` `{label?, history?}`: the notes thread (`history: "all"` adds every field change). Once.

`tray` `{composer?, primary?, secondary?, placeholder?}`: the action tray at the bottom. `composer` makes it a text box that
adds a note (and answers the `ask`); `primary` is a button id or `next_stage` (the white pill); `secondary` up to three of
`ask` (a side chat with the bot), `done`, or button ids.
"""

from __future__ import annotations

try:
    from . import fields as F
    from . import query as Q
except ImportError:  # the tests import the modules flat
    import fields as F  # type: ignore[no-redef]
    import query as Q  # type: ignore[no-redef]

BLOCKS = ("header", "ask", "text", "fields", "files", "draft", "entries", "notes")
ONCE = ("header", "ask", "notes")
MAX_BLOCKS = 12
MAX_GROUP = 20


class LayoutError(ValueError):
    """A layout that can't be used; the message says what to change."""


def _label(raw, what: str) -> str | None:
    if raw in (None, ""):
        return None

    return F.clean_value({"key": what, "type": "text"}, raw)[:40] or None


def _field(raw, by_key: dict, what: str, types: tuple) -> str:
    key = str(raw or "").strip().lower()
    field = by_key.get(key)

    if field is None or field.get("retired"):
        raise LayoutError(f"{what}: no field {key!r} in this collection")

    if field["type"] not in types:
        raise LayoutError(f"{what}: {key} is a {field['type']}, it needs a {' or '.join(types)} field")

    return key


TEXTUAL = ("text", "longtext")
SHOWN = tuple(kind for kind in F.TYPES if kind not in F.STRUCTURED + ("draft",))


def clean_block(raw, fields: list[dict]) -> dict:
    if not isinstance(raw, dict):
        raise LayoutError("each block is an object: {block: header|ask|text|fields|files|draft|entries|notes, ...}")

    kind = str(raw.get("block") or "").strip().lower()

    if kind not in BLOCKS:
        raise LayoutError(f"block must be one of {', '.join(BLOCKS)}")

    by_key = {field["key"]: field for field in fields}
    block: dict = {"block": kind}

    if kind == "header":
        if raw.get("meta"):
            block["meta"] = _field(raw["meta"], by_key, "header.meta", SHOWN)

        if raw.get("byline"):
            block["byline"] = _field(raw["byline"], by_key, "header.byline", ("text",))

    elif kind == "ask":
        block["field"] = _field(raw.get("field"), by_key, "ask.field", TEXTUAL)

        if raw.get("choices"):
            block["choices"] = _field(raw["choices"], by_key, "ask.choices", ("list",))

        answer = str(raw.get("answer") or "note").strip().lower()
        block["answer"] = "note" if answer == "note" else _field(answer, by_key, "ask.answer", TEXTUAL + ("choice",))

        if raw.get("when"):
            try:
                block["when"] = Q.clean_filter(raw["when"], fields)
            except (Q.QueryError, F.FieldError) as error:
                raise LayoutError(f"ask.when: {error}") from None

    elif kind == "text":
        block["field"] = _field(raw.get("field"), by_key, "text.field", TEXTUAL)

        if raw.get("format") not in (None, ""):
            fmt = str(raw["format"]).strip().lower()

            if fmt not in F.FORMATS:
                raise LayoutError(f"text.format is one of {', '.join(F.FORMATS)}")

            block["format"] = fmt

        if raw.get("fold") not in (None, "", 0):
            try:
                fold = int(raw["fold"])
            except (TypeError, ValueError):
                raise LayoutError("text.fold is a number of lines, 1 to 20") from None

            if not 1 <= fold <= 20:
                raise LayoutError("text.fold is a number of lines, 1 to 20")

            block["fold"] = fold

    elif kind == "fields":
        keys = raw.get("fields")

        if not isinstance(keys, list) or not 1 <= len(keys) <= MAX_GROUP:
            raise LayoutError(f"fields.fields is a list of 1 to {MAX_GROUP} field keys")

        block["fields"] = [_field(key, by_key, "fields", SHOWN) for key in keys]

    elif kind == "files":
        block["field"] = _field(raw.get("field"), by_key, "files.field", ("files", "link", "url"))

    elif kind == "draft":
        block["field"] = _field(raw.get("field"), by_key, "draft.field", ("draft",))

    elif kind == "entries":
        block["field"] = _field(raw.get("field"), by_key, "entries.field", ("entries",))

    elif kind == "notes":
        history = str(raw.get("history") or "notes").strip().lower()

        if history not in ("notes", "all"):
            raise LayoutError("notes.history is notes (the thread) or all (every change too)")

        block["history"] = history

    label = _label(raw.get("label"), f"{kind}.label")

    if label and kind not in ("header", "draft"):
        block["label"] = label

    return block


def clean_tray(raw, actions: list[dict]) -> dict | None:
    if raw in (None, "", {}):
        return None

    if not isinstance(raw, dict):
        raise LayoutError("tray is {composer?, primary?, secondary?, placeholder?}")

    ids = {action["id"] for action in actions}
    tray: dict = {}

    if raw.get("composer"):
        tray["composer"] = True

    if raw.get("primary"):
        primary = str(raw["primary"]).strip().lower()

        if primary != "next_stage" and primary not in ids:
            raise LayoutError(f"tray.primary is a button id ({', '.join(sorted(ids)) or 'this collection has none'}) or next_stage")

        tray["primary"] = primary

    secondary = raw.get("secondary") or []

    if not isinstance(secondary, list) or len(secondary) > 3:
        raise LayoutError("tray.secondary is a list of up to 3: ask, done, or button ids")

    cleaned = []

    for item in secondary:
        name = str(item or "").strip().lower()

        if name not in ("ask", "done") and name not in ids:
            raise LayoutError(f"tray.secondary: {name!r} is not ask, done or one of this collection's buttons")

        if name not in cleaned:
            cleaned.append(name)

    if cleaned:
        tray["secondary"] = cleaned

    if raw.get("placeholder"):
        tray["placeholder"] = F.clean_value({"key": "tray.placeholder", "type": "text"}, raw["placeholder"])[:60]

    if tray.get("composer") and tray.get("primary"):
        raise LayoutError("a tray is either a composer or a white pill, not both")

    return tray or None


def clean_layout(raw, fields: list[dict], actions: list[dict]) -> dict | None:
    """A collection's layout, checked against its fields and buttons, or None for the app's standard record screen."""
    if raw in (None, "", {}, False):
        return None

    if not isinstance(raw, dict) or not isinstance(raw.get("blocks"), list):
        raise LayoutError("layout is {blocks: [{block, ...}, ...], tray?: {...}}")

    if not 1 <= len(raw["blocks"]) <= MAX_BLOCKS:
        raise LayoutError(f"a layout has 1 to {MAX_BLOCKS} blocks")

    blocks = [clean_block(item, fields) for item in raw["blocks"]]

    for kind in ONCE:
        if sum(1 for block in blocks if block["block"] == kind) > 1:
            raise LayoutError(f"a layout has at most one {kind} block")

    if any(block["block"] == "header" for block in blocks[1:]):
        raise LayoutError("the header block comes first")

    layout: dict = {"blocks": blocks}
    tray = clean_tray(raw.get("tray"), actions)

    if tray:
        layout["tray"] = tray

    return layout


def still_valid(layout: dict | None, fields: list[dict], actions: list[dict]) -> dict | None:
    """A stored layout after its collection's fields or buttons changed: blocks that name a field that is gone are left
    out, and so are tray buttons that are gone (a bot's layout never breaks its record screen)."""
    if not layout:
        return None

    kept = []

    for block in layout.get("blocks") or []:
        try:
            kept.append(clean_block(block, fields))
        except (LayoutError, F.FieldError):
            continue

    if not kept:
        return None

    result: dict = {"blocks": kept}
    tray = dict(layout.get("tray") or {})
    ids = {action["id"] for action in actions}

    if tray.get("primary") not in (None, "next_stage") and tray["primary"] not in ids:
        tray.pop("primary")

    if tray.get("secondary"):
        tray["secondary"] = [name for name in tray["secondary"] if name in ("ask", "done") or name in ids]

    tray = {key: value for key, value in tray.items() if value}

    if tray:
        result["tray"] = tray

    return result
