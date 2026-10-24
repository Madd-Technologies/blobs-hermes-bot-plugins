"""The four tools a bot keeps its person's dashboards with: collections, records, queries and views.

Each call acts as the bot whose turn it is (`profile_name_for_home(get_hermes_home())`). What a bot sends is checked in
`fields.py`, `query.py` and `store.py`; a mistake comes back as a tool error saying what to change.
"""

from __future__ import annotations

import json
import logging

try:
    from . import store as S
except ImportError:  # the tests import the modules flat
    import store as S  # type: ignore[no-redef]

log = logging.getLogger("hermes-dashboard")

TOOLSET = "dashboard"


def current_profile() -> str:
    from hermes_constants import get_hermes_home, profile_name_for_home

    return profile_name_for_home(get_hermes_home()) or "default"


def _result(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def _error(message: str) -> str:
    try:
        from tools.registry import tool_error

        return tool_error(message)
    except Exception:  # outside Hermes (tests)
        return json.dumps({"error": message}, ensure_ascii=False)


def _field_list(collection: dict) -> str:
    return ", ".join(f"{field['key']} ({field['type']})" for field in collection["fields"] if not field.get("retired"))


def _button_list(collection: dict) -> str:
    buttons = collection.get("actions") or []

    return f"; buttons {', '.join(action['label'] for action in buttons)}" if buttons else ""


FIELD_SCHEMA = {
    "type": "object",
    "description": (
        "A field: {key, label, type, required?, readonly?}. status needs stages [{name, done}] (at least one open, one done); "
        "choice needs options; money may set currency; longtext and draft may set format: plain, markdown or html; draft (text "
        "you wrote for your person to send or use, shown in full and editable) may set to (the field naming who it is for), "
        "approve: true (its button is your person's approval of exactly this content: an email to send), attachments (a files "
        "field sent with it) and covers (other fields that are part of what is approved, like subject). list is short texts "
        "(choices), entries is dated lines [{label, detail?, at?, tone?: good|warn|bad}], files is [{name, path or url, mime?, "
        "size?, sha256?}]. readonly: your person can't change it in the app."
    ),
    "properties": {
        "key": {"type": "string"},
        "label": {"type": "string"},
        "type": {"type": "string", "enum": ["text", "longtext", "number", "money", "percent", "date", "datetime", "checkbox", "status", "choice", "phone", "email", "url", "link", "rating", "draft", "list", "entries", "files"]},
        "required": {"type": "boolean"},
        "readonly": {"type": "boolean"},
        "stages": {"type": "array", "items": {"type": "object", "properties": {"name": {"type": "string"}, "done": {"type": "boolean"}}}},
        "options": {"type": "array", "items": {"type": "string"}},
        "currency": {"type": "string"},
        "to": {"type": "string"},
        "format": {"type": "string", "enum": ["plain", "markdown", "html"]},
        "approve": {"type": "boolean"},
        "attachments": {"type": "string"},
        "covers": {"type": "array", "items": {"type": "string"}},
    },
}

LAYOUT_SCHEMA = {
    "type": "object",
    "description": (
        "create/update: how each record's screen is laid out, from blocks the app draws in its own design (null: the standard "
        "screen). {blocks: [...], tray?}. Blocks: {block: header, meta?, byline?} first; {block: ask, field (the question), "
        "choices? (a list field), answer?: note (default) or a field, when?: filter (default: while it needs your person)}; "
        "{block: text, field, label?, format?, fold?: lines}; {block: fields, fields: [keys], label?}; {block: files, field}; "
        "{block: draft, field}; {block: entries, field, label?}; {block: notes, label?, history?: notes|all}. tray: "
        "{composer: true (a box that adds a note and answers the ask) | primary: a button id or next_stage, secondary?: up to 3 "
        "of ask, done, button ids, placeholder?}."
    ),
}

ACTION_SCHEMA = {
    "type": "object",
    "description": (
        "A button on each record: {id, label (24 characters, a verb: Send reply), prompt (what you will be asked to do when "
        "your person presses it), when? (a filter: shown only while it matches), draft? (the draft field it uses)}. Pressing "
        "it opens a side chat with you that sends the prompt with the record; do the job, then update the record."
    ),
    "properties": {
        "id": {"type": "string"},
        "label": {"type": "string"},
        "prompt": {"type": "string"},
        "when": {"type": "object"},
        "draft": {"type": "string"},
    },
}

FILTER_DESCRIPTION = (
    "A filter: {field, op, value} or {all: [...]}, {any: [...]}, {not: ...}. ops: eq, ne, in, nin, lt, lte, gt, gte, empty, "
    "not_empty, contains, within_days (next N days), in_last_days, older_than_days, is_open, is_done (no field). Fields are "
    "the collection's keys or _created, _updated, _closed."
)

COLLECTION_SCHEMA = {
    "name": "dashboard_collection",
    "description": "Design the collections you track things in for your person (their HermesBot app shows them): create one with its own typed fields, change or retire fields, share it with another bot, list or describe them. Load skill_view(\"hermes-dashboard:dashboard\") first.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["create", "update", "retire", "share", "unshare", "list", "describe"]},
            "collection": {"type": "string", "description": "Its id (yours, or one shared with you), or owner/id."},
            "id": {"type": "string", "description": "create: lower-case id, e.g. support-emails. Omit and the title is used."},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "fields": {"type": "array", "items": FIELD_SCHEMA, "description": "create: the fields."},
            "add_fields": {"type": "array", "items": FIELD_SCHEMA},
            "change_fields": {"type": "array", "items": {"type": "object"}, "description": "{key, label?, stages?, options?, required?, format?, readonly?, approve?, attachments?, covers?}; a type never changes."},
            "retire_fields": {"type": "array", "items": {"type": "string"}},
            "actions": {"type": "array", "items": ACTION_SCHEMA, "description": "create/update: the buttons (up to 4); update replaces the whole list."},
            "feed": {"type": "object", "description": "create/update: how this collection is kept fresh, shown to your person: {how: script (a no_agent cron job using write.py) | model_on_change (an agent job gated by a monitor_script) | model_timer (last resort), job: the cron job id, every: e.g. 5m}."},
            "layout": LAYOUT_SCHEMA,
            "profile": {"type": "string", "description": "share/unshare: the other bot's profile name."},
            "can_write": {"type": "boolean", "description": "share: whether that bot may add and change records."},
        },
        "required": ["action"],
    },
}

