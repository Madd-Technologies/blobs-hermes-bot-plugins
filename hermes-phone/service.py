"""hermes-phone's call service: its own process, the only thing Twilio reaches (through Tailscale Funnel or another tunnel).

- Places approved calls, one at a time, within the limits: test numbers only until the stage is `live`, calling hours, a
  daily cap, the line set up. Twilio's own `TimeLimit` caps each call a minute past ours.
- Bridges each answered call: Twilio's Media Stream (μ-law) ⇄ Grok's realtime voice (`audio/pcmu`), with the call brain
  (`brain.py`) deciding what passes.
- Holds the voice's questions and approvals for the person (`_ask`): a push with buttons, a card in the app, a minute to
  answer (three while they listen in).
- Lets the person listen in and take over (`/app/listen`): a WebSocket opened with a single-use ticket that only the
  signed-in dashboard route mints (`store.mint_listen`), carried as a subprotocol so it is never in a path or a log. The
  person hears both sides (`line.py`), and while they have the line their microphone goes to Twilio and into the voice's ears.
  A dropped socket hands the call back to the voice after a few seconds.
- Turns every inbound call away for now (callbacks from called numbers come in a later version).
- When an errand's calls are done: one push to the person, one fixed line to the bot (`deliver.py`).

Every request must carry Twilio's signature for a URL this service built from `public_url`; each call's paths carry a
single-use token, so nothing here logs a path, a URL, a number or a brief.

    python run.py          # what the systemd unit runs, on Hermes's own interpreter
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from urllib.parse import quote
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

try:
    from . import brain as M
    from . import deliver as D
    from . import dialing
    from . import grok
    from . import line as L
    from . import persona as P
    from . import store as S
    from . import twilio_api as T
except ImportError:  # the tests import the modules flat
    import brain as M  # type: ignore[no-redef]
    import deliver as D  # type: ignore[no-redef]
    import dialing  # type: ignore[no-redef]
    import grok  # type: ignore[no-redef]
    import line as L  # type: ignore[no-redef]
    import persona as P  # type: ignore[no-redef]
    import store as S  # type: ignore[no-redef]
    import twilio_api as T  # type: ignore[no-redef]

log = logging.getLogger("hermes-phone")

STEP_S = 1.0
TICK_S = 0.25
RING_S = 30
QUESTION_WAIT_S = 60
#: How long a question waits while the person is listening in: they are right there.
LISTEN_WAIT_S = 180
#: A listener who had the line and dropped: the voice takes the call back after this long without them.
DROP_HAND_BACK_S = 5.0
#: How long a listen socket opened while the call rings waits for it to be answered.
LISTEN_RING_S = RING_S + 20
LISTEN_PROTOCOL = "hermes-phone.listen"
NOTIFY_RETRY_S = 60
NOTIFY_GIVE_UP_S = 30 * 60
START_WAIT_S = 10
#: The server binds its port only after startup returns, so the first dial waits until Twilio's callbacks can reach it.
FIRST_STEP_S = 2.0
#: Twilio doesn't retry a status callback that failed (a tunnel hiccup, a restart), so a dialed call that has gone quiet
#: this long is looked up instead of waited out, and again every CHECK_EVERY_S.
CHECK_AFTER_S = RING_S + 15
CHECK_EVERY_S = 20
READY_WAIT_S = 6
TERMINAL = ("done", "failed", "skipped", "cancelled")
#: SIP codes in Twilio's final callback for a call turned away before it rang. Carriers screen new or low-trust numbers
#: this way (a personal Twilio account's calls go out with the lowest SHAKEN/STIR attestation, C).
BLOCKED = {
    "603": "Turned away before it rang (603): they declined, or their carrier screened it as possible spam.",
    "607": "They marked the call as unwanted (607).",
    "608": "Their carrier blocked it as possible spam (608).",
}
TWILIO_FINAL = {"busy": "no_answer", "no-answer": "no_answer", "failed": "failed", "canceled": "cancelled", "completed": "no_answer"}


@dataclass
class Live:
    errand_id: str
    call_id: str
    brain: M.Brain
    twilio: WebSocket
    grok: Any
    line: L.Line = field(default_factory=L.Line)
    listener: Optional[WebSocket] = None
    lost_at: float = 0.0  # when a listener who had the line dropped
    told: Dict[str, Any] = field(default_factory=dict)  # what the listener was last sent (mode, transcript)


class Service:
    def __init__(self, root: Optional[Path] = None, store: Optional[S.Store] = None, rest: Optional[Callable[[Dict[str, str]], T.Rest]] = None,
                 grok_connect: Optional[Callable[..., Any]] = None, bearer: Optional[Callable[[Path, str], Any]] = None,
                 deliverer: Any = D, clock: Callable[[], float] = time.time, local_hour: Optional[Callable[[], int]] = None):
        self.root = Path(root) if root is not None else S.default_root()
        self.store = store or S.Store(self.root, clock=clock)
        self.rest = rest or (lambda c: T.Rest(c["account_sid"], c["auth_token"], c["api_key"], c["api_secret"]))
        self.grok_connect = grok_connect or grok.connect
        self.bearer = bearer or grok.bearer
        self.deliver = deliverer
        self.clock = clock
        self.local_hour = local_hour or (lambda: time.localtime().tm_hour)
        self.live: Dict[str, Live] = {}
        self.dialing: Optional[str] = None
        self.dialed_at = 0.0
        self.checked_at = 0.0
        self.started = clock()
        self.beat_at = 0.0
        self.app = Starlette(routes=[
            Route("/twilio/voice", self.inbound, methods=["POST"]),
            Route("/twilio/status/{token}", self.status, methods=["POST"]),
            Route("/twilio/amd/{token}", self.amd, methods=["POST"]),
            WebSocketRoute("/twilio/stream/{token}", self.stream),
            WebSocketRoute("/app/listen", self.listen),
            Route("/health", lambda request: JSONResponse({"ok": True})),
        ], lifespan=self._lifespan)

    # configuration

    def creds(self) -> Optional[Dict[str, str]]:
        return T.credentials(T.read_env(self.root / ".env"))

    def public(self) -> str:
        return str(self.store.settings().get("public_url") or "").rstrip("/")

    def urls(self, token: str) -> Dict[str, str]:
        public = self.public()
        return {"status": f"{public}/twilio/status/{token}", "amd": f"{public}/twilio/amd/{token}",
                "stream": "wss://" + public.removeprefix("https://") + f"/twilio/stream/{token}"}

    async def _form(self, request: Request, url: str) -> Optional[Dict[str, str]]:
        creds = self.creds()
        form = {k: str(v) for k, v in (await request.form()).items()}
        if not creds or not self.public() or not T.valid(creds["auth_token"], url, form, request.headers.get("x-twilio-signature")):
            return None
        return form

    # Twilio webhooks

    async def inbound(self, request: Request) -> Response:
        form = await self._form(request, f"{self.public()}/twilio/voice")
        if form is None:
            return Response(status_code=403)
        log.info("hermes-phone: an inbound call was turned away")
        return Response(T.reject_twiml(), media_type="application/xml")

    async def status(self, request: Request) -> Response:
        token = request.path_params["token"]
        form = await self._form(request, self.urls(token)["status"])
        found = self.store.find_call(token) if form is not None else None
        if found is None:
            return Response(status_code=403 if form is None else 204)
        self._twilio_says(*found, form.get("CallStatus", ""), form.get("SipResponseCode", ""))
        return Response(status_code=204)

    def _twilio_says(self, errand: Dict[str, Any], call: Dict[str, Any], twilio_status: str, sip: str = "") -> None:
        """A call's status from Twilio (its callback, or a lookup): ringing, or over before its stream began."""
        token = call["token"]
        if twilio_status == "ringing" and call["status"] == "dialing":
            self.store.update_call(errand["id"], call["id"], lambda c: c.update(status="ringing"))
        elif twilio_status in TWILIO_FINAL and token not in self.live and call["status"] in ("dialing", "ringing"):
            note = BLOCKED.get(sip) or {"busy": "The line was busy.", "failed": "The call couldn't be placed."}.get(twilio_status, "Nobody answered.")
            outcome = {"status": "no_answer", "notes": note}
            self.store.update_call(errand["id"], call["id"], lambda c: c.update(status="done" if twilio_status != "failed" else "failed",
                                                                                 outcome=outcome, ended=self.clock()))
            if self.dialing == token:
                self.dialing = None

    async def amd(self, request: Request) -> Response:
        token = request.path_params["token"]
        form = await self._form(request, self.urls(token)["amd"])
        if form is None:
            return Response(status_code=403)
        live = self.live.get(token)
        if live is not None:
            await live.brain.on_amd(form.get("AnsweredBy", "unknown"))
        return Response(status_code=204)

    # the call's audio

    async def stream(self, ws: WebSocket) -> None:
        token = ws.path_params["token"]
        creds = self.creds()
        if not creds or not self.public() or not T.valid(creds["auth_token"], self.urls(token)["stream"], {}, ws.headers.get("x-twilio-signature")):
            await ws.close(code=1008)
            return
        found = self.store.find_call(token)
        if found is None or found[1]["status"] not in ("dialing", "ringing") or token in self.live:
            await ws.close(code=1008)
            return
        errand, call = found
        await ws.accept()

        try:
            start = await asyncio.wait_for(self._start(ws), START_WAIT_S)
        except (asyncio.TimeoutError, WebSocketDisconnect):
            start = None
        if start is None or (call["sid"] and start.get("start", {}).get("callSid") != call["sid"]):
            await ws.close()
            return
        if self.dialing == token:
            self.dialing = None

        settings = self.store.settings()
        try:
            bearer, account, voice = await asyncio.to_thread(self.bearer, self.root, errand["profile"])
            grok_ws = await self.grok_connect(bearer)
        except Exception as error:
            reason = getattr(error, "reason", type(error).__name__)
            log.warning("hermes-phone: call %s: the voice could not start (%s)", call["id"], reason)
            self.store.update_call(errand["id"], call["id"], lambda c: c.update(status="failed", error=f"The voice could not start ({reason}).", ended=self.clock()))
            await ws.close()
            return

        ready = asyncio.Event()

        async def to_grok(event: Dict[str, Any]) -> None:
            await grok_ws.send(json.dumps(event))

        async def to_twilio(event: Dict[str, Any]) -> None:
            live = self.live.get(token)
            if live is not None and event.get("event") == "media":
                live.line.to_them(event.get("media", {}).get("payload", ""))
            elif live is not None and event.get("event") == "clear":
                live.line.cleared()
            try:
                await ws.send_text(json.dumps(event))
            except Exception:
                pass

        async def hang_up() -> None:
            try:
                await ws.close()
            except Exception:
                pass
            sid = call.get("sid") or start.get("start", {}).get("callSid", "")
            if sid:
                try:
                    await asyncio.to_thread(self.rest(creds).hang_up, sid)
                except Exception:
                    pass  # already over

        def save(update: Dict[str, Any]) -> None:
            update = dict(update)
            if "transcript" in update:
                update["transcript"] = [{k: v for k, v in line.items() if k != "item"} for line in update["transcript"]]
            if update.get("status") == "done":
                update["ended"] = self.clock()
            self.store.update_call(errand["id"], call["id"], lambda c: c.update(update))

        setup = M.Setup(brief=errand["brief"], call=call, name=settings["name"], voice=voice, callback=creds["number"],
                        ask_owner=bool(settings["ask_owner"]), transcripts=bool(settings["transcripts"]), opener=str(settings.get("opener") or ""),
                        max_minutes=int(settings["max_minutes"]), hold_minutes=int(settings["hold_minutes"]), never_share=list(settings["never_share"]))
        brain = M.Brain(setup, M.Ports(grok=to_grok, twilio=to_twilio, hang_up=hang_up, ask_owner=lambda ask: self._ask(errand, call, ask, token),
                                       save=save, clock=self.clock, log=lambda message: log.info("hermes-phone: call %s: %s", call["id"], message)))
        self.live[token] = Live(errand["id"], call["id"], brain, ws, grok_ws)
        log.info("hermes-phone: call %s answered (%s)", call["id"], account)

        try:
            await to_grok({"type": "session.update", "session": P.session(errand["brief"], call, settings["name"], voice,
                                                                          bool(settings["ask_owner"]), True, transcribed=bool(settings["transcripts"]))})
            await brain.on_twilio(start)
            tasks = [asyncio.ensure_future(self._from_twilio(ws, brain, ready, token)), asyncio.ensure_future(self._from_grok(grok_ws, brain, ready)),
                     asyncio.ensure_future(self._ticks(brain, token))]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in tasks:
                task.cancel()
        finally:
            await brain.finish()
            try:
                await grok_ws.close()
            except Exception:
                pass
            ended = self.live.pop(token, None)
            if ended is not None and ended.listener is not None:
                await self._tell(ended, {"type": "end"})
                try:
                    await ended.listener.close()
                except Exception:
                    pass
            self._after_call(errand, call, brain)
            try:
                await ws.close()
            except Exception:
                pass  # already closed by Twilio

    async def _start(self, ws: WebSocket) -> Optional[Dict[str, Any]]:
        while True:
            message = json.loads(await ws.receive_text())
            if message.get("event") == "start":
                return message
            if message.get("event") == "stop":
                return None

    async def _from_twilio(self, ws: WebSocket, brain: M.Brain, ready: asyncio.Event, token: str = "") -> None:
        try:
            await asyncio.wait_for(ready.wait(), READY_WAIT_S)
        except asyncio.TimeoutError:
            return
        try:
            while True:
                message = json.loads(await ws.receive_text())
                live = self.live.get(token)
                if live is not None and message.get("event") == "media":
                    # Their frame paces what the listener hears, and carries the person's microphone into the voice's ears.
                    heard, chunk = live.line.from_them(message.get("media", {}).get("payload", ""), live.listener is not None)
                    message = {**message, "media": {**message["media"], "payload": heard}}
                    if chunk:
                        await self._tell(live, {"type": "audio", "pcmu": chunk})
                await brain.on_twilio(message)
                if message.get("event") == "stop" or brain.state.phase == "ended":
                    return
        except (WebSocketDisconnect, RuntimeError):
            await brain.finish(hung_up_by_them=True)

    async def _from_grok(self, grok_ws: Any, brain: M.Brain, ready: asyncio.Event) -> None:
        try:
            async for raw in grok_ws:
                event = json.loads(raw)
                if event.get("type") == "session.updated":
                    ready.set()
                await brain.on_grok(event)
                if brain.state.phase == "ended":
                    return
        except Exception as error:
            log.warning("hermes-phone: the voice connection dropped (%s)", type(error).__name__)
        if brain.state.phase != "ended":
            brain.state.outcome = brain.state.outcome or {"status": "call_back", "notes": "The voice connection dropped."}

    async def _ticks(self, brain: M.Brain, token: str = "") -> None:
        while brain.state.phase != "ended":
            await asyncio.sleep(TICK_S)
            await brain.tick()
            live = self.live.get(token)
            if live is None:
                continue
            if live.lost_at and live.listener is None and self.clock() - live.lost_at >= DROP_HAND_BACK_S:
                live.lost_at = 0.0
                if await brain.hand_back(dropped=True):
                    self._log(live, "dropped")
            if live.listener is not None:
                await self._update_listener(live)

    async def _ask(self, errand: Dict[str, Any], call: Dict[str, Any], ask: Dict[str, Any], token: str = "") -> Optional[Dict[str, Any]]:
        """Ask the person, not the bot: a question (with choices) or an approval, as a card in the app and a push with buttons.
        It waits about a minute, three while they listen in; their joining the call settles it (this is cancelled)."""
        kind, text, choices = ask.get("kind") or "question", str(ask.get("question") or ""), list(ask.get("choices") or [])
        ask_id = secrets.token_urlsafe(9)
        live = self.live.get(token)
        wait = LISTEN_WAIT_S if live is not None and live.listener is not None else QUESTION_WAIT_S
        record = {"id": ask_id, "kind": kind, "text": text, "choices": choices, "asked": self.clock(), "deadline": self.clock() + wait,
                  "answer": None, "approved": None}
        self.store.update_call(errand["id"], call["id"], lambda c: c.update(question=record))
        self.store.log_call(errand["id"], call["id"], "asked" if kind == "question" else "asked_ok", text)
        body = f"{call['name']}: {text}. Allow it?" if kind == "approval" else f"{call['name']} asks: {text}"
        await asyncio.to_thread(self.deliver.push, self.root, "call_question", profile=errand["profile"], bot="Phone call", body=body,
                                key=f"q-{call['id']}", url=self.app_link(errand, call), actions=self.ask_actions(errand, call, record))
        reply: Optional[Dict[str, Any]] = None
        how = "no_answer"
        try:
            deadline = record["deadline"]
            while self.clock() < deadline:
                await asyncio.sleep(0.5)
                current = next((c for c in (self.store.errand(errand["id"]) or {}).get("calls", []) if c["id"] == call["id"]), {})
                question = current.get("question") or {}
                if question.get("id") != ask_id or current.get("hangup"):
                    break
                deadline = float(question.get("deadline") or deadline)
                if isinstance(question.get("approved"), bool):
                    reply, how = {"approved": question["approved"]}, "allowed" if question["approved"] else "denied"
                    break
                if (question.get("answer") or "").strip():
                    reply, how = {"text": question["answer"].strip()}, "answered"
                    break
        except asyncio.CancelledError:
            how = "joined"
            raise
        finally:
            answer = (reply or {}).get("text") or ({"allowed": "Allowed", "denied": "Denied"}.get(how))

            def settle(c):
                c["question"] = None
                c.setdefault("answers", []).append({"question": text, "answer": answer, "asked": record["asked"], "kind": kind, "how": how})

            try:
                self.store.update_call(errand["id"], call["id"], settle)
                self.store.log_call(errand["id"], call["id"], how, answer or "")
            except KeyError:
                pass
        return reply

    def app_link(self, errand: Dict[str, Any], call: Dict[str, Any]) -> str:
        """The app's live call screen for this call."""
        return (f"hermesbot://bot/{quote(errand['profile'], safe='')}/errand-call?errand={quote(errand['id'], safe='')}"
                f"&call={quote(call['id'], safe='')}")

    def ask_actions(self, errand: Dict[str, Any], call: Dict[str, Any], record: Dict[str, Any]) -> List[Dict[str, str]]:
        """A push's buttons: Allow / Deny, or the first two choices, then Join. Each opens the app, which answers through the
        signed-in dashboard; the ask's random id in the link means only this question can be answered by it."""
        base = (f"hermesbot://phone/answer?profile={quote(errand['profile'], safe='')}&errand={quote(errand['id'], safe='')}"
                f"&call={quote(call['id'], safe='')}&ask={quote(record['id'], safe='')}")
        if record["kind"] == "approval":
            buttons = [{"title": "Allow", "url": f"{base}&approved=1"}, {"title": "Deny", "url": f"{base}&approved=0"}]
        else:
            buttons = [{"title": choice, "url": f"{base}&choice={quote(choice, safe='')}"} for choice in record["choices"][:2]]
        join = (f"hermesbot://phone/join?profile={quote(errand['profile'], safe='')}&errand={quote(errand['id'], safe='')}"
                f"&call={quote(call['id'], safe='')}")
        return buttons + [{"title": "Join the call", "url": join}]

    # listening in and taking over

    def _log(self, live: Live, what: str, text: str = "") -> None:
        try:
            self.store.log_call(live.errand_id, live.call_id, what, text)
        except KeyError:
            pass

    async def _tell(self, live: Live, message: Dict[str, Any]) -> None:
        if live.listener is None:
            return
        try:
            await live.listener.send_text(json.dumps(message))
        except Exception:
            pass

    async def _update_listener(self, live: Live) -> None:
        """Where the call is (who has the line) and the transcript lines that are new or refined."""
        brain = live.brain
        mode = brain.mode
        if live.told.get("mode") != mode:
            live.told["mode"] = mode
            await self._tell(live, {"type": "state", "mode": mode})
        lines = brain.state.transcript
        sent = live.told.get("lines", 0)
        last = (lines[sent - 1]["text"] if 0 < sent <= len(lines) else None)
        start = sent - 1 if sent and last != live.told.get("last") else sent
        if start < len(lines):
            await self._tell(live, {"type": "lines", "from": start, "lines": [{k: v for k, v in line.items() if k != "item"} for line in lines[start:]]})
            live.told["lines"], live.told["last"] = len(lines), lines[-1]["text"]

    async def _wait_live(self, token: str) -> Optional[Live]:
        deadline = self.clock() + LISTEN_RING_S
        while self.clock() < deadline:
            live = self.live.get(token)
            if live is not None:
                return live
            found = self.store.find_call(token)
            if found is None or found[1]["status"] not in S.CALL_ACTIVE:
                return None
            await asyncio.sleep(0.5)
        return None

    async def listen(self, ws: WebSocket) -> None:
        """The person listening in on a call, and taking it over. Opened only with a fresh ticket from the dashboard."""
        offered = [p.strip() for p in (ws.headers.get("sec-websocket-protocol") or "").split(",")]
        ticket = next((p[len("ticket."):] for p in offered if p.startswith("ticket.")), "")
        found = self.store.use_listen(ticket) if LISTEN_PROTOCOL in offered else None
        if found is None:
            await ws.close(code=1008)
            return
        errand, call = found
        await ws.accept(subprotocol=LISTEN_PROTOCOL)
        live = await self._wait_live(call["token"])
        if live is None:
            await ws.send_text(json.dumps({"type": "end"}))
            await ws.close()
            return
        if live.listener is not None:  # one listener per call: the newest
            old, live.listener = live.listener, None
            try:
                await old.close()
            except Exception:
                pass
        live.listener, live.lost_at, live.told = ws, 0.0, {}
        self._log(live, "listen")
        self._longer_wait(live)
        brain = live.brain
        await self._tell(live, {"type": "hello", "mode": brain.mode, "started": brain.state.started,
                                "ends": brain.state.started + brain.setup.max_minutes * 60})
        await self._update_listener(live)
        try:
            while True:
                message = json.loads(await ws.receive_text())
                kind = message.get("type") if isinstance(message, dict) else None
                if kind == "audio":
                    if brain.mode == "owner" and brain.state.stream:
                        data = live.line.from_person(str(message.get("pcmu") or ""))
                        if data:
                            # Straight to them, never into what the listener hears (they'd hear themselves late).
                            await live.twilio.send_text(json.dumps({"event": "media", "streamSid": brain.state.stream,
                                                                    "media": {"payload": message["pcmu"]}}))
                elif kind == "take_over":
                    if await brain.take_over():
                        self._log(live, "took_over")
                    else:
                        await self._tell(live, {"type": "refused", "message": "The call can't be joined right now."})
                elif kind == "hand_back":
                    live.line.stop_mic()
                    if await brain.hand_back(str(message.get("note") or "")):
                        self._log(live, "handed_back", str(message.get("note") or ""))
                elif kind == "leave":
                    break
                await self._update_listener(live)
        except (WebSocketDisconnect, RuntimeError, ValueError):
            pass
        finally:
            if live.listener is ws:
                live.listener = None
                live.line.stop_mic()
                if brain.mode in ("owner", "handover"):
                    live.lost_at = self.clock()
                self._log(live, "stopped_listening")
            try:
                await ws.close()
            except Exception:
                pass

    def _longer_wait(self, live: Live) -> None:
        """A question already waiting gets the longer wait once the person is listening."""
        def longer(c):
            question = c.get("question")
            if question and question.get("answer") is None and question.get("approved") is None:
                question["deadline"] = max(float(question.get("deadline") or 0), float(question["asked"]) + LISTEN_WAIT_S)

        try:
            self.store.update_call(live.errand_id, live.call_id, longer)
        except KeyError:
            pass

    def _after_call(self, errand: Dict[str, Any], call: Dict[str, Any], brain: M.Brain) -> None:
        self.store.remember_number(call["number"], {"errand": errand["id"], "call": call["id"], "name": call["name"], "profile": errand["profile"]})
        if (brain.state.outcome or {}).get("dont_call_again"):
            blocked = self.store.settings()["do_not_call"]
            if call["number"] not in blocked:
                self.store.update_settings({"do_not_call": blocked + [call["number"]]})
        log.info("hermes-phone: call %s ended (%s)", call["id"], (brain.state.outcome or {}).get("status"))

    # the dialer

    async def step(self) -> None:
        self.store.expire_pending()
        await self._hangups()
        if (self.dialing is not None and not self.live and self.clock() - self.dialed_at > CHECK_AFTER_S
                and self.clock() - self.checked_at > CHECK_EVERY_S):
            await self._look_up_dialing()
        if not self.live and (self.dialing is None or self.clock() - self.dialed_at > RING_S + 90):
            if self.dialing is not None:
                self._dial_timed_out(self.dialing)
            await self._dial_next()
        await self._finish_errands()
        if self.clock() - self.beat_at >= S.HEARTBEAT_S:
            self.beat_at = self.clock()
            self.store.beat({"pid": os.getpid(), "started": self.started, "live": len(self.live), "line": bool(self.creds()),
                             "public_url": bool(self.public())})

    async def _hangups(self) -> None:
        for errand in self.store.errands(status=("running", "cancelled"), limit=20):
            for call in errand["calls"]:
                if call.get("hangup") and call["status"] in S.CALL_ACTIVE:
                    live = self.live.get(call["token"])
                    if live is not None:
                        live.brain.state.outcome = live.brain.state.outcome or {"status": "call_back", "notes": "Hung up from the app."}
                        await live.brain.finish()
                    elif call["sid"] and self.creds():
                        try:
                            await asyncio.to_thread(self.rest(self.creds()).hang_up, call["sid"])
                        except T.TwilioError:
                            pass

    async def _look_up_dialing(self) -> None:
        self.checked_at = self.clock()
        found = self.store.find_call(self.dialing or "")
        creds = self.creds()
        if not found or not found[1].get("sid") or not creds:
            return
        try:
            twilio_status = await asyncio.to_thread(self.rest(creds).call_status, found[1]["sid"])
        except T.TwilioError:
            return
        self._twilio_says(*found, twilio_status)

    def _dial_timed_out(self, token: str) -> None:
        found = self.store.find_call(token)
        if found and found[1]["status"] in ("dialing", "ringing"):
            self.store.update_call(found[0]["id"], found[1]["id"], lambda c: c.update(status="failed", error="Twilio never reported back on the call.", ended=self.clock()))
        self.dialing = None

    def _problem(self, errand: Dict[str, Any], problem: str) -> None:
        if errand.get("problem") != problem:
            self.store.update_errand(errand["id"], lambda e: e.update(problem=problem))

    async def _dial_next(self) -> None:
        settings = self.store.settings()
        for errand in sorted(self.store.errands(status=("approved", "running"), limit=50), key=lambda e: e["created"]):
            if any(c["status"] in S.CALL_ACTIVE for c in errand["calls"]):
                return
            waiting = [c for c in errand["calls"] if c["status"] == "waiting"]
            if not waiting:
                continue
            if (errand.get("decided") or {}).get("hash") != S.brief_hash(errand["brief"]):
                self.store.update_errand(errand["id"], lambda e: e.update(status="cancelled", problem="The brief changed after it was approved."))
                log.warning("hermes-phone: errand %s changed after approval; not dialed", errand["id"])
                continue
            creds = self.creds()
            if not creds or not self.public():
                self._problem(errand, "The phone line isn't set up yet (Twilio keys and the public address).")
                return
            call = None
            for candidate in waiting:
                if settings["stage"] == "test" and candidate["number"] not in {dialing.try_normalize(n) for n in settings["test_numbers"]}:
                    self.store.update_call(errand["id"], candidate["id"], lambda c: c.update(status="skipped", error="Test stage: only test numbers are called."))
                    continue
                call = candidate
                break
            if call is None:
                continue
            start, end = settings["hours"]
            if not start <= self.local_hour() < end:
                self._problem(errand, f"Waiting for calling hours ({start}:00 to {end}:00).")
                return
            if self.store.starts_today() >= settings["daily_cap"]:
                self._problem(errand, f"The daily limit of {settings['daily_cap']} calls was reached.")
                return
            urls = self.urls(call["token"])
            try:
                sid = await asyncio.to_thread(self.rest(creds).create_call, call["number"], creds["number"], T.stream_twiml(urls["stream"]),
                                              urls["status"], urls["amd"], int(settings["max_minutes"]) * 60 + 60, RING_S)
            except T.TwilioError as error:
                log.warning("hermes-phone: call %s was refused by Twilio (%s)", call["id"], error.code or error.status)
                self.store.update_call(errand["id"], call["id"], lambda c: c.update(status="failed", error=str(error), ended=self.clock()))
                return  # one attempt per step; the next step moves on
            self.store.log_start()
            self.store.update_call(errand["id"], call["id"], lambda c: c.update(status="dialing", sid=sid, started=self.clock()))
            self.store.update_errand(errand["id"], lambda e: e.update(status="running", problem=""))
            self.dialing, self.dialed_at = call["token"], self.clock()
            log.info("hermes-phone: dialing call %s of errand %s", call["id"], errand["id"])
            return

    async def _finish_errands(self) -> None:
        for errand in self.store.errands(status=("approved", "running", "done", "cancelled"), limit=50):
            if errand["status"] in ("approved", "running") and all(c["status"] in TERMINAL for c in errand["calls"]):
                errand = self.store.update_errand(errand["id"], lambda e: e.update(status="done", finished=self.clock()))
            if errand["status"] not in ("done", "cancelled") or errand.get("notified") in (True, "gave_up"):
                continue
            if errand.get("notify_after", 0) > self.clock():
                continue
            await self._notify(errand)

    async def _notify(self, errand: Dict[str, Any]) -> None:
        line = D.errand_line(errand)
        if not errand.get("pushed"):
            summary = line.split(" · ")[-1].split("]")[0]
            await asyncio.to_thread(self.deliver.push, self.root, "call", profile=errand["profile"], bot="Phone call",
                                    body=f"{errand['brief']['title']}: {summary}", key=f"e-{errand['id']}", url="hermesbot://inbox")
            self.store.update_errand(errand["id"], lambda e: e.update(pushed=True))
        result = await asyncio.to_thread(self.deliver.tell_bot, self.root, errand["profile"], line, D.delivery_id(errand["id"]))
        if result == "delivered":
            self.store.update_errand(errand["id"], lambda e: e.update(notified=True))
        elif self.clock() - errand.get("finished", self.clock()) > NOTIFY_GIVE_UP_S or result == "failed":
            self.store.update_errand(errand["id"], lambda e: e.update(notified="gave_up"))
        else:
            self.store.update_errand(errand["id"], lambda e: e.update(notify_after=self.clock() + NOTIFY_RETRY_S))
        log.info("hermes-phone: errand %s: the bot was told (%s)", errand["id"], result)

    @asynccontextmanager
    async def _lifespan(self, app):
        async def loop():
            await asyncio.sleep(FIRST_STEP_S)
            while True:
                try:
                    await self.step()
                except Exception:
                    log.exception("hermes-phone: the dialer step failed")
                await asyncio.sleep(STEP_S)

        task = asyncio.ensure_future(loop())
        log.info("hermes-phone: call service up (line %s, public address %s)", "set" if self.creds() else "NOT set",
                 "set" if self.public() else "NOT set")
        try:
            yield
        finally:
            task.cancel()


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    service = Service()
    port = int(service.store.settings()["port"])
    # No access log: every Twilio path carries a call's token.
    uvicorn.run(service.app, host="127.0.0.1", port=port, access_log=False, log_level="warning", proxy_headers=False)
