"""The call brain against fake ports and a fake clock: every rule about who hears what, and when the call ends."""

import asyncio
import base64
import json
import unittest

from helpers import CALLBACK, SETTINGS, septic

import brain as M  # noqa: E402
import brief as B  # noqa: E402
import ulaw  # noqa: E402

QUIET = base64.b64encode(bytes([ulaw.SILENCE]) * 160).decode()
LOUD = ulaw.frames(ulaw.tone("5"))[0]


LOOP = asyncio.new_event_loop()


def tearDownModule():
    LOOP.close()

class Harness:
    def __init__(self, ask_owner=True, answer="Yes, the lid is exposed.", max_minutes=20, hold_minutes=15, transcripts=True, opener=""):
        brief, _ = B.check(septic(), SETTINGS, CALLBACK)
        self.t = [1000.0]
        self.grok, self.twilio, self.saved, self.hung, self.asked = [], [], [], [], []
        self.answer = answer

        async def grok(event):
            self.grok.append(event)

        async def twilio(event):
            self.twilio.append(event)

        async def hang_up():
            self.hung.append(True)

        async def ask(question):
            self.asked.append(question)
            return self.answer

        ports = M.Ports(grok=grok, twilio=twilio, hang_up=hang_up, ask_owner=ask, save=self.saved.append, clock=lambda: self.t[0])
        setup = M.Setup(brief=brief, call=brief["calls"][0], name="Alex", callback=CALLBACK, ask_owner=ask_owner,
                        transcripts=transcripts, opener=opener, max_minutes=max_minutes, hold_minutes=hold_minutes, never_share=SETTINGS["never_share"])
        self.brain = M.Brain(setup, ports)

    def run(self, coro):
        return LOOP.run_until_complete(coro)

    def twilio_event(self, event, **extra):
        self.run(self.brain.on_twilio({"event": event, **extra}))

    def grok_event(self, kind, **extra):
        self.run(self.brain.on_grok({"type": kind, **extra}))

    def media(self, payload, seconds):
        for _ in range(int(seconds / 0.02)):
            self.t[0] += 0.02
            self.twilio_event("media", media={"payload": payload})

    def start(self):
        self.twilio_event("start", start={"streamSid": "MZ1", "callSid": "CA1"})

    def types(self):
        return [e["type"] for e in self.grok]

    def forced(self):
        return [e["item"]["content"][0]["text"] for e in self.grok if e["type"] == "conversation.item.create" and e["item"]["type"] == "force_message"]

    def vad(self):
        return [e["session"]["turn_detection"] for e in self.grok if e["type"] == "session.update" and "turn_detection" in e["session"]]

    def tool(self, name, call_id="call1", **args):
        self.grok_event("response.function_call_arguments.done", name=name, call_id=call_id, arguments=json.dumps(args))

    def outputs(self):
        return [json.loads(e["item"]["output"]) for e in self.grok if e["type"] == "conversation.item.create" and e["item"]["type"] == "function_call_output"]

    def sent_media(self):
        return [e for e in self.twilio if e["event"] == "media"]

    def opened(self):
        """A person said hello, the opening played, and the call is live."""
        self.start()
        self.media(LOUD, 0.6)
        self.media(QUIET, 0.8)
        self.grok_event("response.created", response={"id": "r0"})
        self.grok_event("response.done")
        self.twilio_event("mark", mark={"name": "opening"})


