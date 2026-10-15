import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import ROOT, Phone

from fastapi import FastAPI
from fastapi.testclient import TestClient


def load_api(folder):
    """Load plugin_api.py the way `hermes serve` does: by path, under its own module name."""
    name = "hermes_dashboard_plugin_hermes-bot-mobile-test"
    spec = importlib.util.spec_from_file_location(name, ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    store_module, notify_module = module._modules()
    patch = mock.patch.object(store_module, "default_dir", lambda: Path(folder))

    return module, store_module, notify_module, patch


class Api(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.module, self.store_module, self.notify, patch = load_api(self.dir.name)
        patch.start()
        self.addCleanup(patch.stop)
        app = FastAPI()
        app.include_router(self.module.router, prefix="/api/plugins/hermes-bot-mobile")
        self.client = TestClient(app)
        self.base = "/api/plugins/hermes-bot-mobile"
        self.phone = Phone()

    def tearDown(self):
        self.dir.cleanup()

        for key in [k for k in sys.modules if k.startswith("_hermes_bot_mobile_package") or k.startswith("hermes_dashboard_plugin_hermes-bot-mobile-test")]:
            del sys.modules[key]

    def register(self, phone=None, name="Test phone"):
        phone = phone or self.phone

        return self.client.post(f"{self.base}/devices", json={"endpoint": phone.endpoint, "p256dh": phone.p256dh, "auth": phone.auth, "name": name})

    def test_registers_a_device_and_never_hands_the_keys_back(self):
        response = self.register()
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual((body["name"], body["host"]), ("Test phone", "ntfy.example"))
        self.assertNotIn("p256dh", body)
        self.assertNotIn("auth", body)
        self.assertNotIn("endpoint", body)

        status = self.client.get(f"{self.base}/status").json()
        self.assertEqual([d["id"] for d in status["devices"]], [body["id"]])
        self.assertNotIn("p256dh", status["devices"][0])

    def test_status_carries_the_servers_vapid_key_for_the_phone_to_register_with(self):
        status = self.client.get(f"{self.base}/status").json()
        self.assertRegex(status["vapid"], r"^[A-Za-z0-9_-]{87}$")
        self.assertEqual(status["vapid"], self.client.get(f"{self.base}/status").json()["vapid"])

    def test_rejects_bad_keys_with_a_reason(self):
        response = self.client.post(f"{self.base}/devices", json={"endpoint": self.phone.endpoint, "p256dh": "AAAA", "auth": self.phone.auth})
        self.assertEqual(response.status_code, 422)
        self.assertIn("P-256", response.json()["detail"])

    def test_unregisters(self):
        identity = self.register().json()["id"]
        self.assertEqual(self.client.delete(f"{self.base}/devices/{identity}").status_code, 200)
        self.assertEqual(self.client.delete(f"{self.base}/devices/{identity}").status_code, 404)
        self.assertEqual(self.client.get(f"{self.base}/status").json()["devices"], [])

    def test_a_registered_phone_says_what_it_has_on_screen(self):
        identity = self.register().json()["id"]
        response = self.client.put(f"{self.base}/presence", json={"device": identity, "foreground": True, "sessions": ["s1", "live-1"]})
        self.assertEqual(response.status_code, 200)
        seen = self.store_module.DeviceStore().presence()
        self.assertEqual((seen["foreground"], seen["sessions"]), (True, {"s1", "live-1"}))

        self.client.put(f"{self.base}/presence", json={"device": identity, "foreground": False})
        self.assertEqual(self.store_module.DeviceStore().presence()["foreground"], False)

        self.assertEqual(self.client.put(f"{self.base}/presence", json={"device": "nope", "foreground": True}).status_code, 404)

    def test_levels_round_trip(self):
        response = self.client.put(f"{self.base}/settings", json={"default": "needs_you", "bots": {"juniper": "off"}})
        self.assertEqual(response.json(), {"default": "needs_you", "bots": {"juniper": "off"}})
        self.assertEqual(self.client.get(f"{self.base}/status").json()["settings"]["bots"], {"juniper": "off"})
        self.assertEqual(self.client.put(f"{self.base}/settings", json={"default": "loud"}).status_code, 422)

    def test_a_test_push_goes_through_the_real_encryption_and_reports_the_answer(self):
        identity = self.register().json()["id"]
        sent = []

        def fake_post(url, headers, content, timeout):
            sent.append((url, content))

            return 201

        with mock.patch.object(self.notify, "http_post", fake_post):
            response = self.client.post(f"{self.base}/test", json={})

        self.assertEqual(response.json()["results"], [{"device": identity, "ok": True, "status": 201, "error": None, "gone": False}])
        ((url, content),) = sent
        self.assertEqual(url, self.phone.endpoint)
        message = json.loads(self.phone.read(content))
        self.assertEqual((message["title"], message["body"], message["url"]), ("Hermes", "Notifications are working", "hermesbot://inbox"))

    def test_testing_an_unknown_device_is_a_404(self):
        self.assertEqual(self.client.post(f"{self.base}/test", json={"device": "nope"}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
