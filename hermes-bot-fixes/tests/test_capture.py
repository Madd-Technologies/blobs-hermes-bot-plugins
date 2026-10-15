import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import capture  # noqa: E402


def window(app, title="", wid=1):
    return {"app_name": app, "title": title, "pid": wid, "window_id": wid, "off_screen": False, "z_index": 0}


class Panels(unittest.TestCase):
    def setUp(self):
        def choose(windows, *, app_requested, exact_target=False):
            return windows[0]

        self.module = SimpleNamespace(_select_capture_target=choose)
        self.assertTrue(capture.patch_target_choice(self.module))
        self.platform = mock.patch.object(capture.sys, "platform", "linux")
        self.platform.start()

    def tearDown(self):
        self.platform.stop()

    def test_an_unnamed_capture_skips_the_panels(self):
        windows = [window("xfce4-panel", "xfce4-panel", 1), window("Google-chrome", "Example Domain - Google Chrome", 2)]
        self.assertEqual(self.module._select_capture_target(windows, app_requested=False)["window_id"], 2)

    def test_a_named_or_exact_capture_is_left_alone(self):
        windows = [window("xfce4-panel", "", 1), window("chrome", "", 2)]
        self.assertEqual(self.module._select_capture_target(windows, app_requested=True)["window_id"], 1)
        self.assertEqual(self.module._select_capture_target(windows, app_requested=False, exact_target=True)["window_id"], 1)

    def test_only_panels_falls_back_to_them(self):
        windows = [window("xfce4-panel", "", 1)]
        self.assertEqual(self.module._select_capture_target(windows, app_requested=False)["window_id"], 1)

    def test_patching_twice_wraps_once(self):
        wrapped = self.module._select_capture_target
        self.assertTrue(capture.patch_target_choice(self.module))
        self.assertIs(self.module._select_capture_target, wrapped)

    def test_a_different_function_is_not_touched(self):
        other = SimpleNamespace(_select_capture_target=lambda items, flag: items[0])
        original = other._select_capture_target
        self.assertFalse(capture.patch_target_choice(other))
        self.assertIs(other._select_capture_target, original)


class Settle(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def follow(backend, res, do_capture, fence=lambda: None, session_id=None):
            self.calls.append(("capture", do_capture))
            return "captured"

        self.slept = []
        self.module = SimpleNamespace(_maybe_follow_capture=follow)
        self.assertTrue(capture.patch_settle(self.module, sleep=self.slept.append))

    def test_the_screenshot_after_an_action_waits_first(self):
        self.assertEqual(self.module._maybe_follow_capture(None, SimpleNamespace(ok=True), True, session_id="s"), "captured")
        self.assertEqual(self.slept, [capture.SETTLE_SECONDS])

    def test_no_wait_without_a_screenshot_or_after_a_failed_action(self):
        self.module._maybe_follow_capture(None, SimpleNamespace(ok=True), False)
        self.module._maybe_follow_capture(None, SimpleNamespace(ok=False), True)
        self.assertEqual(self.slept, [])
        self.assertEqual(len(self.calls), 2)

    def test_the_wait_can_be_tuned_and_is_capped(self):
        with mock.patch.dict(capture.os.environ, {"HERMES_BOT_FIXES_SETTLE_SECONDS": "1.5"}):
            self.assertEqual(capture.settle_seconds(), 1.5)

        with mock.patch.dict(capture.os.environ, {"HERMES_BOT_FIXES_SETTLE_SECONDS": "60"}):
            self.assertEqual(capture.settle_seconds(), capture.SETTLE_MAX)

        with mock.patch.dict(capture.os.environ, {"HERMES_BOT_FIXES_SETTLE_SECONDS": "soon"}):
            self.assertEqual(capture.settle_seconds(), capture.SETTLE_SECONDS)

    def test_a_different_function_is_not_touched(self):
        other = SimpleNamespace(_maybe_follow_capture=lambda result: result)
        self.assertFalse(capture.patch_settle(other))


if __name__ == "__main__":
    unittest.main()
