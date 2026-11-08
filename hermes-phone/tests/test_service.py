"""The call service with fakes for Twilio's REST API, Grok's socket and the deliverer; Twilio's webhooks and Media Stream go
through Starlette's test client, signed like Twilio signs them."""

import asyncio
import base64
import json
import signal
import time
import tempfile
import unittest
from pathlib import Path

from helpers import SETTINGS, septic
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import brief as B  # noqa: E402
import service as V  # noqa: E402
import store as S  # noqa: E402
import twilio_api as T  # noqa: E402
import ulaw  # noqa: E402

PUBLIC = "https://node.example.ts.net:8443"
ENV = "HERMES_PHONE_TWILIO_ACCOUNT_SID=AC1\nHERMES_PHONE_TWILIO_AUTH_TOKEN=tok\nHERMES_PHONE_TWILIO_PHONE_NUMBER=+12025550123\n"
QUIET = base64.b64encode(bytes([ulaw.SILENCE]) * 160).decode()
LOUD = ulaw.frames(ulaw.tone("5"))[0]


class FakeRest:
    def __init__(self, sid="CA1", error=None):
        self.created, self.hung, self.looked_up = [], [], []
        self.sid, self.error = sid, error
        self.status = "ringing"

    def create_call(self, *args):
        if self.error:
            raise self.error
        self.created.append(args)
        return self.sid

    def hang_up(self, sid):
        self.hung.append(sid)

    def call_status(self, sid):
        self.looked_up.append(sid)
        return self.status


class FakeGrok:
    """Answers a session.update with session.updated, and a scripted line with a short spoken response."""

    def __init__(self):
        self.sent, self.queue, self.closed = [], None, False

    def _q(self):
        if self.queue is None:
            self.queue = asyncio.Queue()
        return self.queue

    async def send(self, raw):
        event = json.loads(raw)
        self.sent.append(event)
        if event["type"] == "session.update" and "audio" in event["session"]:
            await self._q().put({"type": "session.updated", "session": {"audio": event["session"]["audio"]}})
        if event["type"] == "conversation.item.create" and event["item"]["type"] == "force_message":
            for reply in ({"type": "response.created", "response": {"id": "r1"}}, {"type": "response.output_audio.delta", "delta": "OPENING"},
                          {"type": "response.output_audio_transcript.done", "transcript": event["item"]["content"][0]["text"]},
                          {"type": "response.done"}):
                await self._q().put(reply)

    def __aiter__(self):
        return self

    async def __anext__(self):
        event = await self._q().get()
        if event is None:
            raise StopAsyncIteration
        return json.dumps(event)

    async def close(self):
        self.closed = True
        await self._q().put(None)


class FakeDeliver:
    def __init__(self, result="delivered"):
        self.pushes, self.told, self.result = [], [], result

    def push(self, root, kind, **kwargs):
        self.pushes.append((kind, kwargs))

    def tell_bot(self, root, profile, line, key):
        self.told.append((profile, line, key))
        return self.result


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        (self.root / ".env").write_text(ENV)
        self.t = [1000.0]
        self.store = S.Store(self.root, clock=lambda: self.t[0])
        self.store.update_settings({"name": "Alex", "public_url": PUBLIC, "test_numbers": ["202-555-0101", "202-555-0102"]})
        self.rest = FakeRest()
        self.grok = FakeGrok()
        self.deliver = FakeDeliver()
        self.hour = [12]

        async def connect(token):
            self.connected = token
            return self.grok

        self.service = V.Service(root=self.root, store=self.store, rest=lambda creds: self.rest, grok_connect=connect,
                                 bearer=lambda root, profile: ("xai-token", "sign-in", "ara"), deliverer=self.deliver,
                                 clock=lambda: self.t[0], local_hour=lambda: self.hour[0])
        self.client = TestClient(self.service.app)

    def tearDown(self):
        self.dir.cleanup()

    def approved(self, **extra):
        brief, flags = B.check(septic(**extra), SETTINGS, "+12025550123")
        errand = self.store.new_errand("default", "s1", brief, flags)
        self.store.update_errand(errand["id"], lambda e: e.update(status="approved", decided={"by": "app", "hash": S.brief_hash(e["brief"])}))
        return self.store.errand(errand["id"])

    def step(self):
        asyncio.run(self.service.step())

    def signed_post(self, path, form):
        url = PUBLIC.replace(":8443", "") + path  # Twilio signs voice callbacks without the port
        return self.client.post(path, data=form, headers={"X-Twilio-Signature": T.signature("tok", url, form)})