RECORD_SCHEMA = {
    "name": "dashboard_record",
    "description": "Add or change records in a collection. upsert adds records or updates the one with the same source_key (use the source's own id, e.g. the email's message id, so nothing is added twice). Fields your person changed are kept unless override_person. close/reopen move the status to its first done/open stage. note adds a note to the record's history. report says what happened to a draft your person approved: outcome sent (digest: what went out) or refused (detail: why), so they see whether it matched.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["upsert", "update", "close", "reopen", "note", "get", "report"]},
            "collection": {"type": "string", "description": "upsert: the collection."},
            "records": {"type": "array", "items": {"type": "object", "properties": {"source_key": {"type": "string"}, "values": {"type": "object"}}}, "description": "upsert: up to 100 records, each {source_key, values}."},
            "record_id": {"type": "string", "description": "update/close/reopen/note/get: the record."},
            "values": {"type": "object", "description": "update: field key → value."},
            "note": {"type": "string"},
            "override_person": {"type": "boolean", "description": "Overwrite fields your person changed. Only when they asked you to."},
            "draft": {"type": "string", "description": "report: the draft field."},
            "digest": {"type": "string", "description": "report: the approval digest of what went out."},
            "outcome": {"type": "string", "enum": ["sent", "refused"], "description": "report"},
            "detail": {"type": "string", "description": "report: why it was refused, or where it went."},
        },
        "required": ["action"],
    },
}

QUERY_SCHEMA = {
    "name": "dashboard_query",
    "description": "Read a collection: records by filter, sorted, with a count or sum and grouping, and optionally each record's history (your person's edits and notes are in it). Use it to answer what is open or waiting, and before updating records.",
    "parameters": {
        "type": "object",
        "properties": {
            "collection": {"type": "string"},
            "filter": {"type": "object", "description": FILTER_DESCRIPTION},
            "sort": {"type": "array", "items": {"type": "object", "properties": {"field": {"type": "string"}, "dir": {"type": "string", "enum": ["asc", "desc"]}}}},
            "limit": {"type": "integer"},
            "aggregate": {"type": "object", "description": "{op: count|sum|avg|min|max, field}"},
            "group_by": {"type": "string"},
            "include_history": {"type": "boolean"},
        },
        "required": ["collection"],
    },
}

