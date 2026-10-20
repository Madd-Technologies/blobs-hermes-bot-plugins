"""The call brief: everything the voice on a call will know, checked in code before the person is asked to approve it.

A bot researches freely with its own tools, then distils what a call needs into a brief: who to call, the goal, the facts
the other party may hear, the questions to ask and what to record. The voice on the call gets only that (rendered by
`for_call`), so a caller who talks it round still gets nothing else. The research notes stay with the person.

`check` refuses what may never go on a call (payment cards, bank details, passwords and codes, any amount of money) and
flags what needs the person's explicit tick (anything on their never-share list, other addresses, emails, phone numbers,
dates of birth, door codes). Refusals are plain sentences the bot can act on.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

try:
    from . import dialing
except ImportError:  # the tests import the modules flat
    import dialing  # type: ignore[no-redef]

#: Whether called numbers can call this line back. Not yet: every inbound call is turned away until callbacks come (a
#: later version), so no call gives out the number to call back on.
CALLBACKS = False
MODES = ("quote", "book", "ask")
MAX_CALLS = 5
MAX_ITEMS = 12
LIMITS = {"title": 80, "goal": 300, "opening": 160, "label": 40, "value": 200, "question": 160, "accept": 200, "notes": 2000, "name": 80,
          "source": 200, "why": 200, "record_label": 60}
RESERVED_RECORD = {"status", "open_questions", "they_asked", "notes", "dont_call_again"}
DEFAULT_RECORD = [{"key": "answer", "label": "What they said"}]

_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

#: Never on a call, whatever the person ticks.
_HARD_WORDS = re.compile(
    r"\b(pass(word|code|phrase)|pin|one[- ]time (code|password)|otp|verification code|security code|cvv|cvc|routing number|"
    r"account number|bank account|cards?|credit|debit|"
    r"payment (card|details|info|method)|expiration date|security question|api key|token|social security|ssn)\b", re.I)
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_MONEY = re.compile(r"(\$\s*\d|\d\s*(dollars?|bucks|usd)\b|\busd\s*\d|\b\d+(\.\d+)?\s*k\b(?!m))", re.I)

#: Allowed only with the person's tick on that fact.
_SENSITIVE_WORDS = re.compile(
    r"\b(date of birth|birth ?day|dob|driver'?s? licen[cs]e|licen[cs]e number|gate code|door code|access code|alarm code|"
    r"lock ?box|e-?mail|cell|mobile|home phone|personal phone)\b", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}")
_STREET = re.compile(
    r"\b\d{1,6}\s+(\w+\s){0,4}(st|street|ave|avenue|rd|road|dr|drive|ln|lane|ct|court|blvd|boulevard|way|pl|place|hwy|"
    r"highway|cir|circle|trl|trail|pkwy|parkway|ter|terrace)\b", re.I)


class BriefError(ValueError):
    """The brief can't be used; `problems` says why, one sentence each."""

    def __init__(self, problems: List[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def clean(text: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", _CONTROL.sub(" ", str(text or ""))).strip()[:limit]


def _luhn_card(text: str) -> bool:
    for match in re.finditer(r"(?:\d[ -]?){13,19}", text):
        digits = [int(d) for d in re.sub(r"\D", "", match.group())]
        if 13 <= len(digits) <= 19:
            total = 0
            for i, d in enumerate(reversed(digits)):
                if i % 2:
                    d *= 2
                    d -= 9 if d > 9 else 0
                total += d
            if total % 10 == 0:
                return True
    return False


def _fold(text: str) -> str:
    """Letters and digits only, so case, spacing and punctuation can't hide a never-share value."""
    return re.sub(r"[\W_]+", "", str(text or "").casefold())


def _digits(text: str) -> str:
    return re.sub(r"\D", "", str(text or ""))


def never_share_hits(text: str, never_share: List[Dict[str, str]]) -> List[str]:
    """Labels of never-share entries whose value appears in `text` (case and spacing ignored; numbers by their digits)."""
    folded, digits = _fold(text), _digits(text)
    hits = []

    for entry in never_share or []:
        value = str(entry.get("value") or "")
        fold, only = _fold(value), _digits(value)
        numeric = len(only) >= 4 and len(only) >= len(re.sub(r"[\s().+-]", "", value)) * 0.6

        if (numeric and only in digits) or (len(fold) >= 4 and fold in folded):
            hits.append(str(entry.get("label") or "a never-share entry"))

    return hits


def _hard(where: str, text: str, problems: List[str]) -> None:
    if _luhn_card(text):
        problems.append(f"{where} looks like a payment card number; cards are never given on a call (the person pays themself).")
    if _SSN.search(text):
        problems.append(f"{where} looks like a Social Security number; it is never given on a call.")
    word = _HARD_WORDS.search(text)
    if word:
        problems.append(f"{where} mentions “{word.group()}”; passwords, codes, PINs and bank or card details never go on a call.")
    if _MONEY.search(text):
        problems.append(f"{where} names an amount of money; a brief never holds money (the call records prices and never names or agrees to one).")


def screen(text: str, never_share: List[Dict[str, str]] = ()) -> List[str]:
    """Why a line can't go on a call (empty when it can): the hard blocks, and values on the never-share list. For what
    crosses during a call: questions the voice wants to ask the person, and the person's answers."""
    problems: List[str] = []
    _hard("It", text, problems)
    problems.extend(f"It holds “{label}” from the never-share list." for label in never_share_hits(text, never_share))
    return problems


def check(raw: Dict[str, Any], settings: Dict[str, Any], callback: str = "") -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """(the clean brief, flags) or BriefError. `callback` is this line's own number, which facts may always mention."""
    if not isinstance(raw, dict):
        raise BriefError(["The brief must be an object."])

    problems: List[str] = []
    flags: List[Dict[str, Any]] = []

    if not clean(settings.get("name"), LIMITS["name"]):
        problems.append("The person hasn't set the name to use on calls yet (in the app: Settings, Phone).")

    mode = raw.get("mode") or "quote"
    if mode not in MODES:
        problems.append(f"mode must be one of {', '.join(MODES)}.")

    title = clean(raw.get("title"), LIMITS["title"])
    goal = clean(raw.get("goal"), LIMITS["goal"])
    if not goal:
        problems.append("goal is missing: one sentence on what the call should get done.")
    for where, text in (("title", title), ("goal", goal)):
        _hard(where, text, problems)

    accept = clean(raw.get("accept"), LIMITS["accept"])
    if mode == "book" and not accept:
        problems.append("A booking call needs accept: the times or choices it may agree to (no money).")
    if mode != "book" and accept:
        accept = ""
    if accept:
        _hard("accept", accept, problems)

    spoken = clean(raw.get("opening"), LIMITS["opening"])
    if spoken:
        _hard("opening", spoken, problems)
        if never_share_hits(spoken, settings.get("never_share") or []) or _EMAIL.search(spoken) or _STREET.search(spoken):
            problems.append("opening is said to whoever answers, even a voicemail: keep private details out of it.")

    facts = []
    raw_facts = raw.get("facts") or []
    if not isinstance(raw_facts, list) or len(raw_facts) > MAX_ITEMS:
        problems.append(f"facts must be a list of at most {MAX_ITEMS}.")
        raw_facts = []
    callback_digits = _digits(callback)[-10:]

    for i, fact in enumerate(raw_facts):
        fact = fact if isinstance(fact, dict) else {}
        label, value = clean(fact.get("label"), LIMITS["label"]), clean(fact.get("value"), LIMITS["value"])
        where = f"Fact {i + 1} ({label or 'no label'})"
        if not label or not value:
            problems.append(f"{where} needs both a label and a value.")
            continue
        both = f"{label}: {value}"
        _hard(where, both, problems)
        reasons = [f"on your never-share list ({hit})" for hit in never_share_hits(both, settings.get("never_share") or [])]
        sensitive = _SENSITIVE_WORDS.search(both)
        if sensitive:
            reasons.append(f"mentions {sensitive.group().lower()}")
        if _EMAIL.search(value):
            reasons.append("contains an email address")
        if any(_digits(m.group())[-10:] != callback_digits for m in _PHONE.finditer(value)):
            reasons.append("contains a phone number other than this line")
        if _STREET.search(value):
            reasons.append("contains a street address")
        facts.append({"label": label, "value": value, "estimate": bool(fact.get("estimate")), "only_if_asked": bool(fact.get("only_if_asked"))})
        if reasons:
            flags.append({"fact": len(facts) - 1, "label": label, "why": "; ".join(dict.fromkeys(reasons))})

    questions = []
    raw_questions = raw.get("questions") or []
    if not isinstance(raw_questions, list) or len(raw_questions) > MAX_ITEMS:
        problems.append(f"questions must be a list of at most {MAX_ITEMS}.")
        raw_questions = []
    for i, question in enumerate(raw_questions):
        text = clean(question, LIMITS["question"])
        if text:
            _hard(f"Question {i + 1}", text, problems)
            questions.append(text)

    record = []
    raw_record = raw.get("record") or []
    if not isinstance(raw_record, list) or len(raw_record) > MAX_ITEMS:
        problems.append(f"record must be a list of at most {MAX_ITEMS} fields.")
        raw_record = []
    for field in raw_record:
        field = field if isinstance(field, dict) else {"key": field, "label": field}
        key = str(field.get("key") or "").strip().lower()
        if not _KEY.match(key) or key in RESERVED_RECORD:
            problems.append(f"record key {key!r} must be a short snake_case name and not one of {', '.join(sorted(RESERVED_RECORD))}.")
            continue
        if key not in {f["key"] for f in record}:
            record.append({"key": key, "label": clean(field.get("label") or key.replace("_", " "), LIMITS["record_label"])})
    record = record or [dict(f) for f in DEFAULT_RECORD]

    calls = []
    raw_calls = raw.get("calls") or []
    if not isinstance(raw_calls, list) or not 1 <= len(raw_calls) <= MAX_CALLS:
        problems.append(f"calls must list 1 to {MAX_CALLS} places to call.")
        raw_calls = []
    blocked = {dialing.try_normalize(n) for n in settings.get("do_not_call") or []}
    seen = set()
    for i, call in enumerate(raw_calls):
        call = call if isinstance(call, dict) else {}
        name = clean(call.get("name"), LIMITS["name"])
        where = f"Call {i + 1} ({name or 'no name'})"
        try:
            number = dialing.normalize(call.get("number"))
        except dialing.NumberError as error:
            problems.append(f"{where}: {error}")
            continue
        if not name:
            problems.append(f"{where} needs the name of who is being called.")
            continue
        if number in blocked:
            problems.append(f"{where}: that number asked not to be called again.")
            continue
        if number == dialing.try_normalize(callback):
            problems.append(f"{where}: that is this line's own number.")
            continue
        if number in seen:
            continue
        seen.add(number)
        calls.append({"name": name, "number": number, "source": clean(call.get("source"), LIMITS["source"]),
                      "why": clean(call.get("why"), LIMITS["why"])})

    if problems:
        raise BriefError(list(dict.fromkeys(problems)))

    brief = {"title": title or goal[:LIMITS["title"]], "mode": mode, "goal": goal, "opening": spoken, "facts": facts, "questions": questions,
             "record": record, "accept": accept, "notes": clean(raw.get("notes"), LIMITS["notes"]), "calls": calls}

    return brief, flags


def apply_edits(brief: Dict[str, Any], remove_facts: List[int], remove_calls: List[int]) -> Dict[str, Any]:
    """The brief with the facts and calls the person struck out removed (indexes into the stored brief)."""
    out = dict(brief)
    out["facts"] = [f for i, f in enumerate(brief["facts"]) if i not in set(remove_facts or [])]
    out["calls"] = [c for i, c in enumerate(brief["calls"]) if i not in set(remove_calls or [])]

    return out


MODE_WORDS = {
    "quote": "Get prices and details only. Never book, schedule or agree to anything.",
    "book": "You may book or schedule, but only within what is listed under \"You may agree to\". Never agree to a price or payment.",
    "ask": "Ask your questions or pass on the message. Never book or agree to anything.",
}


def for_call(brief: Dict[str, Any], call: Dict[str, Any], name: str, answers: List[Dict[str, str]] = ()) -> str:
    """The brief as the voice sees it: only what may cross into the call. No research notes, sources or reasons."""
    lines = [f"Calling: {call['name']}", f"Goal: {brief['goal']}", f"Mode: {MODE_WORDS[brief['mode']]}", "Facts you may share:"]

    for fact in brief["facts"]:
        marks = [m for m, on in (("an estimate: say \"we think\"", fact["estimate"]), ("only if they ask", fact["only_if_asked"])) if on]
        lines.append(f"- {fact['label']}: {fact['value']}" + (f" ({'; '.join(marks)})" if marks else ""))

    lines.append(f"- Name: {name}")
    if CALLBACKS:
        lines.append("- Callback number: the number this call comes from (say \"the number I'm calling from\")")
    else:
        lines.append(f"- Callback number: none to give: say {name} will call them back")

    for answer in answers or []:
        lines.append(f"- {name} answered during this call, to “{answer['question']}”: {answer['answer']}")

    if brief["questions"]:
        lines.append("Questions to ask:")
        lines.extend(f"- {q}" for q in brief["questions"])
    if brief["mode"] == "book" and brief["accept"]:
        lines.append(f"You may agree to: {brief['accept']}")

    lines.append("Write down: " + "; ".join(f"{f['label']} ({f['key']})" for f in brief["record"]))

    return "\n".join(lines)
