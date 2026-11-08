"""The app's side of hermes-phone: settings, the errands waiting for the person's OK, and live calls.

Mounted by Hermes under `/api/plugins/hermes-phone/`, behind the dashboard's own sign-in. Install the plugin in the default
profile; one copy serves the app for every bot.

  GET  /status                                  {version, service, line, public_url, stage, features}
  GET  /settings                                the settings; never-share values are never sent back, only a hint
  PUT  /settings                                any settings, plus never_share_add [{label, value}] / never_share_remove [label]
  GET  /errands?open=true                       newest first
  GET  /errands/{id}                            one errand: the brief, flags, research notes, calls, outcomes, transcripts
  POST /errands/{id}/approve                    {ticked: [fact index], remove_facts: [index], remove_calls: [index]}
  POST /errands/{id}/decline
  POST /errands/{id}/cancel                     stop the calls not made yet, hang up a live one
  POST /errands/{id}/calls/{call_id}/hangup
  POST /errands/{id}/calls/{call_id}/answer     {text} | {choice} | {approved}, with the question's {ask} id: the answer to
                                                a question or approval a call is waiting on (screened like a brief)
  POST /errands/{id}/calls/{call_id}/listen     {url, protocols, expires}: a single-use ticket to listen in on a live call
                                                and take it over, on the call service's own socket

Approving is the only way an errand gets dialed: every flagged fact must be ticked, the edited brief is checked again, and
the approval records a hash of the brief so the call service refuses one that changed afterwards. Refusals are
`{"detail": {"reason", "message"}}`.
"""

from __future__ import annotations

import importlib
import logging
import sys
import types
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Query
from fastapi.responses import JSONResponse

PLUGIN = Path(__file__).resolve().parent.parent
_PACKAGE = "_hermes_phone_core"
log = logging.getLogger("hermes-phone")


def _core(name: str):
    """A module of the plugin, loaded once under a private package (Hermes imports this file apart from the plugin)."""
    if _PACKAGE not in sys.modules:
        package = types.ModuleType(_PACKAGE)
        package.__path__ = [str(PLUGIN)]  # type: ignore[attr-defined]
        sys.modules[_PACKAGE] = package
    return importlib.import_module(f"{_PACKAGE}.{name}")


def _root() -> Path:
    from hermes_constants import get_default_hermes_root

    return get_default_hermes_root()


#: Tests replace these.
root = _root
clock = None


def _store():
    S = _core("store")
    return S.Store(root(), clock=clock) if clock else S.Store(root())


def _refuse(status: int, reason: str, message: str) -> JSONResponse:
    log.info("hermes-phone: refused (%s)", reason)
    return JSONResponse(status_code=status, content={"detail": {"reason": reason, "message": message}})