class Greeting(unittest.TestCase):
    def test_a_short_hello_then_quiet_gets_the_opening_word_for_word(self):
        h = Harness()
        h.start()
        self.assertEqual(h.vad(), [])
        h.media(LOUD, 0.6)
        self.assertEqual(h.forced(), [])
        h.media(QUIET, 0.8)
        self.assertEqual(len(h.forced()), 1)
        self.assertTrue(h.forced()[0].startswith("Hi, this is an AI assistant calling for Alex. This call is transcribed."))
        force = next(e for e in h.grok if e["type"] == "conversation.item.create")
        self.assertFalse(force["item"]["interruptible"])
        self.assertNotIn("input_audio_buffer.commit", h.types())  # on xAI a commit would start a reply of its own
        h.grok_event("response.created", response={"id": "r0"})
        h.grok_event("response.output_audio.delta", delta="AAA")
        self.assertEqual(h.sent_media()[-1]["media"]["payload"], "AAA")
        h.grok_event("response.done")
        self.assertEqual(h.twilio[-1], {"event": "mark", "streamSid": "MZ1", "mark": {"name": "opening"}})
        h.grok_event("input_audio_buffer.speech_started")  # an "uh-huh" while it plays can't cut the disclosure short
        self.assertNotIn("clear", [e["event"] for e in h.twilio])
        self.assertEqual(h.vad(), [])  # generated is not heard: live once Twilio has played it all
        h.twilio_event("mark", mark={"name": "opening"})
        self.assertEqual(h.types()[-2:], ["input_audio_buffer.clear", "session.update"])
        self.assertEqual(h.vad(), [{"type": "server_vad"}])

    def test_the_persons_own_opening_line_is_spoken_word_for_word(self):
        h = Harness(opener="Hi, I'm calling for {name}.")
        h.start()
        h.media(LOUD, 0.6)
        h.media(QUIET, 0.8)
        self.assertEqual(len(h.forced()), 1)
        self.assertTrue(h.forced()[0].startswith("Hi, I'm calling for Alex. I'm calling"), h.forced()[0])

    def test_the_call_goes_on_if_the_opening_mark_never_comes_back(self):
        h = Harness()
        h.start()
        h.media(LOUD, 0.6)
        h.media(QUIET, 0.8)
        h.grok_event("response.created", response={"id": "r0"})
        h.grok_event("response.done")
        h.t[0] += M.OPENING_PLAY_MAX_S + 0.1
        h.run(h.brain.tick())
        self.assertEqual(h.vad(), [{"type": "server_vad"}])

    def test_nobody_says_anything_for_three_seconds(self):
        h = Harness()
        h.start()
        h.t[0] += 3.1
        h.run(h.brain.tick())
        self.assertEqual(len(h.forced()), 1)

    def test_the_opening_is_never_said_twice(self):
        h = Harness()
        h.start()
        h.media(QUIET, 3.2)
        h.run(h.brain.tick())
        self.assertEqual(len(h.forced()), 1)

    def test_a_long_greeting_is_a_menu_the_voice_handles(self):
        h = Harness()
        h.start()
        h.media(LOUD, 4.2)
        self.assertEqual(h.forced(), [])
        self.assertEqual(h.vad(), [{"type": "server_vad"}])

    def test_the_voice_talking_before_the_disclosure_gets_the_opening_instead(self):
        h = Harness()
        h.start()
        h.media(LOUD, 4.2)
        h.brain.state.human = True
        h.grok_event("response.created", response={"id": "r1"})
        h.grok_event("response.output_audio.delta", delta="VOICE")
        self.assertNotIn("VOICE", [m["media"]["payload"] for m in h.sent_media()])
        self.assertIn("response.cancel", h.types())
        self.assertEqual(len(h.forced()), 1)


class Menus(unittest.TestCase):
    def test_keys_go_to_twilio_as_tones(self):
        h = Harness()
        h.start()
        h.media(LOUD, 4.2)
        h.tool("press_keys", digits="1")
        tones = h.sent_media()
        self.assertEqual(h.twilio[0]["event"], "clear")
        self.assertEqual(ulaw.detect(ulaw.decode(b"".join(base64.b64decode(m["media"]["payload"]) for m in tones))), "1")
        self.assertEqual(h.outputs()[-1], {"ok": True, "pressed": "1"})
        self.assertNotIn("response.create", h.types())

    def test_keys_are_rate_limited_and_filtered(self):
        h = Harness()
        h.start()
        h.tool("press_keys", digits="1; DROP 2")
        self.assertEqual(h.outputs()[-1]["pressed"], "12")
        h.tool("press_keys", call_id="c2", digits="3")
        self.assertFalse(h.outputs()[-1]["ok"])
        h.t[0] += 2.1
        h.tool("press_keys", call_id="c3", digits="1234567890")
        self.assertEqual(h.outputs()[-1]["pressed"], "123456")