class Webhooks(Base):
    def test_inbound_calls_are_turned_away(self):
        answer = self.signed_post("/twilio/voice", {"CallSid": "CA9", "From": "+12025550188"})
        self.assertEqual(answer.status_code, 200)
        self.assertIn("<Reject/>", answer.text)
        self.assertEqual(self.client.post("/twilio/voice", data={"CallSid": "CA9"}, headers={"X-Twilio-Signature": "bad"}).status_code, 403)

    def test_a_busy_line(self):
        errand = self.approved()
        self.step()
        token = errand["calls"][0]["token"]
        self.assertEqual(self.service.dialing, token)
        self.assertEqual(self.signed_post(f"/twilio/status/{token}", {"CallSid": "CA1", "CallStatus": "busy"}).status_code, 204)
        call = self.store.errand(errand["id"])["calls"][0]
        self.assertEqual((call["status"], call["outcome"]["notes"]), ("done", "The line was busy."))
        self.assertIsNone(self.service.dialing)

    def test_a_call_blocked_before_it_rang_says_so(self):
        errand = self.approved()
        self.step()
        token = errand["calls"][0]["token"]
        self.signed_post(f"/twilio/status/{token}", {"CallSid": "CA1", "CallStatus": "busy", "SipResponseCode": "603"})
        call = self.store.errand(errand["id"])["calls"][0]
        self.assertEqual(call["outcome"]["status"], "no_answer")
        self.assertIn("possible spam", call["outcome"]["notes"])

    def test_a_call_whose_callbacks_were_lost_is_looked_up(self):
        errand = self.approved()
        self.step()
        self.t[0] += V.CHECK_AFTER_S - 1
        self.step()
        self.assertEqual(self.rest.looked_up, [])
        self.t[0] += 2
        self.step()
        call = self.store.errand(errand["id"])["calls"][0]
        self.assertEqual((self.rest.looked_up, call["status"]), (["CA1"], "ringing"))
        self.t[0] += 1
        self.step()
        self.assertEqual(len(self.rest.looked_up), 1)  # not again before CHECK_EVERY_S
        self.rest.status = "busy"
        self.t[0] += V.CHECK_EVERY_S
        self.step()
        first, second = self.store.errand(errand["id"])["calls"]
        self.assertEqual((first["status"], first["outcome"]["notes"]), ("done", "The line was busy."))
        self.assertEqual(self.service.dialing, second["token"])  # and the same step moved on to the next call

    def test_unsigned_callbacks_change_nothing(self):
        errand = self.approved()
        self.step()
        token = errand["calls"][0]["token"]
        self.assertEqual(self.client.post(f"/twilio/status/{token}", data={"CallStatus": "busy"}).status_code, 403)
        self.assertEqual(self.store.errand(errand["id"])["calls"][0]["status"], "dialing")