VIEW_SCHEMA = {
    "name": "dashboard_view",
    "description": (
        "Build the panels on your person's dashboard: set (create or replace by id), remove, list, reorder. Kinds and config: "
        "list/checklist {show: {title, subtitle?, meta?}, filter?, sort?, needs_you?, include_closed?, limit?}; board {group_by: "
        "a status or choice field, show, filter?, needs_you?, closed_within_days?}; table {columns: [keys], filter?, sort?}; "
        "number {aggregate: {op, field?}, filter?, period?: {field, unit: day|week|month, compare}}; chart {aggregate, field: "
        "a date field, bucket: day|week|month, count, style: bar|line, filter?}; summary {text}; stat {metrics: [{label, "
        "value, change}]}. needs_you is a filter: matching open records light the amber dot. signals (list, checklist, board, "
        "table) gives a row a small dot, green (good), grey (warn) or red (bad): [{when: <filter>, tone}], first match wins, "
        "e.g. good once an email is sent, bad when it is still unsent and the date is within_days 1, and no rule for the "
        "rest; use them sparingly, amber is only for needs_you; done and muted rows carry none. A phone shows a table on the "
        "dashboard as rows (first column the title, the first amount on the right, the rest one short line), so put the name "
        "first and keep it to a few short columns. Design the page with the skill: what needs them first, three to five "
        "panels, figures as numbers. OPENS: a number or chart may set opens: true or {kind: list|table|board, title?, show?, "
        "sort?, columns?, group_by? (a date, status or choice field)}, and tapping it opens a page of the records it counts (a "
        "chart: the bar's records); a stat with a collection may give a metric opens: {filter}. A page holds only records: "
        "dashboard, page, record. show.link names a url field a row opens. LIVE PANELS: instead of collection, give source {kind: sqlite (path in your Hermes folder, query: one "
        "SELECT) | json (url, rows: path to the list) | ics (url or path, days) | script (a file in your scripts/ folder "
        "that prints JSON), ttl: seconds (default 60), fields: {key: type} to override inferred types}; list, table, "
        "number and chart panels then read it when your person looks, with no routine and no model. Prefer that for data "
        "that already exists. " + FILTER_DESCRIPTION
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["set", "remove", "list", "reorder"]},
            "id": {"type": "string"},
            "title": {"type": "string"},
            "kind": {"type": "string", "enum": ["list", "checklist", "board", "table", "number", "chart", "summary", "stat"]},
            "collection": {"type": "string"},
            "source": {"type": "object", "description": "A live source instead of a collection (see above)."},
            "config": {"type": "object"},
            "position": {"type": "integer"},
            "ids": {"type": "array", "items": {"type": "string"}, "description": "reorder: view ids in the order to show them."},
        },
        "required": ["action"],
    },
}