class Hold(unittest.TestCase):
    def on_hold(self):
        h = Harness()
        h.start()
        h.media(LOUD, 4.2)
        h.grok_event("response.created", response={"id": "r1"})
        h.tool("stay_silent", reason="hold")
        return h

    def test_entering_hold_silences_everything(self):
        h = self.on_hold()
        self.assertEqual(h.vad()[-1], None)
        self.assertIn("response.cancel", h.types())
        self.assertEqual(h.twilio[-1]["event"], "clear")
        self.assertTrue(h.saved[-1]["hold"])
        h.grok_event("response.done")
        h.grok_event("response.created", response={"id": "r2"})
        h.grok_event("response.output_audio.delta", delta="(Silence — holding)")
        self.assertEqual(h.sent_media(), [])
        self.assertEqual(h.types().count("response.cancel"), 2)

    def heard(self, h, text, item="i1"):
        """xAI's transcription events carry the whole call's transcript so far."""
        h.grok_event("conversation.item.input_audio_transcription.completed", item_id=item, transcript=text)

    RECORDING = "Please hold. Thank you for your patience. Please continue to hold."

    def test_hold_clears_the_buffer_and_never_commits(self):
        h = self.on_hold()
        h.t[0] += M.HOLD_CLEAR_S + 0.1
        h.run(h.brain.tick())
        self.assertEqual(h.types()[-1], "input_audio_buffer.clear")
        self.assertNotIn("input_audio_buffer.commit", h.types())

    def test_a_recording_is_judged_as_a_whole_burst(self):
        h = self.on_hold()
        self.heard(h, "For billing, press 2. Please hold. Thank you for your patience.")
        self.heard(h, "For billing, press 2. " + self.RECORDING)
        self.assertTrue(h.brain.state.hold)  # "thank you for your patience" alone would have sounded live
        h.t[0] += M.HOLD_BURST_GAP_S + 0.1
        h.run(h.brain.tick())
        self.assertTrue(h.brain.state.hold)
        self.assertTrue(h.brain.state.heard_on_hold[-1].endswith(self.RECORDING))
        self.assertEqual(h.sent_media(), [])

    def test_a_live_person_ends_hold_and_gets_the_opening(self):
        h = self.on_hold()
        self.heard(h, "For billing, press 2. " + self.RECORDING)
        h.t[0] += M.HOLD_BURST_GAP_S + 0.1
        h.run(h.brain.tick())
        self.heard(h, "For billing, press 2. " + self.RECORDING + " Hi, thanks for holding, this is Dana.", item="i2")
        self.assertTrue(h.brain.state.hold)
        self.heard(h, "For billing, press 2. " + self.RECORDING + " Hi, thanks for holding, this is Dana. How can I help you?", item="i2")
        self.assertFalse(h.brain.state.hold)
        self.assertEqual(len(h.forced()), 1)
        self.assertEqual(h.brain.state.transcript[-1]["text"], "Hi, thanks for holding, this is Dana. How can I help you?")
        self.heard(h, "For billing, press 2. " + self.RECORDING + " Hi, thanks for holding, this is Dana. How can I help you today?", item="i2")
        self.assertEqual(h.brain.state.transcript[-1]["text"], "Hi, thanks for holding, this is Dana. How can I help you today?")

    def test_after_the_opening_a_live_person_gets_a_response(self):
        h = Harness()
        h.opened()
        h.grok_event("response.created", response={"id": "r1"})
        h.tool("stay_silent", reason="hold")
        h.grok_event("response.done")
        self.heard(h, "Okay, sorry about the wait. What can I do for you?", item="i9")
        self.assertEqual(h.vad()[-1], {"type": "server_vad"})
        # A fresh buffer (xAI ignores a commit of audio begun with detection off), their words as text, then the reply.
        self.assertEqual(h.types()[-4:], ["input_audio_buffer.clear", "session.update", "conversation.item.create", "response.create"])
        said = [e for e in h.grok if e["type"] == "conversation.item.create" and e["item"]["type"] == "message"][-1]
        self.assertEqual(said["item"]["content"][0]["text"], "Okay, sorry about the wait. What can I do for you?")
        self.assertNotIn("input_audio_buffer.commit", h.types())
        self.assertEqual(len(h.forced()), 1)

    def test_hold_has_a_cap(self):
        h = Harness(hold_minutes=1)
        h.start()
        h.tool("stay_silent", reason="hold")
        h.t[0] += 61
        h.run(h.brain.tick())
        self.assertEqual(h.saved[-1]["outcome"]["status"], "call_back")
        self.assertEqual(h.hung, [True])

    def test_sounds_live(self):
        heard = ["Please hold, your call is important to us. A representative will be with you shortly."]
        self.assertFalse(M.sounds_live("Yeah.", heard))
        self.assertFalse(M.sounds_live("Your call is important to us. A representative will be with you shortly.", heard))
        self.assertFalse(M.sounds_live("For billing, press 2.", []))
        self.assertTrue(M.sounds_live("Thanks for holding, this is Dana.", heard))
        self.assertTrue(M.sounds_live("Pine Valley Septic, how can I help you?", heard))


