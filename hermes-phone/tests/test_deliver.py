import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import deliver  # noqa: E402


def authority(*, ready=True, store=True, receipt=None, raises=None):
    """Stands in for Hermes 0.22's tools.bot_live_delivery: the profile's gateway admits deliveries into Bot Chat."""
    module = types.ModuleType("bot_live_delivery")
    module.sent = []

    def find_canonical_live_owner(home):
        if not ready:
            raise ValueError("profile authority is not ready")

        return {"profile_home": str(home), "session_id": "", "canonical": True, "lease_id": "l1", "live_session_id": ""} if store else None

    def deliver_to_live_owner(home, owner, message, *, delivery_id=None, author=None):
        if raises:
            raise raises

        module.sent.append({"home": home, "message": message, "id": delivery_id, "author": author})

        return receipt if receipt is not None else {"status": "queued", "delivery_id": delivery_id, "admission_id": "a1"}

    module.find_canonical_live_owner = find_canonical_live_owner
    module.deliver_to_live_owner = deliver_to_live_owner

    return module


class Delivering(unittest.TestCase):
    """On Hermes 0.22 a finished call's result goes to the bot through its gateway, which queues it in Bot Chat."""

    home = Path("/home/orion/.hermes/profiles/harbor")
    key = "a" * 64

    def test_the_gateway_admits_it_into_bot_chat(self):
        live = authority()
        self.assertEqual(deliver._tell_authority(live, self.home, "Call to Dana Ruiz finished.", self.key), "delivered")
        self.assertEqual(live.sent[0]["id"], self.key)
        self.assertEqual(live.sent[0]["author"], deliver.AUTHOR)

    def test_a_gateway_that_is_not_up_yet_means_try_later(self):
        self.assertEqual(deliver._tell_authority(authority(ready=False), self.home, "x", self.key), "busy")
        self.assertEqual(deliver._tell_authority(authority(raises=ValueError("dial failed")), self.home, "x", self.key), "busy")

    def test_an_unadmitted_receipt_is_tried_again_later(self):
        self.assertEqual(deliver._tell_authority(authority(receipt={"refused": True, "admitted": False}), self.home, "x", self.key), "busy")

    def test_no_store_or_an_unexpected_error_fails(self):
        self.assertEqual(deliver._tell_authority(authority(store=False), self.home, "x", self.key), "failed")
        self.assertEqual(deliver._tell_authority(authority(raises=RuntimeError("boom")), self.home, "x", self.key), "failed")

    def test_tell_bot_takes_the_gateway_path_on_hermes_0_22(self):
        live = authority()
        tools = types.ModuleType("tools")
        tools.bot_live_delivery = live
        saved = {name: sys.modules.get(name) for name in ("tools", "tools.bot_live_delivery")}
        sys.modules["tools"], sys.modules["tools.bot_live_delivery"] = tools, live

        try:
            self.assertEqual(deliver.tell_bot(Path("/home/orion/.hermes"), "harbor", "Done.", self.key), "delivered")
        finally:
            for name, module in saved.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module

        self.assertEqual(live.sent[0]["home"], Path("/home/orion/.hermes/profiles/harbor"))


if __name__ == "__main__":
    unittest.main()