class Dialer(Base):
    def test_dials_the_first_call_with_our_urls_and_twilios_own_cap(self):
        errand = self.approved()
        self.step()
        to, from_, twiml, status_url, amd_url, limit, ring = self.rest.created[0]
        token = errand["calls"][0]["token"]
        self.assertEqual((to, from_, limit, ring), ("+12025550101", "+12025550123", 21 * 60, V.RING_S))
        self.assertIn(f'url="wss://node.example.ts.net:8443/twilio/stream/{token}"', twiml)
        self.assertEqual((status_url, amd_url), (f"{PUBLIC}/twilio/status/{token}", f"{PUBLIC}/twilio/amd/{token}"))
        after = self.store.errand(errand["id"])
        self.assertEqual((after["status"], after["calls"][0]["status"], after["calls"][0]["sid"]), ("running", "dialing", "CA1"))
        self.step()
        self.assertEqual(len(self.rest.created), 1)  # one call at a time

    def test_test_stage_skips_other_numbers(self):
        self.store.update_settings({"test_numbers": ["202-555-0102"]})
        errand = self.approved()
        self.step()
        calls = self.store.errand(errand["id"])["calls"]
        self.assertEqual((calls[0]["status"], calls[1]["status"]), ("skipped", "dialing"))

    def test_guards(self):
        errand = self.approved()
        self.hour[0] = 22
        self.step()
        self.assertEqual(self.rest.created, [])
        self.assertIn("calling hours", self.store.errand(errand["id"])["problem"])
        self.hour[0] = 12
        self.store.update_settings({"daily_cap": 1})
        self.store.log_start()
        self.step()
        self.assertIn("daily limit", self.store.errand(errand["id"])["problem"])

    def test_no_line_no_calls(self):
        (self.root / ".env").write_text("")
        errand = self.approved()
        self.step()
        self.assertIn("isn't set up", self.store.errand(errand["id"])["problem"])

    def test_a_brief_changed_after_approval_is_never_dialed(self):
        errand = self.approved()
        self.store.update_errand(errand["id"], lambda e: e["brief"]["facts"].append({"label": "Home address", "value": "12 Orchard Lane"}))
        self.step()
        self.assertEqual(self.rest.created, [])
        self.assertEqual(self.store.errand(errand["id"])["status"], "cancelled")

    def test_twilio_refusing_a_call(self):
        self.rest.error = T.TwilioError("Account not authorized to call this number (Geo Permissions)", 400, 21215)
        errand = self.approved()
        self.step()
        call = self.store.errand(errand["id"])["calls"][0]
        self.assertEqual(call["status"], "failed")
        self.assertIn("Geo Permissions", call["error"])

    def test_the_bot_is_told_once_when_the_errand_is_done(self):
        errand = self.approved()

        def done(e):
            e["calls"][0].update(status="done", outcome={"status": "quoted"})
            e["calls"][1].update(status="done", outcome={"status": "no_answer"})

        self.store.update_errand(errand["id"], done)
        self.step()
        (profile, line, key), = self.deliver.told
        self.assertEqual(profile, "default")
        self.assertEqual(line, f'[Phone errand finished · "Septic pump-out quotes" · {errand["id"]} · 1 quoted, 1 no answer] Read the details with phone_call_report.')
        self.assertEqual(self.deliver.pushes[0][1]["body"], "Septic pump-out quotes: 1 quoted, 1 no answer")
        self.assertTrue(self.store.errand(errand["id"])["notified"])
        self.step()
        self.assertEqual(len(self.deliver.told), 1)

    def test_a_busy_chat_is_tried_again_later(self):
        self.deliver.result = "busy"
        errand = self.approved()
        self.store.update_errand(errand["id"], lambda e: [c.update(status="done", outcome={"status": "quoted"}) for c in e["calls"]])
        self.step()
        self.step()
        self.assertEqual(len(self.deliver.told), 1)
        self.t[0] += V.NOTIFY_RETRY_S + 1
        self.step()
        self.assertEqual(len(self.deliver.told), 2)
        self.assertEqual(len(self.deliver.pushes), 1)