class Tools(unittest.TestCase):
    def test_the_outcome_is_cleaned_and_the_voice_goes_on(self):
        h = Harness()
        h.opened()
        h.grok_event("response.created", response={"id": "r1"})
        h.tool("record_outcome", status="quoted", base_price="$385 including disposal", earliest="x" * 500, home_address="12 Orchard Lane",
               they_asked="Email them a photo of the lid", dont_call_again="yes")
        outcome = h.saved[-1]["outcome"]
        self.assertEqual(outcome["status"], "quoted")
        self.assertEqual(len(outcome["earliest"]), M.FIELD_LIMIT)
        self.assertNotIn("home_address", outcome)
        self.assertNotIn("dont_call_again", outcome)
        self.assertEqual(outcome["they_asked"], "Email them a photo of the lid")
        self.assertNotIn("response.create", h.types())
        h.grok_event("response.done")
        self.assertEqual(h.types()[-1], "response.create")

    def test_the_status_has_to_match_what_was_written_down(self):
        h = Harness()
        h.opened()
        h.tool("record_outcome", status="quoted", notes="They wanted a deposit first.")
        self.assertEqual(h.brain.state.outcome["status"], "call_back")
        h.tool("record_outcome", status="booked", earliest="Tuesday")  # a quote call can't book
        self.assertEqual(h.brain.state.outcome["status"], "call_back")
        h.tool("record_outcome", status="quoted", base_price="$385")
        self.assertEqual(h.brain.state.outcome["status"], "quoted")

    def test_an_unknown_status_becomes_done(self):
        h = Harness()
        h.opened()
        h.tool("record_outcome", status="paid")
        self.assertEqual(h.saved[-1]["outcome"]["status"], "done")

    def test_ask_owner_waits_on_the_person_and_the_answer_joins_the_instructions(self):
        h = Harness()
        h.opened()
        h.tool("ask_owner", question="Is the lid exposed?")
        h.run(asyncio.sleep(0))
        h.run(asyncio.sleep(0))
        self.assertEqual(h.asked, ["Is the lid exposed?"])
        self.assertEqual(h.outputs()[-1], {"answer": "Yes, the lid is exposed."})
        update = [e for e in h.grok if e["type"] == "session.update" and "instructions" in e["session"]][-1]
        self.assertIn("Alex answered during this call, to “Is the lid exposed?”: Yes, the lid is exposed.", update["session"]["instructions"])
        self.assertEqual(h.types()[-1], "response.create")

    def test_no_answer_and_the_limit(self):
        h = Harness(answer=None)
        h.opened()
        for i in range(4):
            h.tool("ask_owner", call_id=f"q{i}", question=f"Question {i}?")
            h.run(asyncio.sleep(0))
            h.run(asyncio.sleep(0))
        self.assertEqual(len(h.asked), 3)
        self.assertIsNone(h.outputs()[-1]["answer"])
        self.assertIn("get back to them", h.outputs()[-1]["note"])

    def test_a_caller_cant_fish_through_the_person(self):
        h = Harness()
        h.opened()
        for question in ("What card should we put on file?", "Is $450 all right?", "Should I give them the gate PIN?"):
            h.tool("ask_owner", question=question)
            self.assertIsNone(h.outputs()[-1]["answer"])
            self.assertIn("never gives that", h.outputs()[-1]["note"])
        self.assertEqual(h.asked, [])
        h.tool("ask_owner", question="Is the lid exposed?")  # the blocked ones didn't use up the three questions
        h.run(asyncio.sleep(0))
        h.run(asyncio.sleep(0))
        self.assertEqual(h.asked, ["Is the lid exposed?"])

    def test_an_answer_that_cant_go_on_a_call_is_held_back(self):
        for answer in ("Sure, it's 4111 1111 1111 1111", "Up to $400 is fine", "Tell them 12 orchard lane pinecrest"):
            with self.subTest(answer=answer):
                h = Harness(answer=answer)
                h.opened()
                h.tool("ask_owner", question="Is the lid exposed?")
                h.run(asyncio.sleep(0))
                h.run(asyncio.sleep(0))
                self.assertIsNone(h.outputs()[-1]["answer"])
                self.assertEqual(h.brain.state.answers, [])
                self.assertNotIn("4111", json.dumps(h.grok))
                self.assertNotIn("400", json.dumps(h.grok))

    def test_ask_owner_off(self):
        h = Harness(ask_owner=False)
        h.opened()
        h.tool("ask_owner", question="Is the lid exposed?")
        self.assertEqual(h.asked, [])
        self.assertIsNone(h.outputs()[-1]["answer"])

    def test_being_asked_not_to_call_is_honored_without_the_voice(self):
        h = Harness()
        h.opened()
        h.grok_event("conversation.item.input_audio_transcription.completed", item_id="i1", transcript="Stop calling us.")
        self.assertTrue(h.brain.state.outcome["dont_call_again"])
        self.assertNotEqual(h.types()[-1], "response.create")  # voice detection's own reply may be on its way
        h.t[0] += M.NUDGE_S + 0.1
        h.run(h.brain.tick())
        self.assertEqual(h.types()[-1], "response.create")  # it wasn't: a turn to apologize
        h.grok_event("response.created", response={"id": "r1"})
        h.grok_event("response.output_audio.delta", delta="SORRY")
        h.tool("record_outcome", status="declined")  # the voice forgot dont_call_again: it stays
        self.assertTrue(h.brain.state.outcome["dont_call_again"])
        h.grok_event("response.done")
        self.assertEqual(h.twilio[-1].get("mark"), {"name": "goodbye"})
        for words in ("We don't call people back on weekends.", "They told us you'd call."):
            self.assertIsNone(M.DO_NOT_CALL.search(words))
        for words in ("Take this number off your list.", "Please don't call here again", "Remove us from your calling list"):
            self.assertIsNotNone(M.DO_NOT_CALL.search(words))

    def test_do_not_call_ends_after_the_goodbye_it_already_said(self):
        h = Harness()
        h.opened()
        h.grok_event("response.created", response={"id": "r1"})
        h.grok_event("response.output_audio.delta", delta="SORRY")
        h.tool("record_outcome", status="declined", dont_call_again=True)
        creates = h.types().count("response.create")
        h.grok_event("response.done")
        self.assertEqual(h.types().count("response.create"), creates)  # no second goodbye
        self.assertEqual(h.twilio[-1].get("mark"), {"name": "goodbye"})

    def test_do_not_call_before_a_word_gets_one_reply_first(self):
        h = Harness()
        h.opened()
        h.grok_event("response.created", response={"id": "r1"})
        h.tool("record_outcome", status="declined", dont_call_again=True)
        h.grok_event("response.done")
        self.assertEqual(h.types()[-1], "response.create")
        self.assertNotIn({"name": "goodbye"}, [e.get("mark") for e in h.twilio])
        h.grok_event("response.created", response={"id": "r2"})
        h.grok_event("response.output_audio.delta", delta="SORRY")
        h.grok_event("response.done")
        self.assertEqual(h.twilio[-1].get("mark"), {"name": "goodbye"})

    def test_end_call_hangs_up_after_the_goodbye_has_played(self):
        h = Harness()
        h.opened()
        h.grok_event("response.created", response={"id": "r1"})
        h.grok_event("response.output_audio.delta", delta="BYE")
        h.tool("end_call")
        self.assertEqual(h.hung, [])
        h.grok_event("response.done")
        self.assertEqual(h.twilio[-1], {"event": "mark", "streamSid": "MZ1", "mark": {"name": "goodbye"}})
        self.assertEqual(h.hung, [])
        h.twilio_event("mark", mark={"name": "goodbye"})
        self.assertEqual(h.hung, [])  # a moment for their goodbye
        h.t[0] += M.GOODBYE_GRACE_S - 0.1
        h.run(h.brain.tick())
        self.assertEqual(h.hung, [])
        h.t[0] += 0.2
        h.run(h.brain.tick())
        self.assertEqual(h.hung, [True])
        self.assertEqual(h.saved[-1]["status"], "done")
        self.assertEqual(h.saved[-1]["outcome"]["status"], "call_back")

    def test_a_goodbye_that_never_echoes_still_hangs_up(self):
        h = Harness()
        h.opened()
        h.tool("end_call")
        h.t[0] += M.GOODBYE_TIMEOUT_S + 0.1
        h.run(h.brain.tick())
        self.assertEqual(h.hung, [True])


