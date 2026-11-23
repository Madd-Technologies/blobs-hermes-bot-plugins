import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import apns  # noqa: E402


class ApnsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.folder = Path(self.dir.name)
        self.valid_token = "a" * 64

    def tearDown(self):
        self.dir.cleanup()

    def test_validate_device_token_and_env(self):
        apns.validate_apns_device(self.valid_token, "production")
        apns.validate_apns_device(self.valid_token.upper(), "sandbox")

        for bad_tok, bad_env in [("short", "production"), ("z" * 64, "production"), (self.valid_token, "dev")]:
            with self.assertRaises(ValueError):
                apns.validate_apns_device(bad_tok, bad_env)

    def test_load_config_from_file_and_env_override(self):
        self.assertFalse(apns.is_configured(self.folder))
        (self.folder / "apns.json").write_text(
            json.dumps({"key_id": "KEY1", "team_id": "TEAM1", "bundle_id": "com.example.blobs", "private_key": "PEMDATA"}),
            encoding="utf-8",
        )
        cfg = apns.load_config(self.folder)
        self.assertEqual(cfg["key_id"], "KEY1")
        self.assertTrue(apns.is_configured(self.folder))

        with mock.patch.dict("os.environ", {"HERMES_APNS_SANDBOX": "true"}):
            cfg2 = apns.load_config(self.folder)
            self.assertTrue(cfg2["sandbox"])

    def test_build_payload_and_send_with_mock_poster(self):
        payload = {
            "id": 42,
            "title": "Juniper",
            "body": "Needs your OK: run script",
            "url": "hermesbot://bot/juniper",
            "actions": [{"title": "Allow once", "url": "hermesbot://approve?id=r1&profile=juniper&choice=once&kind=session"}],
            "approval": {"id": "r1", "profile": "juniper", "kind": "session", "session": "s1"},
        }
        aps = apns.build_aps_payload("approval", payload)
        self.assertEqual(aps["aps"]["category"], "APPROVAL")
        self.assertEqual(aps["aps"]["interruption-level"], "time-sensitive")
        self.assertEqual(aps["aps"]["mutable-content"], 1)
        self.assertEqual(aps["approval"]["id"], "r1")

        calls = []

        def fake_poster(url, headers, body, timeout):
            calls.append((url, headers, json.loads(body.decode("utf-8"))))
            return 200, ""

        cfg = {"key_id": "K", "team_id": "T", "bundle_id": "com.example.app", "private_key": "PEM", "sandbox": False}
        status, err = apns.send_apns(self.valid_token, "approval", payload, cfg, environment="sandbox", poster=fake_poster, signer=lambda *_: "jwt.tok")
        self.assertEqual((status, err), (200, None))
        self.assertTrue(calls[0][0].startswith("https://api.sandbox.push.apple.com/3/device/"))
        self.assertEqual(calls[0][1]["authorization"], "bearer jwt.tok")
        self.assertEqual(calls[0][1]["apns-topic"], "com.example.app")
        self.assertEqual(calls[0][2]["aps"]["category"], "APPROVAL")

        # 410 BadDeviceToken detected cleanly
        s2, e2 = apns.send_apns(
            self.valid_token,
            "reply",
            {"id": 1, "title": "J", "body": "Hi"},
            cfg,
            poster=lambda *_: (400, json.dumps({"reason": "BadDeviceToken"})),
            signer=lambda *_: "jwt.tok",
        )
        self.assertEqual((s2, e2), (410, "BadDeviceToken"))


if __name__ == "__main__":
    unittest.main()
