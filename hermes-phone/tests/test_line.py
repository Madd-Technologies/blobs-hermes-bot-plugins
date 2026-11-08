"""Mixing a call for the person listening in, and their microphone for the voice."""

import base64
import unittest

import helpers  # noqa: F401
import line as L  # noqa: E402
import ulaw  # noqa: E402

SILENT = bytes([ulaw.SILENCE]) * ulaw.FRAME
TONE = ulaw.tone("5")[: ulaw.FRAME * 10]


def b64(data):
    return base64.b64encode(data).decode()


class Mixing(unittest.TestCase):
    def test_mix_is_a_sum_and_silence_changes_nothing(self):
        frame = TONE[: ulaw.FRAME]
        self.assertEqual(ulaw.decode(ulaw.mix(frame, SILENT)), ulaw.decode(frame))
        loud = ulaw.mix(frame, frame)
        self.assertGreater(abs(ulaw.decode(loud)[5]), abs(ulaw.decode(frame)[5]))
        self.assertEqual(len(ulaw.mix(frame, frame[:40])), ulaw.FRAME)

    def test_the_listener_hears_both_sides_paced_by_their_frames(self):
        line = L.Line()
        line.to_them(b64(TONE))  # a burst of the voice, ahead of playback
        chunks = []
        for _ in range(10):
            to_voice, chunk = line.from_them(b64(SILENT), listening=True)
            self.assertEqual(to_voice, b64(SILENT))  # the voice hears only them
            if chunk:
                chunks.append(base64.b64decode(chunk))
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0] + chunks[1], TONE)  # the voice, frame by frame, as they heard it
        self.assertEqual(len(line.sent), 0)

    def test_a_clear_drops_what_they_will_never_hear(self):
        line = L.Line()
        line.to_them(b64(TONE))
        line.cleared()
        chunk = None
        for _ in range(5):
            _, chunk = line.from_them(b64(SILENT), listening=True)
        self.assertEqual(base64.b64decode(chunk), SILENT * 5)

    def test_not_listening_keeps_nothing(self):
        line = L.Line()
        line.to_them(b64(TONE))
        for _ in range(10):
            self.assertIsNone(line.from_them(b64(SILENT), listening=False)[1])
        self.assertEqual(len(line.sent), 0)
        self.assertEqual(len(line.down), 0)

    def test_the_persons_microphone_goes_into_what_the_voice_hears(self):
        line = L.Line()
        self.assertEqual(line.from_person(b64(TONE[:320])), TONE[:320])
        to_voice, _ = line.from_them(b64(SILENT), listening=True)
        self.assertEqual(base64.b64decode(to_voice), ulaw.mix(SILENT, TONE[:160]))
        line.from_them(b64(SILENT), listening=True)
        self.assertEqual(len(line.mic), 0)
        self.assertEqual(line.from_them(b64(SILENT), listening=True)[0], b64(SILENT))

    def test_backlogs_are_capped(self):
        line = L.Line()
        for _ in range(20):
            line.from_person(b64(TONE))
        self.assertLessEqual(len(line.mic), L.MIC_MAX)
        self.assertEqual(line.from_person("not base64!"), b"")


if __name__ == "__main__":
    unittest.main()