class Endings(unittest.TestCase):
    def test_voicemail_leaves_the_scripted_message(self):
        h = Harness()
        h.start()
        h.media(LOUD, 4.2)
        h.tool("stay_silent", reason="voicemail_greeting")
        h.run(h.brain.on_amd("machine_end_beep"))
        self.assertIn("Alex will call you back.", h.forced()[-1])
        h.grok_event("response.created", response={"id": "vm"})
        h.grok_event("response.output_audio.delta", delta="VM")
        self.assertEqual(h.sent_media()[-1]["media"]["payload"], "VM")
        h.grok_event("response.done")
        h.twilio_event("mark", mark={"name": "goodbye"})
        self.assertEqual(h.saved[-1]["outcome"], {"status": "no_answer", "notes": "Left a voicemail."})
        self.assertEqual(h.saved[-1]["answered_by"], "machine")
        self.assertEqual(h.hung, [True])

    def test_the_time_cap_warns_then_hangs_up(self):
        h = Harness(max_minutes=2)
        h.opened()
        h.t[0] += 61
        h.run(h.brain.tick())
        update = [e for e in h.grok if e["type"] == "session.update" and "instructions" in e["session"]][-1]
        self.assertIn("About a minute is left", update["session"]["instructions"])
        h.t[0] += 60
        h.run(h.brain.tick())
        self.assertEqual(h.hung, [True])
        self.assertEqual(h.saved[-1]["outcome"]["notes"], "The call reached its time limit.")

    def test_they_hang_up(self):
        h = Harness()
        h.opened()
        h.twilio_event("stop")
        self.assertEqual(h.hung, [])
        self.assertEqual(h.saved[-1]["outcome"]["notes"], "They hung up before an outcome was recorded.")

    def test_nobody_answered(self):
        h = Harness()
        h.start()
        h.twilio_event("stop")
        self.assertEqual(h.saved[-1]["outcome"], {"status": "no_answer"})