class Stream(Base):
    def setUp(self):
        super().setUp()
        signal.signal(signal.SIGALRM, self._stuck)  # a stuck socket fails the test instead of hanging the run
        signal.alarm(20)
        self.addCleanup(signal.alarm, 0)

    def _stuck(self, *_):
        raise TimeoutError("the call's sockets got stuck")

    def live_call(self):
        errand = self.approved()
        call = errand["calls"][0]
        self.store.update_call(errand["id"], call["id"], lambda c: c.update(status="dialing", sid="CA1"))
        return errand, call

    def url(self, token):
        return f"wss://node.example.ts.net:8443/twilio/stream/{token}"

    def test_an_unsigned_stream_is_refused(self):
        errand, call = self.live_call()
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect(f"/twilio/stream/{call['token']}", headers={"x-twilio-signature": "bad"}) as ws:
                ws.receive_json()
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect("/twilio/stream/not-a-token", headers={"x-twilio-signature": T.signature("tok", self.url("not-a-token"), {})}) as ws:
                ws.receive_json()

    def test_the_opening_line_from_settings_is_what_the_call_says(self):
        self.store.update_settings({"opener": "Hi, I'm calling for {name}."})
        errand, call = self.live_call()
        token = call["token"]
        with self.client.websocket_connect(f"/twilio/stream/{token}", headers={"x-twilio-signature": T.signature("tok", self.url(token), {})}) as ws:
            ws.send_json({"event": "connected"})
            ws.send_json({"event": "start", "start": {"streamSid": "MZ1", "callSid": "CA1"}})
            for payload, seconds in ((LOUD, 0.5), (QUIET, 0.9)):
                for _ in range(int(seconds / 0.02)):
                    self.t[0] += 0.02
                    ws.send_json({"event": "media", "media": {"payload": payload}})
            ws.receive_json()
            ws.send_json({"event": "stop"})
            try:
                ws.receive_json()
            except WebSocketDisconnect:
                pass

        forced = [e for e in self.grok.sent if e["type"] == "conversation.item.create" and e["item"]["type"] == "force_message"]
        self.assertTrue(forced[0]["item"]["content"][0]["text"].startswith("Hi, I'm calling for Alex. "), forced[0])

    def test_an_answered_call_end_to_end(self):
        errand, call = self.live_call()
        token = call["token"]
        with self.client.websocket_connect(f"/twilio/stream/{token}", headers={"x-twilio-signature": T.signature("tok", self.url(token), {})}) as ws:
            ws.send_json({"event": "connected"})
            ws.send_json({"event": "start", "start": {"streamSid": "MZ1", "callSid": "CA1"}})
            for payload, seconds in ((LOUD, 0.5), (QUIET, 0.9)):
                for _ in range(int(seconds / 0.02)):
                    self.t[0] += 0.02
                    ws.send_json({"event": "media", "media": {"payload": payload}})
            message = ws.receive_json()
            self.assertEqual(message, {"event": "media", "streamSid": "MZ1", "media": {"payload": "OPENING"}})
            ws.send_json({"event": "stop"})
            try:
                ws.receive_json()
            except WebSocketDisconnect:
                pass

        session = self.grok.sent[0]
        self.assertEqual((session["type"], session["session"]["voice"], session["session"]["audio"]["input"]["format"]),
                         ("session.update", "ara", {"type": "audio/pcmu"}))
        self.assertNotIn("County records", session["session"]["instructions"])
        self.assertEqual(self.connected, "xai-token")
        appended = [e for e in self.grok.sent if e["type"] == "input_audio_buffer.append"]
        self.assertGreater(len(appended), 40)
        forced = [e for e in self.grok.sent if e["type"] == "conversation.item.create" and e["item"]["type"] == "force_message"]
        self.assertTrue(forced[0]["item"]["content"][0]["text"].startswith("Hi, this is an AI assistant calling for Alex."))
        after = self.store.errand(errand["id"])["calls"][0]
        self.assertEqual(after["status"], "done")
        self.assertEqual(after["outcome"]["status"], "call_back")
        self.assertEqual(after["transcript"][0]["who"], "assistant")
        self.assertIsNotNone(self.store.known_number("+12025550101", 14))
        self.assertTrue(self.grok.closed)



