"""A chat's history for the phone, light enough to open over a phone line.

Hermes's own `/api/sessions/{id}/messages` hands back whole stored rows. In a busy bot's chat most of that is never drawn: the
prompt as the model was sent it (`api_content`), the provider's encrypted reasoning, and every tool's full output. Measured on a
real server, the latest 200 rows of a chat came to 1.2 MB, of which about 50 KB was words a person reads.

The app shows a run of tool steps as one line (the last line of the last output) and opens the full output only when tapped.
So a row goes to the phone with:

- the reasoning and the model's copy of the prompt dropped (the app never draws stored reasoning; thinking shows live);
- a tool's result cut down: every small field kept (exit code, error, status, the picture or file it made), and a long text
  kept by its end, since the line the app shows is the output's last one; an edit's result is kept whole, as Hermes's own
  compact history does, because its diff is drawn;
- a tool call's long arguments cut to their start (the app names a step by its command, path or address);
- `omitted` on a row that was cut (how many characters it had), so the app can fetch it whole (`full_rows`) when the step
  comes near the screen or is tapped.

And a page is counted in the person's turns, not stored rows: a turn with forty tool steps is still one turn.
Pure: no Hermes imports; the route in `dashboard/plugin_api.py` reads the rows.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

#: Never drawn by the app: the prompt as the model saw it, reasoning text and the providers' encrypted reasoning.
DROP = frozenset({"api_content", "reasoning", "reasoning_content", "reasoning_details", "codex_reasoning_items"})

#: Their result is the edit itself (a diff, the file written): kept whole, as in Hermes's compact history.
EDIT_TOOLS = frozenset({"write_file", "patch", "skill_manage"})

#: A text in a tool's result longer than this is cut to its last `TAIL` characters.
LONG = 1500
TAIL = 600

#: A tool call's argument longer than this is cut to its first `ARG_HEAD` characters.
ARG_LONG = 600
ARG_HEAD = 300

#: A nested value (a list of search results, say) whose JSON is longer than this is replaced by the end of that JSON.
NESTED_LONG = 400

ELLIPSIS = "…"


def _tail(text: str) -> str:
    return text if len(text) <= LONG else ELLIPSIS + text[-TAIL:]


def _head(text: str) -> str:
    return text if len(text) <= ARG_LONG else text[:ARG_HEAD] + ELLIPSIS


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content

    return json.dumps(content, ensure_ascii=False)


def slim_result(content: Any, tool: str | None) -> tuple[Any, int | None]:
    """A tool result cut down for the phone, and how long it was when it was cut (None when it went whole)."""
    if content is None or (tool or "") in EDIT_TOOLS:
        return content, None

    text = _text_of(content)

    if len(text) <= LONG:
        return content, None

    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        parsed = None

    if not isinstance(parsed, dict):
        return _tail(text), len(text)

    slim: dict[str, Any] = {}

    for key, value in parsed.items():
        if isinstance(value, str):
            slim[key] = _tail(value)
        elif isinstance(value, (list, dict)):
            nested = json.dumps(value, ensure_ascii=False)
            slim[key] = value if len(nested) <= NESTED_LONG else ELLIPSIS + nested[-NESTED_LONG:]
        else:
            slim[key] = value

    return json.dumps(slim, ensure_ascii=False), len(text)


def slim_arguments(arguments: Any) -> tuple[Any, bool]:
    """A tool call's arguments with each long text cut to its start, and whether anything was cut."""
    if not isinstance(arguments, str) or len(arguments) <= ARG_LONG:
        return arguments, False

    try:
        parsed = json.loads(arguments)
    except (TypeError, ValueError):
        return _head(arguments), True

    if not isinstance(parsed, dict):
        return _head(arguments), True

    cut = {key: _head(value) if isinstance(value, str) else value for key, value in parsed.items()}
    out = json.dumps(cut, ensure_ascii=False)

    return (out, True) if len(out) <= ARG_LONG * 4 else (_head(arguments), True)


def slim_row(row: dict[str, Any], tool_names: dict[str, str]) -> dict[str, Any]:
    """One stored row as the phone gets it. `tool_names` maps a call id to its tool, from the assistant rows read so far."""
    out = {key: value for key, value in row.items() if key not in DROP}
    omitted = 0

    if out.get("role") == "assistant" and isinstance(out.get("tool_calls"), list):
        calls = []

        for call in out["tool_calls"]:
            if not isinstance(call, dict):
                calls.append(call)
                continue

            function = call.get("function") if isinstance(call.get("function"), dict) else None

            if function is not None and isinstance(call.get("id"), str) and isinstance(function.get("name"), str):
                tool_names[call["id"]] = function["name"]

            if function is None:
                calls.append(call)
                continue

            arguments, cut = slim_arguments(function.get("arguments"))

            if cut:
                omitted += len(function.get("arguments") or "")

            calls.append({**call, "function": {**function, "arguments": arguments}})

        out["tool_calls"] = calls

    if out.get("role") == "tool":
        tool = out.get("tool_name") or tool_names.get(str(out.get("tool_call_id") or ""))
        content, size = slim_result(out.get("content"), tool)

        if size is not None:
            out["content"] = content
            omitted += size

    if omitted:
        out["omitted"] = omitted

    return out


def slim_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows in order (oldest first), each cut down for the phone."""
    tool_names: dict[str, str] = {}

    return [slim_row(row, tool_names) for row in rows if isinstance(row, dict)]


def full_row(row: dict[str, Any]) -> dict[str, Any]:
    """A row asked for whole: everything the app might draw, only what it never draws dropped."""
    return {key: value for key, value in row.items() if key not in DROP}


def is_turn(row: dict[str, Any]) -> bool:
    """Whether a row starts one of the person's turns: something from the person the app shows."""
    if row.get("role") != "user" or row.get("display_kind") == "hidden":
        return False

    text = row.get("display_content") if isinstance(row.get("display_content"), str) else row.get("content")

    return isinstance(text, str) and text.strip() != ""


def turns_in(rows: list[dict[str, Any]]) -> int:
    return sum(1 for row in rows if is_turn(row))


def cut_to_turns(rows: list[dict[str, Any]], turns: int) -> list[dict[str, Any]]:
    """The newest `turns` of the person's turns from `rows` (oldest first): everything from the start of the oldest of them.
    Fewer turns than that, and all of `rows`."""
    if turns <= 0:
        return []

    seen = 0

    for index in range(len(rows) - 1, -1, -1):
        if is_turn(rows[index]):
            seen += 1

            if seen == turns:
                return rows[index:]

    return rows