class Transcript(unittest.TestCase):
    def test_refined_transcripts_keep_one_line(self):
        h = Harness()
        h.opened()
        h.grok_event("conversation.item.input_audio_transcription.completed", item_id="i1", transcript="Pump out is three")
        h.grok_event("conversation.item.input_audio_transcription.completed", item_id="i1", transcript="Pump-out is $385.")
        h.grok_event("response.output_audio_transcript.done", transcript="Thanks, and is disposal included?")
        # The next item's transcript carries the whole call so far; its line holds only what it added.
        h.grok_event("conversation.item.input_audio_transcription.completed", item_id="i2", transcript="Pump-out is $385. Yes, it's included.")
        lines = [(l["who"], l["text"]) for l in h.brain.state.transcript]
        self.assertEqual(lines, [("them", "Pump-out is $385."), ("assistant", "Thanks, and is disposal included?"), ("them", "Yes, it's included.")])

    def test_gained(self):
        before = "Thank you for calling Pine Valley septic. For service".split()
        self.assertEqual(M.gained(before, "Thank you for calling Pine Valley Septic. For service and pricing, press 1.".split()),
                         ["and", "pricing,", "press", "1."])
        looped = "Please hold. Please hold.".split()
        self.assertEqual(M.gained("Music. Please hold.".split(), "Music. Please hold. Please hold.".split()), looped[2:])
        self.assertEqual(M.gained(before, before), [])
        self.assertEqual(M.gained(before, "Something else entirely".split()), ["Something", "else", "entirely"])
        # A fresh transcript that shares a stray word with the old one is still all new.
        context = "Please hold, your call is important to us. How can I help you?".split()
        fresh = "Sure, our base pump out is 385, and disposal is included.".split()
        self.assertEqual(M.gained(context, fresh), fresh)
        # Audio that was cleared drops out, and the transcript carries on from before it.
        self.assertEqual(M.gained(context[:8] + "Hi, thanks for holding. This".split(), context), "How can I help you?".split())

    def test_no_transcript_saved_when_off(self):
        h = Harness(transcripts=False)
        h.opened()
        h.grok_event("conversation.item.input_audio_transcription.completed", item_id="i1", transcript="Hello")
        h.twilio_event("stop")
        self.assertNotIn("transcript", h.saved[-1])