class Asking(Base):
    """The service's half of ask_owner: the record the app answers, the push with buttons, the wait and its log."""

    def asking(self, ask, during=None, token=""):
        errand = self.approved()
        call = errand["calls"][0]
        self.store.update_call(errand["id"], call["id"], lambda c: c.update(status="live"))

        async def run():
            task = asyncio.ensure_future(self.service._ask(errand, call, ask, token))
            await asyncio.sleep(0.05)
            if during:
                await during(errand, call, task)
            try:
                return await task
            except asyncio.CancelledError:
                return "cancelled"

        return errand, call, asyncio.run(run())

    def answer_with(self, change):
        async def during(errand, call, task):
            self.store.update_call(errand["id"], call["id"], lambda c: c["question"].update(change))
        return during

    def test_an_approval_is_pushed_with_allow_deny_and_join(self):
        errand, call, reply = self.asking({"kind": "approval", "question": "8 pm instead of 7 pm", "choices": []}, self.answer_with({"approved": True}))
        self.assertEqual(reply, {"approved": True})
        kind, push = self.deliver.pushes[-1]
        self.assertEqual(kind, "call_question")
        self.assertEqual(push["body"], "Pine Valley Septic: 8 pm instead of 7 pm. Allow it?")
        self.assertEqual(push["url"], f"hermesbot://bot/default/errand-call?errand={errand['id']}&call={call['id']}")
        self.assertEqual([a["title"] for a in push["actions"]], ["Allow", "Deny", "Join the call"])
        self.assertIn("&approved=1", push["actions"][0]["url"])
        self.assertNotIn("+1202", json.dumps(push))  # no number in a push
        after = self.store.errand(errand["id"])["calls"][0]
        self.assertIsNone(after["question"])
        self.assertEqual(after["answers"][-1] | {"asked": 0}, {"question": "8 pm instead of 7 pm", "answer": "Allowed", "asked": 0, "kind": "approval", "how": "allowed"})
        self.assertEqual([e["what"] for e in after["log"]], ["asked_ok", "allowed"])

    def test_a_question_with_choices(self):
        errand, call, reply = self.asking({"kind": "question", "question": "Which day?", "choices": ["Monday", "Tuesday", "Friday"]},
                                          self.answer_with({"answer": "Tuesday"}))
        self.assertEqual(reply, {"text": "Tuesday"})
        titles = [a["title"] for a in self.deliver.pushes[-1][1]["actions"]]
        self.assertEqual(titles, ["Monday", "Tuesday", "Join the call"])

    def test_no_answer_in_time(self):
        async def late(errand, call, task):
            self.t[0] += V.QUESTION_WAIT_S + 1
        errand, call, reply = self.asking({"kind": "question", "question": "Which day?", "choices": []}, late)
        self.assertIsNone(reply)
        self.assertEqual(self.store.errand(errand["id"])["calls"][0]["answers"][-1]["how"], "no_answer")

    def test_joining_settles_it(self):
        async def join(errand, call, task):
            task.cancel()
            await asyncio.sleep(0)
        errand, call, reply = self.asking({"kind": "question", "question": "Which day?", "choices": []}, join)
        self.assertEqual(reply, "cancelled")
        after = self.store.errand(errand["id"])["calls"][0]
        self.assertEqual((after["answers"][-1]["how"], after["log"][-1]["what"], after["question"]), ("joined", "joined", None))

    def test_a_listener_waits_longer(self):
        self.service.live["tok"] = V.Live("e", "c", brain=None, twilio=None, grok=None, listener=object())
        async def check(errand, call, task):
            question = self.store.errand(errand["id"])["calls"][0]["question"]
            self.assertEqual(question["deadline"] - question["asked"], V.LISTEN_WAIT_S)
            task.cancel()
            await asyncio.sleep(0)
        self.asking({"kind": "question", "question": "Which day?", "choices": []}, check, token="tok")


