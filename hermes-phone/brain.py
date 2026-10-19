"""One phone call between Twilio and Grok: what the service does with each event from either side.

Transport-free (the service and the tests give it ports), so every rule here is tested without a network:

- **Greeting:** voice detection starts off. The service listens to who answered. A short greeting then quiet gets the
  opening line (the AI disclosure, spoken word for word). A long one is a menu or recording, and the voice handles it
  with its tools; the opening is said the first time a live person is on the line.
- **Hold mode** (`stay_silent`): Grok's audio is dropped before Twilio and any response is cancelled, so a slip is never
  heard. Voice detection goes off and nothing is committed (on xAI a commit starts a reply); the buffer is cleared every
  few seconds instead. xAI keeps transcribing as it goes, one running transcript of the whole call, so the words it gains
  are judged a burst at a time (what was said between pauses): one that sounds like a live person talking to us ends it.
- **Do not call:** asked not to be called again, in so many words, the number goes on the do-not-call list and the call ends
  after the voice's goodbye, whether or not the voice records it.
- **Voicemail:** when answering-machine detection reports the end of the greeting, the scripted message is left word for
  word and the call ends.
- **Tools:** keypad tones go straight to Twilio as audio. The outcome is checked and saved. `ask_owner` waits on the
  person, never the bot, and the answer joins the instructions. `end_call` hangs up once the goodbye has played.
- **Limits:** a warning a minute before the call's cap, then a hang-up. A hold cap. At most 3 questions to the person.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Awaitable, Callable, Dict, List, Optional

try:
    from . import brief as B
    from . import persona as P
    from . import ulaw
except ImportError:  # the tests import the modules flat
    import brief as B  # type: ignore[no-redef]
    import persona as P  # type: ignore[no-redef]
    import ulaw  # type: ignore[no-redef]

#: Mean absolute sample of a 20 ms frame above which someone is talking (μ-law silence decodes to ~0).
SPEECH_LEVEL = 450
GREETING_QUIET_S = 0.7
GREETING_NOBODY_S = 3.0
#: A greeting longer than this is a menu, a recording or a voicemail greeting, not a person saying hello.
GREETING_LONG_S = 4.0
HOLD_CLEAR_S = 8.0
#: After they ask not to be called again: how long to wait for voice detection's reply before asking for one.
NUDGE_S = 1.5
#: The longest the opening line can take to play; past it the call goes on without Twilio's mark.
OPENING_PLAY_MAX_S = 20.0
TRANSCRIPT_WINDOW = 80
#: On hold: a pause this long ends a burst of words; a burst this long is judged without waiting for one.
HOLD_BURST_GAP_S = 2.0
HOLD_BURST_WORDS = 40
KEYS_EVERY_S = 2.0
MAX_KEYS = 6
MAX_QUESTIONS = 3
FIELD_LIMIT = 300
GOODBYE_TIMEOUT_S = 10.0
SAVE_EVERY_S = 15.0

_RECORDING = re.compile(
    r"(your call is important|please (continue to )?hold|(please )?stay on the line|next available|estimated (wait|hold)|"
    r"wait time|press \d|visit (us at|our website)|calls? (may|will) be (recorded|monitored)|in the queue|"
    r"remain on the line|for (english|spanish|quality)|we appreciate your patience|all (of )?our (agents|representatives|staff)|"
    r"(agents|representatives) are (currently )?(busy|helping|assisting)|in the order (it was|they were|calls are) received|"
    r"(office|business) hours|monday through friday|your call will be|if you know your party)", re.I)
_LIVE = re.compile(
    r"(thanks? (you )?for (holding|waiting|your patience)|this is \w+|how (can|may) i help|speaking|sorry (for|about) (the wait|that)|"
    r"are you (still )?there|hello|^hi\b|^hey\b|^(okay|ok|alright|all right)\b|can i help)", re.I)


def _words(text: str) -> List[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def sounds_live(text: str, heard_on_hold: List[str]) -> bool:
    """Whether words heard on hold are a live person talking to us rather than the hold recording or music."""
    words = _words(text)

    if len(words) < 3:
        return False
    for earlier in heard_on_hold:
        old = set(_words(earlier))
        if old and len(old & set(words)) / len(old | set(words)) >= 0.5:
            return False
    if _RECORDING.search(text):
        return False

    return bool(_LIVE.search(text)) or len(words) >= 8


#: The other party asking not to be called again, in their words (the voice is told to honor it too; this doesn't rely on it).
DO_NOT_CALL = re.compile(
    r"\b(stop calling|quit calling|don'?t (ever )?call (us|me|here|this number)|do not call (us|me|here|this number)|"
    r"(take|remove|get) (this|my|our|the) number (off|from)|(take|remove|get) (us|me) (off|from) (your|the|that) (list|calling list)|"
    r"(put|add) (us|me|this number) (on|to) (your|the|a) do[- ]not[- ]call)", re.I)


def gained(before: List[str], now: List[str]) -> List[str]:
    """The words a transcript gained between two versions of it. xAI's usually runs on (it grows at the end, refines its last
    words, and drops words whose audio was cleared), so the new words are what follows the last real match with the earlier
    version, case and punctuation aside. It sometimes starts afresh instead: with no match of a few words, all of it is new
    (a stray shared word like "is" is not a match)."""
    if not before:
        return list(now)
    a = [w.strip(".,!?;:\"").lower() for w in before[-TRANSCRIPT_WINDOW:]]
    start = max(0, min(len(now), len(before)) - TRANSCRIPT_WINDOW - 20)
    b = [w.strip(".,!?;:\"").lower() for w in now[start:]]
    blocks = [block for block in SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks() if block.size >= min(3, len(a))]
    if not blocks:
        return list(now)  # a fresh transcript
    return list(now[start + blocks[-1].b + blocks[-1].size :])


@dataclass
class Ports:
    grok: Callable[[Dict[str, Any]], Awaitable[None]]
    twilio: Callable[[Dict[str, Any]], Awaitable[None]]
    hang_up: Callable[[], Awaitable[None]]
    ask_owner: Callable[[str], Awaitable[Optional[str]]]
    save: Callable[[Dict[str, Any]], None]
    clock: Callable[[], float]
    log: Callable[[str], None] = lambda message: None


@dataclass
class Setup:
    brief: Dict[str, Any]
    call: Dict[str, Any]
    name: str
    voice: str = "eve"
    callback: str = ""
    ask_owner: bool = True
    transcripts: bool = True
    max_minutes: int = 20
    hold_minutes: int = 15
    never_share: List[Dict[str, str]] = field(default_factory=list)  # never sent to the voice; only screens what crosses


@dataclass
class State:
    phase: str = "connecting"  # connecting, greeting, opening, live, voicemail, ending, ended
    stream: str = ""
    started: float = 0.0
    greeting_heard: float = 0.0  # seconds of audio, counted from the frames (frames can arrive in bursts)
    greeting_speech: float = 0.0
    greeting_quiet: float = 0.0
    opening_said: bool = False
    opening_sent: float = 0.0
    vad: bool = False
    hold: bool = False
    hold_since: float = 0.0
    last_clear: float = 0.0
    hold_pending: str = ""  # the burst of words heard on hold since the last pause
    hold_heard_at: float = 0.0
    no_more_calls: bool = False  # they asked not to be called again
    responses: int = 0
    nudge_at: float = 0.0
    nudge_after: int = 0
    heard_on_hold: List[str] = field(default_factory=list)
    response: Optional[str] = None
    response_gated: bool = False
    force_pending: Optional[str] = None  # "opening" or "voicemail" while a scripted line plays
    keys_at: float = -1e9
    questions: int = 0
    answers: List[Dict[str, str]] = field(default_factory=list)
    warned: bool = False
    end_after_response: bool = False
    goodbye_check: bool = False  # asked not to be called again: end after a goodbye, giving it one if it hadn't spoken yet
    response_spoke: bool = False
    respond_after: bool = False
    ending_since: Optional[float] = None
    human: bool = False
    machine: bool = False
    outcome: Optional[Dict[str, Any]] = None
    heard_item: str = ""
    heard_base: List[str] = field(default_factory=list)  # the running transcript when this item began
    heard_last: List[str] = field(default_factory=list)  # the running transcript as last heard
    transcript: List[Dict[str, Any]] = field(default_factory=list)
    saved_at: float = 0.0


class Brain:
    def __init__(self, setup: Setup, ports: Ports):
        self.setup = setup
        self.ports = ports
        self.state = State()
        self._tasks: List[asyncio.Task] = []

    # helpers

    def _now(self) -> float:
        return self.ports.clock()

    def _elapsed(self) -> float:
        return self._now() - self.state.started if self.state.started else 0.0

    async def _grok(self, event: Dict[str, Any]) -> None:
        await self.ports.grok(event)

    async def _twilio(self, event: str, **extra: Any) -> None:
        if self.state.stream:
            await self.ports.twilio({"event": event, "streamSid": self.state.stream, **extra})

    async def _vad(self, on: bool) -> None:
        if self.state.vad != on:
            self.state.vad = on
            await self._grok({"type": "session.update", "session": {"turn_detection": {"type": "server_vad"} if on else None}})

    async def _instructions(self, now: str = "") -> None:
        text = P.instructions(self.setup.brief, self.setup.call, self.setup.name, self.setup.ask_owner, self.state.answers, now,
                              transcribed=self.setup.transcripts)
        await self._grok({"type": "session.update", "session": {"instructions": text}})

    async def _say(self, kind: str, text: str) -> None:
        """A scripted line, word for word, that the other party can't talk over."""
        self.state.force_pending = kind
        await self._grok({"type": "conversation.item.create", "item": {"type": "force_message", "role": "assistant", "interruptible": False,
                                                                       "content": [{"type": "output_text", "text": text}]}})

    async def _output(self, call_id: str, value: Dict[str, Any], respond: bool = False) -> None:
        await self._grok({"type": "conversation.item.create", "item": {"type": "function_call_output", "call_id": call_id, "output": json.dumps(value)}})
        if respond:
            if self.state.response:  # one response at a time: ask again once this one is done
                self.state.respond_after = True
            else:
                await self._grok({"type": "response.create"})

    def _note(self, who: str, text: str) -> None:
        text = text.strip()
        if text:
            self.state.transcript.append({"who": who, "text": text[:1000], "t": round(self._elapsed(), 1)})

    def _save(self, **extra: Any) -> None:
        self.state.saved_at = self._now()
        update = {"hold": self.state.hold, "outcome": self.state.outcome, **extra}
        if self.setup.transcripts:
            update["transcript"] = list(self.state.transcript)
        self.ports.save(update)

    # Twilio

    async def on_twilio(self, message: Dict[str, Any]) -> None:
        event = message.get("event")

        if event == "start":
            self.state.stream = message.get("start", {}).get("streamSid") or message.get("streamSid", "")
            self.state.started = self._now()
            self.state.phase = "greeting"
            self._save(status="live")
        elif event == "media":
            payload = message.get("media", {}).get("payload", "")
            if not payload or self.state.phase in ("ending", "ended"):
                return
            await self._grok({"type": "input_audio_buffer.append", "audio": payload})
            if self.state.phase == "greeting":
                await self._greeting(ulaw.level(payload), ulaw.seconds(payload))
        elif event == "mark":
            if message.get("mark", {}).get("name") == "goodbye":
                await self.finish()
            elif message.get("mark", {}).get("name") == "opening":
                await self._opening_played()
        elif event == "stop":
            await self.finish(hung_up_by_them=True)

    async def _greeting(self, level: float, seconds: float) -> None:
        self.state.greeting_heard += seconds
        if level >= SPEECH_LEVEL:
            self.state.greeting_speech += seconds
            self.state.greeting_quiet = 0.0
            if self.state.greeting_speech >= GREETING_LONG_S:
                # A menu, a recording or a voicemail greeting: the voice takes it from here with its tools.
                self.state.phase = "live"
                await self._vad(True)
            return

        self.state.greeting_quiet += seconds
        if (self.state.greeting_speech >= 0.2 and self.state.greeting_quiet >= GREETING_QUIET_S) or (
                self.state.greeting_speech < 0.2 and self.state.greeting_heard >= GREETING_NOBODY_S):
            await self._opening()

    async def _opening(self) -> None:
        if self.state.opening_said or self.state.force_pending or self.state.phase in ("voicemail", "ending", "ended"):
            return
        self.state.phase = "opening"
        self.state.human = True
        # No commit: on xAI it would start a reply of its own next to the scripted line. What they said stays in the buffer
        # and goes in with their next turn.
        await self._say("opening", P.opening(self.setup.brief, self.setup.name, self.setup.transcripts))

    async def _opening_played(self) -> None:
        if self.state.phase != "opening" or not self.state.opening_said:
            return
        self.state.phase = "live"
        # A fresh buffer: audio from before (the greeting, an "uh-huh" during the opening) sat in an item that voice
        # detection, switched on now, would never commit, and it would swallow the start of their reply.
        await self._grok({"type": "input_audio_buffer.clear"})
        await self._vad(True)

    async def on_amd(self, answered_by: str) -> None:
        """Twilio's asynchronous answering-machine detection (with DetectMessageEnd)."""
        if answered_by == "human":
            self.state.human = True
        elif answered_by.startswith("machine_end") and self.state.phase not in ("voicemail", "ending", "ended"):
            self.state.machine = True
            await self._voicemail()
        elif answered_by == "fax":
            self.state.outcome = self.state.outcome or {"status": "wrong_number", "notes": "A fax machine answered."}
            await self.finish()

    async def _voicemail(self) -> None:
        self.state.phase = "voicemail"
        self.state.hold = False
        await self._vad(False)
        if self.state.response:
            await self._grok({"type": "response.cancel"})
        await self._twilio("clear")
        await self._say("voicemail", P.voicemail(self.setup.brief, self.setup.name, self.setup.callback, self.setup.transcripts))

    # Grok

    async def on_grok(self, event: Dict[str, Any]) -> None:
        kind = event.get("type", "")

        if kind == "response.created":
            self.state.response = (event.get("response") or {}).get("id") or "r"
            self.state.response_spoke = False
            self.state.responses += 1
            self.state.response_gated = self.state.hold and not self.state.force_pending
            if self.state.response_gated:
                await self._grok({"type": "response.cancel"})
        elif kind in ("response.output_audio.delta", "response.audio.delta"):
            if self.state.response_gated or self.state.hold or self.state.phase in ("greeting", "ending", "ended"):
                return
            if not self.state.opening_said and not self.state.force_pending:
                # The voice is about to talk to someone before the disclosure: say the opening instead.
                self.state.response_gated = True
                await self._grok({"type": "response.cancel"})
                await self._opening()
                return
            self.state.response_spoke = True
            await self._twilio("media", media={"payload": event.get("delta", "")})
        elif kind in ("response.output_audio_transcript.done", "response.audio_transcript.done"):
            if not self.state.response_gated and not self.state.hold:
                self._note("assistant", str(event.get("transcript") or ""))
        elif kind == "response.done":
            await self._response_done()
        elif kind == "input_audio_buffer.speech_started":
            if self.state.phase == "live" and not self.state.hold:
                await self._twilio("clear")
        elif kind == "conversation.item.input_audio_transcription.completed":
            await self._heard(str(event.get("item_id") or ""), str(event.get("transcript") or ""))
        elif kind == "response.function_call_arguments.done":
            await self._tool(str(event.get("name") or ""), str(event.get("call_id") or ""), str(event.get("arguments") or "{}"))
        elif kind == "error":
            message = str((event.get("error") or {}).get("message") or event.get("message") or "")[:200]
            if not re.search(r"buffer|cancellation failed", message, re.I):  # clearing an empty buffer, cancelling a finished reply
                self.ports.log(f"grok error: {message}")

    async def _response_done(self) -> None:
        self.state.response = None
        forced, self.state.force_pending = self.state.force_pending, None
        spoke, self.state.response_spoke = self.state.response_spoke, False

        if forced == "opening":
            # Generated is not heard: voice detection waits until Twilio has played it all (the mark comes back), so
            # nobody can talk over the disclosure and cut it short.
            self.state.opening_said = True
            self.state.opening_sent = self._now()
            await self._twilio("mark", mark={"name": "opening"})
        elif forced == "voicemail":
            self.state.outcome = self.state.outcome or {"status": "no_answer", "notes": "Left a voicemail."}
            await self._goodbye()
        elif self.state.end_after_response:
            if self.state.goodbye_check and not spoke and self.state.phase == "live":
                self.state.goodbye_check = False
                await self._grok({"type": "response.create"})  # it hadn't said a word: one reply to apologize, then the end
            else:
                await self._goodbye()
        elif self.state.respond_after and not self.state.hold and self.state.phase == "live":
            self.state.respond_after = False
            await self._grok({"type": "response.create"})

        if self._now() - self.state.saved_at >= SAVE_EVERY_S:
            self._save()

    async def _goodbye(self) -> None:
        """Hang up once what was said has finished playing (Twilio echoes the mark)."""
        if self.state.phase in ("ending", "ended"):
            return
        self.state.phase = "ending"
        self.state.ending_since = self._now()
        await self._twilio("mark", mark={"name": "goodbye"})

    async def _heard(self, item_id: str, text: str) -> None:
        """A transcription event. xAI sends the whole call's transcript so far with every one (refining its last words as it
        goes), so what an item said is what the transcript gained since that item began."""
        words = text.split()
        if item_id != self.state.heard_item:
            self.state.heard_item, self.state.heard_base = item_id, self.state.heard_last
        previous, self.state.heard_last = self.state.heard_last, words

        if self.state.hold:
            await self._hold_heard(gained(previous, words), words)
            return
        said = " ".join(gained(self.state.heard_base, words))
        if not said:
            return
        for line in reversed(self.state.transcript):  # the same item, refined: one line
            if line.get("item") == item_id:
                line["text"] = said[:1000]
                if DO_NOT_CALL.search(said):
                    await self._asked_not_to_call()
                return
        self._note("them", said)
        if self.state.transcript and item_id:
            self.state.transcript[-1]["item"] = item_id
        if DO_NOT_CALL.search(said):
            await self._asked_not_to_call()

    async def _hold_heard(self, new: List[str], words: List[str]) -> None:
        """Words heard on hold gather into a burst (what was said between pauses), judged as a whole: a recording often has a
        line that sounds like a person ("thank you for your patience") next to one that gives it away ("please continue to
        hold"). A burst ending in a question is judged at once: that's how a person picks up."""
        if not new:
            return
        self.state.hold_pending = (self.state.hold_pending + " " + " ".join(new)).strip()
        self.state.hold_heard_at = self._now()
        if self.state.hold_pending.endswith("?") or len(_words(self.state.hold_pending)) >= HOLD_BURST_WORDS:
            await self._hold_judge(words)

    async def _hold_judge(self, words: Optional[List[str]] = None) -> None:
        burst, self.state.hold_pending = self.state.hold_pending, ""
        if not sounds_live(burst, self.state.heard_on_hold):
            self.state.heard_on_hold = (self.state.heard_on_hold + [burst])[-12:]
            return
        words = self.state.heard_last if words is None else words
        # This item's line starts at the live words, not at the hold recording before them.
        self.state.heard_base = words[: max(0, len(words) - len(burst.split()))]
        self._note("them", burst)
        if self.state.transcript and self.state.heard_item:
            self.state.transcript[-1]["item"] = self.state.heard_item
        await self._leave_hold(burst)
        if DO_NOT_CALL.search(burst):
            await self._asked_not_to_call()

    async def _asked_not_to_call(self) -> None:
        """Honored whatever the voice does with it: the number goes on the do-not-call list after the call, and the call ends
        once the voice has said goodbye (given a turn to apologize if it hadn't spoken)."""
        if self.state.no_more_calls:
            return
        self.state.no_more_calls = True
        self.state.outcome = {**(self.state.outcome or {"status": "declined"}), "dont_call_again": True}
        self._save()
        if not self.state.end_after_response:
            self.state.end_after_response = self.state.goodbye_check = True
            if not self.state.response:
                # Voice detection is likely starting a reply to the same words; ask for one only if it doesn't.
                self.state.nudge_at, self.state.nudge_after = self._now() + NUDGE_S, self.state.responses

    # hold

    async def _enter_hold(self) -> None:
        if self.state.hold:
            return
        self.state.hold = True
        self.state.hold_since = self.state.last_clear = self._now()
        self.state.heard_on_hold, self.state.hold_pending, self.state.hold_heard_at = [], "", self._now()
        await self._vad(False)
        if self.state.response:
            await self._grok({"type": "response.cancel"})
        await self._twilio("clear")
        self._save()

    async def _leave_hold(self, words: str = "") -> None:
        self.state.hold = False
        self.state.phase = "live"
        self._save()
        if not self.state.opening_said:
            await self._opening()
            return
        # Their audio sits in an item begun while voice detection was off, and xAI silently ignores a commit or a
        # response.create for it once detection is on. So: a fresh buffer, and the voice gets their words as text.
        await self._grok({"type": "input_audio_buffer.clear"})
        await self._vad(True)
        if words:
            await self._grok({"type": "conversation.item.create",
                              "item": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": words}]}})
        await self._grok({"type": "response.create"})

    # tools

    async def _tool(self, name: str, call_id: str, raw: str) -> None:
        try:
            args = json.loads(raw or "{}")
            args = args if isinstance(args, dict) else {}
        except ValueError:
            args = {}

        if name == "press_keys":
            digits = "".join(ch for ch in str(args.get("digits") or "") if ch in ulaw.KEYPAD)[:MAX_KEYS]
            if not digits or self._now() - self.state.keys_at < KEYS_EVERY_S:
                await self._output(call_id, {"ok": False, "error": "No keys pressed: wait for the menu to finish, then try once."})
                return
            self.state.keys_at = self._now()
            await self._twilio("clear")
            for frame in ulaw.keypad(digits):
                await self._twilio("media", media={"payload": frame})
            self._note("assistant", f"[pressed {digits}]")
            await self._output(call_id, {"ok": True, "pressed": digits})
        elif name == "stay_silent":
            await self._output(call_id, {"ok": True, "note": "Stay quiet. You'll be told when a live person is on the line."})
            await self._enter_hold()
        elif name == "record_outcome":
            self.state.outcome = self._clean_outcome(args)
            self._save()
            if self.state.outcome.get("dont_call_again") and not self.state.end_after_response:
                # They asked not to be called again: the goodbye is the last thing said, whether or not end_call comes.
                self.state.end_after_response = self.state.goodbye_check = True
                await self._output(call_id, {"ok": True, "note": "Apologize briefly and say goodbye; the call ends after that."})
                if not self.state.response:
                    self.state.goodbye_check = False
                    await self._grok({"type": "response.create"})
                return
            await self._output(call_id, {"ok": True}, respond=not self.state.end_after_response)
        elif name == "ask_owner":
            question = re.sub(r"\s+", " ", str(args.get("question") or "")).strip()[:200]
            if not self.setup.ask_owner or not question or self.state.questions >= MAX_QUESTIONS:
                await self._output(call_id, {"answer": None, "note": f"Not available. Say {self.setup.name} will get back to them."}, respond=True)
                return
            if B.screen(question, self.setup.never_share):
                # A caller angling for a card, a code or a price through the person: never asked.
                self._note("assistant", f"[not asked: {question}]")
                await self._output(call_id, {"answer": None, "note": f"{self.setup.name} never gives that on a call. Say {self.setup.name} will get back to them."},
                                   respond=True)
                return
            self.state.questions += 1
            self._tasks.append(asyncio.ensure_future(self._ask(call_id, question)))
        elif name == "end_call":
            self.state.end_after_response = True
            await self._output(call_id, {"ok": True})
            if not self.state.response:
                await self._goodbye()
        else:
            await self._output(call_id, {"ok": False, "error": "unknown tool"})

    async def _ask(self, call_id: str, question: str) -> None:
        self._note("assistant", f"[asked {self.setup.name}: {question}]")
        try:
            answer = await self.ports.ask_owner(question)
        except Exception:
            answer = None
        if self.state.phase in ("ending", "ended"):
            return
        if answer:
            answer = re.sub(r"\s+", " ", answer).strip()[:300]
        if answer and B.screen(answer, self.setup.never_share):
            self._note("assistant", f"[{self.setup.name}'s answer held back: it can't go on a call]")
            answer = None
        if answer:
            self.state.answers.append({"question": question, "answer": answer})
            self._note("assistant", f"[{self.setup.name} answered: {answer}]")
            await self._instructions()
            await self._output(call_id, {"answer": answer}, respond=True)
        else:
            await self._output(call_id, {"answer": None, "note": f"No answer. Say {self.setup.name} will get back to them about it."}, respond=True)

    def _clean_outcome(self, args: Dict[str, Any]) -> Dict[str, Any]:
        status = args.get("status") if args.get("status") in P.STATUSES else "done"
        recorded = [f["key"] for f in self.setup.brief["record"]]
        out: Dict[str, Any] = {"status": status}

        for key in recorded + ["open_questions", "they_asked", "notes"]:
            value = re.sub(r"\s+", " ", str(args.get(key) or "")).strip()[:FIELD_LIMIT]
            if value:
                out[key] = value
        # The status is checked against what was written down: no quote without a figure, no booking on a call that may not book.
        if (status == "quoted" and recorded and not any(out.get(k) for k in recorded)) or (status == "booked" and self.setup.brief["mode"] != "book"):
            out["status"] = "call_back"
        if args.get("dont_call_again") is True or self.state.no_more_calls:
            out["dont_call_again"] = True

        return out

    # time

    async def tick(self) -> None:
        if self.state.phase in ("connecting", "ended"):
            return
        now = self._now()
        elapsed = self._elapsed()
        cap = self.setup.max_minutes * 60

        if self.state.phase == "greeting" and now - self.state.started >= GREETING_NOBODY_S and self.state.greeting_speech < 0.2:
            await self._opening()
        if self.state.phase == "opening" and self.state.opening_said and now - self.state.opening_sent >= OPENING_PLAY_MAX_S:
            await self._opening_played()  # Twilio never echoed the mark
        if self.state.nudge_at and now >= self.state.nudge_at:
            self.state.nudge_at = 0.0
            if self.state.responses == self.state.nudge_after and not self.state.response and self.state.phase == "live":
                self.state.goodbye_check = False
                await self._grok({"type": "response.create"})
        if self.state.hold and self.state.hold_pending and now - self.state.hold_heard_at >= HOLD_BURST_GAP_S:
            await self._hold_judge()
        if self.state.hold:
            if not self.state.hold_pending and now - self.state.last_clear >= HOLD_CLEAR_S:
                # Keeps the buffer short. Never a commit, which would start a reply, and never while words are coming in,
                # which could cut a person off.
                self.state.last_clear = now
                await self._grok({"type": "input_audio_buffer.clear"})
            if now - self.state.hold_since >= self.setup.hold_minutes * 60:
                self.state.outcome = self.state.outcome or {"status": "call_back", "notes": "On hold too long; nobody came on the line."}
                await self.finish()
                return
        if not self.state.warned and elapsed >= cap - 60:
            self.state.warned = True
            await self._instructions("About a minute is left: call record_outcome, say goodbye and end the call.")
        if elapsed >= cap:
            self.state.outcome = self.state.outcome or {"status": "call_back", "notes": "The call reached its time limit."}
            await self.finish()
        elif self.state.ending_since is not None and now - self.state.ending_since >= GOODBYE_TIMEOUT_S:
            await self.finish()

    async def finish(self, hung_up_by_them: bool = False) -> None:
        if self.state.phase == "ended":
            return
        self.state.phase = "ended"
        for task in self._tasks:
            task.cancel()
        if not self.state.outcome:
            if self.state.human or self.state.opening_said:
                note = "They hung up before an outcome was recorded." if hung_up_by_them else "The call ended before an outcome was recorded."
                self.state.outcome = {"status": "call_back", "notes": note}
            else:
                self.state.outcome = {"status": "no_answer"}
        self._save(status="done", answered_by="machine" if self.state.machine else ("human" if self.state.human else ""))
        if not hung_up_by_them:
            await self.ports.hang_up()