def _version() -> str:
    try:
        for line in (PLUGIN / "plugin.yaml").read_text(encoding="utf-8").splitlines():
            if line.startswith("version:"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return ""


def _line() -> Optional[Dict[str, str]]:
    T = _core("twilio_api")
    return T.credentials(T.read_env(root() / ".env"))


def _hint(value: str) -> str:
    value = str(value or "")
    return f"…{value[-2:]}" if len(value) > 4 else "…"


router = APIRouter()
#: What this version offers the app: questions with choices and approvals on a call, and listening in / taking over.
FEATURES = ("ask_kinds", "listen")


@router.get("/status")
def status() -> Dict[str, Any]:
    store, line = _store(), _line()
    settings = store.settings()
    service = store.service()
    return {"version": _version(), "service": {"running": service["running"], "live": service.get("live", 0)},
            "line": {"set": bool(line), "number": _core("dialing").masked(line["number"]) if line else ""},
            "public_url": bool(settings["public_url"]), "stage": settings["stage"], "features": list(FEATURES)}


def _settings_view(settings: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(settings)
    out["never_share"] = [{"label": e["label"], "hint": _hint(e["value"])} for e in settings["never_share"]]
    return out


@router.get("/settings")
def get_settings() -> Dict[str, Any]:
    return _settings_view(_store().settings())


@router.put("/settings")
def put_settings(change: Dict[str, Any] = Body(...)):
    S = _core("store")
    store = _store()
    change = dict(change)
    added = change.pop("never_share_add", None) or []
    removed = set(change.pop("never_share_remove", None) or [])
    change.pop("never_share", None)  # the whole list is never sent: values are write-only
    try:
        if added or removed:
            current = [e for e in store.settings()["never_share"] if e["label"] not in removed]
            labels = {e.get("label") for e in added if isinstance(e, dict)}
            change["never_share"] = [e for e in current if e["label"] not in labels] + list(added)
        return _settings_view(store.update_settings(change))
    except S.SettingsError as error:
        return _refuse(400, "invalid", str(error))


@router.get("/errands")
def list_errands(open: bool = Query(False)):
    S = _core("store")
    errands = _store().errands(status=S.ERRAND_OPEN if open else None, limit=50)
    return {"errands": [{"id": e["id"], "profile": e["profile"], "title": e["brief"]["title"], "status": e["status"],
                         "created": e["created"], "calls": len(e["calls"]), "flags": len(e["flags"]), "problem": e.get("problem") or "",
                         "question": next((c["question"] for c in e["calls"] if c.get("question")), None)} for e in errands]}


@router.get("/errands/{errand_id}")
def get_errand(errand_id: str):
    errand = _store().errand(errand_id)
    if not errand:
        return _refuse(404, "unknown", "No such errand.")
    errand = dict(errand)
    errand["calls"] = [{k: v for k, v in c.items() if k not in ("token", "sid", "listen")} for c in errand["calls"]]
    return errand


@router.post("/errands/{errand_id}/approve")
def approve(errand_id: str, body: Dict[str, Any] = Body(default={})):
    B, S = _core("brief"), _core("store")
    store = _store()
    errand = store.errand(errand_id)
    if not errand:
        return _refuse(404, "unknown", "No such errand.")
    if errand["status"] != "pending":
        return _refuse(409, "decided", f"This errand is already {errand['status']}.")
    line = _line()
    if not line:
        return _refuse(409, "no_line", "The phone line isn't set up yet.")

    def ints(name: str) -> List[int]:
        values = body.get(name) or []
        return [v for v in values if isinstance(v, int) and not isinstance(v, bool)]

    removed_facts, removed_calls, ticked = set(ints("remove_facts")), set(ints("remove_calls")), set(ints("ticked"))
    kept_facts = [i for i in range(len(errand["brief"]["facts"])) if i not in removed_facts]
    edited = B.apply_edits(errand["brief"], sorted(removed_facts), sorted(removed_calls))
    try:
        brief, flags = B.check(edited, store.settings(), line["number"])
    except B.BriefError as error:
        return _refuse(400, "invalid", " ".join(error.problems))
    unticked = [f for f in flags if kept_facts[f["fact"]] not in ticked]
    if unticked:
        return _refuse(409, "unticked", "Tick or remove: " + "; ".join(f"{f['label']} ({f['why']})" for f in unticked))

    kept_calls = [c for c in errand["calls"] if c["index"] not in removed_calls]

    def decide(e: Dict[str, Any]) -> None:
        e.update(brief=brief, flags=flags, ticked=sorted(i for i in range(len(brief["facts"])) if kept_facts[i] in ticked),
                 status="approved", decided={"at": store.clock(), "by": "app", "hash": S.brief_hash(brief)})
        e["calls"] = [{**c, "index": i} for i, c in enumerate(kept_calls)]

    store.update_errand(errand_id, decide)
    log.info("hermes-phone: errand %s approved (%d calls)", errand_id, len(kept_calls))
    return get_errand(errand_id)


@router.post("/errands/{errand_id}/decline")
def decline(errand_id: str):
    store = _store()
    errand = store.errand(errand_id)
    if not errand:
        return _refuse(404, "unknown", "No such errand.")
    if errand["status"] != "pending":
        return _refuse(409, "decided", f"This errand is already {errand['status']}.")
    store.update_errand(errand_id, lambda e: e.update(status="declined", decided={"at": store.clock(), "by": "app"}, notified=True))
    return get_errand(errand_id)


@router.post("/errands/{errand_id}/cancel")
def cancel(errand_id: str):
    S = _core("store")
    store = _store()
    errand = store.errand(errand_id)
    if not errand:
        return _refuse(404, "unknown", "No such errand.")
    if errand["status"] not in ("approved", "running"):
        return _refuse(409, "not_running", f"This errand is {errand['status']}.")

    def stop(e: Dict[str, Any]) -> None:
        e["status"] = "cancelled"
        for c in e["calls"]:
            if c["status"] == "waiting":
                c["status"] = "cancelled"
            elif c["status"] in S.CALL_ACTIVE:
                c["hangup"] = True

    store.update_errand(errand_id, stop)
    return get_errand(errand_id)


@router.post("/errands/{errand_id}/calls/{call_id}/hangup")
def hangup(errand_id: str, call_id: str):
    try:
        _store().update_call(errand_id, call_id, lambda c: c.update(hangup=True))
    except KeyError:
        return _refuse(404, "unknown", "No such call.")
    return get_errand(errand_id)


@router.post("/errands/{errand_id}/calls/{call_id}/answer")
def answer(errand_id: str, call_id: str, body: Dict[str, Any] = Body(...)):
    """One answer to what a call is waiting on: typed text, one of its choices, or Allow / Deny for an approval. With `ask`
    (a push button's link carries it), only that question is answered."""
    text = " ".join(str(body.get("text") or "").split())[:300]
    choice = " ".join(str(body.get("choice") or "").split())
    approved = body.get("approved") if isinstance(body.get("approved"), bool) else None
    if not text and not choice and approved is None:
        return _refuse(400, "invalid", "The answer is empty.")
    store = _store()
    problems = _core("brief").screen(text, store.settings()["never_share"]) if text else []
    if problems:
        return _refuse(400, "not_on_a_call", " ".join(problems))
    state: Dict[str, Any] = {}

    def give(c: Dict[str, Any]) -> None:
        question = c.get("question")
        if (not question or question.get("answer") or isinstance(question.get("approved"), bool)
                or (body.get("ask") and body.get("ask") != question.get("id"))):
            state["late"] = True
            return
        kind = question.get("kind") or "question"
        if approved is not None:
            if kind != "approval":
                state["wrong"] = "This is a question, not an approval."
                return
            question["approved"] = approved
        elif choice:
            if choice not in (question.get("choices") or []):
                state["wrong"] = "That isn't one of the choices."
                return
            question["answer"] = choice
        else:
            question["answer"] = text

    try:
        store.update_call(errand_id, call_id, give)
    except KeyError:
        return _refuse(404, "unknown", "No such call.")
    if state.get("late"):
        return _refuse(409, "late", "The call isn't waiting on that any more.")
    if state.get("wrong"):
        return _refuse(400, "invalid", state["wrong"])
    return get_errand(errand_id)


@router.post("/errands/{errand_id}/calls/{call_id}/listen")
def listen(errand_id: str, call_id: str):
    """A ticket to listen in on a call that is being made, used once within 30 s on the call service's socket. Only this
    signed-in route mints one: the voice, the bot and the other party never can."""
    S = _core("store")
    store = _store()
    errand = store.errand(errand_id)
    call = next((c for c in (errand or {}).get("calls", []) if c["id"] == call_id), None)
    if not errand or not call:
        return _refuse(404, "unknown", "No such call.")
    if call["status"] not in S.CALL_ACTIVE:
        return _refuse(409, "not_live", "The call isn't on the line.")
    public = str(store.settings().get("public_url") or "").rstrip("/")
    if not public or not store.service()["running"]:
        return _refuse(409, "no_service", "The call service isn't reachable.")
    ticket = store.mint_listen(errand_id, call_id)
    log.info("hermes-phone: call %s: a listen ticket was minted", call_id)
    return {"url": "wss://" + public.removeprefix("https://") + "/app/listen", "protocols": ["hermes-phone.listen", f"ticket.{ticket}"],
            "expires": S.LISTEN_TICKET_S}