class Screeners(unittest.TestCase):
    GOOGLE_VOICE = "Hello, please state your name after the tone and Google Voice will try to connect you."

    def heard(self, h, text, item="i1"):
        h.grok_event("conversation.item.input_audio_transcription.completed", item_id=item, transcript=text)

    def screened(self, prompt=GOOGLE_VOICE):
        """A screener's prompt, long enough for the greeting to hand over to the voice, then its words."""
        h = Harness()
        h.start()
        h.media(LOUD, 4.2)
        self.heard(h, prompt)
        return h

    def answered(self, h):
        h.grok_event("response.created", response={"id": "s1"})
        h.grok_event("response.output_audio.delta", delta="NAME")
        h.grok_event("response.done")
        h.twilio_event("mark", mark={"name": "screen"})

    def test_google_voice_gets_the_opening_after_its_tone_not_a_voicemail(self):
        h = self.screened()
        self.assertEqual((h.brain.state.screening, h.brain.state.phase), ("listening", "screening"))
        self.assertEqual(h.vad()[-1], None)
        h.t[0] += M.SCREEN_PAUSE_S + 0.1
        h.run(h.brain.tick())
        self.assertEqual(h.forced(), [])  # it mentioned a tone: wait for it
        h.run(h.brain.on_amd("machine_end_beep"))
        self.assertEqual(len(h.forced()), 1)
        self.assertTrue(h.forced()[0].startswith("Hi, this is an AI assistant calling for Alex."))
        self.assertNotIn("will call you back", h.forced()[0])  # not the voicemail
        self.answered(h)
        self.assertEqual(h.sent_media()[-1]["media"]["payload"], "NAME")
        self.assertTrue(h.brain.state.hold)
        self.assertEqual(h.brain.state.screening, "waiting")
        self.assertFalse(h.brain.state.machine)

    def test_the_person_picking_up_gets_the_opening_themselves(self):
        h = self.screened()
        h.run(h.brain.on_amd("machine_end_beep"))
        self.answered(h)
        self.heard(h, self.GOOGLE_VOICE + " Hello?", item="i2")
        self.assertEqual(len(h.forced()), 2)
        self.assertEqual(h.forced()[1], h.forced()[0])
        self.assertFalse(h.brain.state.hold)
        self.assertEqual(h.brain.state.screening, "done")

    def test_a_voicemail_greeting_after_the_screener_gets_the_message(self):
        h = self.screened()
        h.run(h.brain.on_amd("machine_end_beep"))
        self.answered(h)
        self.heard(h, self.GOOGLE_VOICE + " The person you are calling is not available. Please leave a message after the tone.", item="i2")
        h.t[0] += M.HOLD_BURST_GAP_S + 0.1
        h.run(h.brain.tick())
        self.assertIn("Alex will call you back.", h.forced()[-1])
        self.assertTrue(h.brain.state.machine)

    def test_a_screener_without_a_tone_is_answered_once_it_stops(self):
        h = self.screened("Hi, the person you're calling is using a screening service. Go ahead and say your name and why you're calling.")
        h.t[0] += M.SCREEN_PAUSE_S - 0.2
        h.run(h.brain.tick())
        self.assertEqual(h.forced(), [])
        h.t[0] += 0.4
        h.run(h.brain.tick())
        self.assertEqual(len(h.forced()), 1)

    def test_its_connecting_message_is_not_a_person(self):
        h = self.screened()
        h.run(h.brain.on_amd("machine_end_beep"))
        self.answered(h)
        self.heard(h, self.GOOGLE_VOICE + " Please wait while Google Voice tries to connect you.", item="i2")
        h.t[0] += M.HOLD_BURST_GAP_S + 0.1
        h.run(h.brain.tick())
        self.assertEqual(len(h.forced()), 1)
        self.assertTrue(h.brain.state.hold)

    def test_nobody_after_a_minute_ends_the_call(self):
        h = self.screened()
        h.run(h.brain.on_amd("machine_end_beep"))
        self.answered(h)
        h.t[0] += M.SCREEN_WAIT_S + 0.1
        h.run(h.brain.tick())
        self.assertEqual(h.hung, [True])
        self.assertEqual(h.saved[-1]["outcome"]["status"], "no_answer")
        self.assertIn("screener", h.saved[-1]["outcome"]["notes"])

    def test_the_voice_talking_to_a_screener_first_waits_for_the_tone(self):
        h = Harness()
        h.start()
        h.media(LOUD, 4.2)  # a long greeting: the voice takes over, with voice detection on
        h.brain.state.transcript.append({"who": "them", "text": self.GOOGLE_VOICE, "t": 2.0})
        h.grok_event("response.created", response={"id": "r1"})
        h.grok_event("response.output_audio.delta", delta="SPOKE")
        self.assertEqual(h.sent_media(), [])
        self.assertEqual(h.forced(), [])
        self.assertEqual(h.brain.state.screening, "listening")

    def test_a_person_asking_who_is_calling_is_not_a_screener(self):
        h = Harness()
        h.start()
        h.media(LOUD, 0.6)
        self.heard(h, "Pine Valley Septic, who's calling?")
        h.media(QUIET, 0.8)
        self.assertEqual(h.brain.state.screening, "")
        self.assertEqual(len(h.forced()), 1)


if __name__ == "__main__":
    unittest.main()