class DashboardTools:
    """The four tools. The acting bot is always the profile whose turn is running (`current_profile()`); there is no way
    to name another one, so a script can't write as a different bot (scripts use write.py, which acts as the profile its
    job runs as)."""

    def __init__(self, store_factory=None):
        self._store_factory = store_factory or open_store

    def _call(self, handler):
        try:
            return _result(handler(self._store_factory(), S.Actor.bot(current_profile())))
        except (S.DashboardError, S.NotFound) as error:
            return _error(str(error))
        except Exception as error:
            log.exception("hermes-dashboard tool failed")
            return _error(f"The dashboard store failed: {type(error).__name__}")

    # -- collections --

    def collection(self, args: dict, **_kw) -> str:
        args = args or {}
        action = str(args.get("action") or "")

        def run(store, actor):
            ref = args.get("collection")

            if action == "create":
                collection = store.create_collection(actor, args)
                return {"collection": collection, "message": f"Created {collection['key']} with fields {_field_list(collection)}{_button_list(collection)}. Add records with dashboard_record and show them with dashboard_view."}

            if action == "update":
                collection = store.update_collection(actor, ref, args)
                return {"collection": collection, "message": f"Updated {collection['key']}: fields {_field_list(collection)}{_button_list(collection)}."}

            if action == "retire":
                collection = store.retire_collection(actor, ref)
                return {"message": f"Retired {collection['key']}; its views are gone and its records kept."}

            if action == "share":
                shared = store.share(actor, ref, str(args.get("profile") or ""), bool(args.get("can_write")))
                return {"shared": shared, "message": f"Shared {shared['collection']} with {shared['profile']} ({'read and write' if shared['can_write'] else 'read only'})."}

            if action == "unshare":
                removed = store.unshare(actor, ref, str(args.get("profile") or ""))
                return {"removed": removed, "message": "Unshared." if removed else "It was not shared with that bot."}

            if action == "list":
                collections = store.list_collections(actor)
                return {"collections": collections, "message": f"{len(collections)} collections." if collections else "No collections yet."}

            if action == "describe":
                return {"collection": store.describe(actor, ref)}

            raise S.DashboardError("action must be create, update, retire, share, unshare, list or describe")

        return self._call(run)

    # -- records --

    def record(self, args: dict, **_kw) -> str:
        args = args or {}
        action = str(args.get("action") or "")

        def run(store, actor):
            if action == "upsert":
                results = store.upsert(actor, args.get("collection"), args.get("records") or [], override_person=bool(args.get("override_person")))
                created = sum(1 for result in results if result["created"])
                kept = sorted({key for result in results for key in result["kept"]})
                changed = sum(1 for result in results if result["changed"] and not result["created"])
                message = f"{created} added, {changed} updated, {len(results) - created - changed} unchanged."

                if results and not created and not changed:
                    message += " Nothing changed: a run that finds nothing new should write nothing. If this keeps a panel fresh on a timer, a script feed (write.py, no model) or a computed or live panel does it without a model run; see the skill's 'Keeping a panel live'."

                if kept:
                    message += f" Kept your person's changes to {', '.join(kept)} (read them with dashboard_query include_history)."

                return {"results": [{"id": result["record"]["id"], "source_key": result["record"]["source_key"], "created": result["created"], "changed": result["changed"], "kept": result["kept"]} for result in results], "message": message}

            record_id = str(args.get("record_id") or "")

            if action == "update":
                result = store.update(actor, record_id, args.get("values") or {}, override_person=bool(args.get("override_person")))
                return {**result, "message": f"Changed {', '.join(result['changed']) or 'nothing'}." + (f" Kept your person's {', '.join(result['kept'])}." if result["kept"] else "")}

            if action in ("close", "reopen"):
                result = store.set_closed(actor, record_id, action == "close")
                return {**result, "message": ("Closed." if action == "close" else "Reopened.") if result["changed"] else "It already was."}

            if action == "note":
                return store.add_note(actor, record_id, args.get("note"))

            if action == "get":
                return store.get_record(actor, record_id)

            if action == "report":
                result = store.report(actor, record_id, args.get("draft"), args.get("digest"), str(args.get("outcome") or ""), args.get("detail"))
                return {**result, "message": ("Reported. It matches what your person approved." if result["matched"] else "Reported. It does NOT match what your person approved; they will see that.") if result["outcome"] == "sent" else "Reported: nothing was sent."}

            raise S.DashboardError("action must be upsert, update, close, reopen, note, get or report")

        return self._call(run)

    # -- queries --

    def query(self, args: dict, **_kw) -> str:
        args = args or {}

        return self._call(lambda store, actor: store.query(actor, args.get("collection"), args))

    # -- views --

    def view(self, args: dict, **_kw) -> str:
        args = args or {}
        action = str(args.get("action") or "")

        def run(store, actor):
            if action == "set":
                view = store.set_view(actor, args)
                message = f"Saved the {view['kind']} panel '{view['title']}'. Your person sees it on your dashboard in the HermesBot app."

                if view.get("preview") is not None:
                    message += f" It reads its source live (every {view['config']['source']['ttl']} s at most, only while your person looks); no routine needed. First rows: {json.dumps(view['preview'], ensure_ascii=False, default=str)[:600]}"

                if view["kind"] in ("summary", "stat") and view.get("rewrites_last_hour", 0) > S.REWRITES_PER_HOUR:
                    message += f" You rewrote this panel {view['rewrites_last_hour']} times in the last hour. If it shows figures, compute them from a collection (number or chart panels) or bind a panel to a live source instead of rewriting it."

                return {"view": view, "message": message}

            if action == "remove":
                removed = store.remove_view(actor, args.get("id"))
                return {"removed": removed, "message": "Removed." if removed else "There was no such panel."}

            if action == "reorder":
                return {"views": store.reorder_views(actor, args.get("ids") or [])}

            if action == "list":
                return {"views": store.list_views(actor.name)}

            raise S.DashboardError("action must be set, remove, list or reorder")

        return self._call(run)

    def registrations(self) -> list[tuple[str, dict, object]]:
        return [
            ("dashboard_collection", COLLECTION_SCHEMA, self.collection),
            ("dashboard_record", RECORD_SCHEMA, self.record),
            ("dashboard_query", QUERY_SCHEMA, self.query),
            ("dashboard_view", VIEW_SCHEMA, self.view),
        ]


_shared: dict = {}


def open_store() -> "S.Store":
    """The process's store, with 0.1's panels moved in the first time it is opened."""
    if "store" not in _shared:
        try:
            from . import migrate
        except ImportError:  # the tests import the modules flat
            import migrate  # type: ignore[no-redef]

        store = S.Store()

        try:
            migrate.migrate_folder(store)
        except Exception:
            log.exception("hermes-dashboard: moving 0.1 panels failed; they stay in their JSON files")

        _shared["store"] = store

    return _shared["store"]