class Listening(Stream):
    """The person listens in, takes the call over and hands it back, over the call service's own socket."""

    def opened(self, ws):
        ws.send_json({"event": "connected"})
        ws.send_json({"event": "start", "start": {"streamSid": "MZ1", "callSid": "CA1"}})
        for payload, seconds in ((LOUD, 0.5), (QUIET, 0.9)):
            for _ in range(int(seconds / 0.02)):
                self.t[0] += 0.02
                ws.send_json({"event": "media", "media": {"payload": payload}})
        self.assertEqual(ws.receive_json()["media"]["payload"], "OPENING")
        self.assertEqual(ws.receive_json()["mark"]["name"], "opening")
        ws.send_json({"event": "mark", "mark": {"name": "opening"}})

    def until(self, ws, kind, key="type"):
        while True:
            message = ws.receive_json()
            if message.get(key) == kind:
                return message

    def test_a_ticket_opens_one_socket_once(self):
        errand, call = self.live_call()
        ticket = self.store.mint_listen(errand["id"], call["id"])
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect("/app/listen", subprotocols=["hermes-phone.listen", "ticket.wrong"]) as ws:
                ws.receive_json()
        self.t[0] += S.LISTEN_TICKET_S + 1
        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect("/app/listen", subprotocols=["hermes-phone.listen", f"ticket.{ticket}"]) as ws:
                ws.receive_json()
        with self.assertRaises(WebSocketDisconnect):  # expired, and now spent too
            with self.client.websocket_connect("/app/listen", subprotocols=["hermes-phone.listen", f"ticket.{ticket}"]) as ws:
                ws.receive_json()

    def test_listen_take_over_and_hand_back(self):
        errand, call = self.live_call()
        token = call["token"]
        with self.client.websocket_connect(f"/twilio/stream/{token}", headers={"x-twilio-signature": T.signature("tok", self.url(token), {})}) as tw:
            self.opened(tw)
            ticket = self.store.mint_listen(errand["id"], call["id"])
            with self.client.websocket_connect("/app/listen", subprotocols=["hermes-phone.listen", f"ticket.{ticket}"]) as app:
                hello = app.receive_json()
                self.assertEqual((hello["type"], hello["mode"]), ("hello", "voice"))
                for _ in range(5):
                    tw.send_json({"event": "media", "media": {"payload": QUIET}})
                heard = self.until(app, "audio")
                self.assertEqual(len(base64.b64decode(heard["pcmu"])), 800)

                app.send_json({"type": "take_over"})
                self.assertEqual(tw.receive_json(), {"event": "clear", "streamSid": "MZ1"})
                self.assertEqual(tw.receive_json()["media"]["payload"], "OPENING")  # the fake voice says its scripted line
                self.assertEqual(tw.receive_json()["mark"]["name"], "handover")
                tw.send_json({"event": "mark", "mark": {"name": "handover"}})
                while self.until(app, "state")["mode"] != "owner":
                    pass

                app.send_json({"type": "audio", "pcmu": LOUD})
                self.assertEqual(self.until(tw, "media", key="event")["media"]["payload"], LOUD)

                app.send_json({"type": "hand_back", "note": "Thursday works."})
                self.assertEqual(self.until(app, "state")["mode"], "voice")
                app.send_json({"type": "leave"})
            tw.send_json({"event": "stop"})
            try:
                tw.receive_json()
            except WebSocketDisconnect:
                pass

        forced = [e["item"]["content"][0]["text"] for e in self.grok.sent if e["type"] == "conversation.item.create" and e["item"]["type"] == "force_message"]
        self.assertIn("One moment, I'll put Alex on the line.", forced)
        instructions = [e["session"]["instructions"] for e in self.grok.sent if e["type"] == "session.update" and "instructions" in e["session"]]
        self.assertIn("noted: Thursday works.", instructions[-1])
        log = [e["what"] for e in self.store.errand(errand["id"])["calls"][0]["log"]]
        self.assertEqual(log, ["listen", "took_over", "handed_back", "stopped_listening"])

    def test_a_dropped_listener_hands_the_call_back(self):
        errand, call = self.live_call()
        token = call["token"]
        with self.client.websocket_connect(f"/twilio/stream/{token}", headers={"x-twilio-signature": T.signature("tok", self.url(token), {})}) as tw:
            self.opened(tw)
            ticket = self.store.mint_listen(errand["id"], call["id"])
            with self.client.websocket_connect("/app/listen", subprotocols=["hermes-phone.listen", f"ticket.{ticket}"]) as app:
                app.receive_json()
                app.send_json({"type": "take_over"})
                while tw.receive_json().get("event") != "mark":
                    pass
                tw.send_json({"event": "mark", "mark": {"name": "handover"}})
                while self.until(app, "state")["mode"] != "owner":
                    pass
            self.t[0] += V.DROP_HAND_BACK_S + 1
            for _ in range(40):  # the ticks notice
                if any(e["what"] == "dropped" for e in self.store.errand(errand["id"])["calls"][0]["log"]):
                    break
                tw.send_json({"event": "media", "media": {"payload": QUIET}})
                time.sleep(0.05)
            tw.send_json({"event": "stop"})
            try:
                tw.receive_json()
            except WebSocketDisconnect:
                pass
        log = [e["what"] for e in self.store.errand(errand["id"])["calls"][0]["log"]]
        self.assertIn("dropped", log)
        instructions = [e["session"]["instructions"] for e in self.grok.sent if e["type"] == "session.update" and "instructions" in e["session"]]
        self.assertIn("The line to Alex dropped", instructions[-1])

if __name__ == "__main__":
    unittest.main()
