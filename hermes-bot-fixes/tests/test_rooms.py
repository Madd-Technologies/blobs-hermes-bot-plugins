import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import rooms  # noqa: E402


def make_startup(calls):
    """Stands in for gateway.run_startup: the mixin whose static method starts the gateway's room worker."""
    module = types.ModuleType("gateway.run_startup")

    class GatewayStartupMixin:
        @staticmethod
        def _start_hosted_room_worker_sync():
            calls.append("started")

            return "service"

    module.GatewayStartupMixin = GatewayStartupMixin

    return module


def make_groups(running):
    """Stands in for tui_gateway.methods_groups: one process-owned room service, running or not."""
    module = types.ModuleType("tui_gateway.methods_groups")
    state = {"service": "service" if running else None, "stops": 0}
    module.get_hosted_room_service = lambda: state["service"]

    def stop_hosted_room_service(*, timeout=5.0):
        state["stops"] += 1
        state["service"] = None

        return True

    module.stop_hosted_room_service = stop_hosted_room_service

    return module, state


class GatewayOutOfRooms(unittest.TestCase):
    def test_the_gateway_starts_no_room_worker(self):
        calls = []
        startup = make_startup(calls)
        self.assertTrue(rooms.patch_startup(startup))
        self.assertIsNone(startup.GatewayStartupMixin._start_hosted_room_worker_sync())
        self.assertEqual(calls, [])

    def test_a_worker_already_running_in_the_gateway_is_stopped(self):
        startup = make_startup([])
        groups, state = make_groups(running=True)
        rooms.patch_startup(startup)

        with mock.patch.dict(sys.modules, {"tui_gateway.methods_groups": groups}):
            startup.GatewayStartupMixin._start_hosted_room_worker_sync()
            startup.GatewayStartupMixin._start_hosted_room_worker_sync()

        self.assertEqual(state["stops"], 1, "stopped once; after that there is nothing to stop")

    def test_applying_twice_changes_nothing_more(self):
        startup = make_startup([])
        rooms.patch_startup(startup)
        first = startup.GatewayStartupMixin.__dict__["_start_hosted_room_worker_sync"]
        self.assertTrue(rooms.patch_startup(startup))
        self.assertIs(startup.GatewayStartupMixin.__dict__["_start_hosted_room_worker_sync"], first)

    def test_an_unexpected_shape_is_left_alone(self):
        module = types.ModuleType("gateway.run_startup")

        class GatewayStartupMixin:
            def _start_hosted_room_worker_sync(self, profile):
                return "service"

        module.GatewayStartupMixin = GatewayStartupMixin
        self.assertFalse(rooms.patch_startup(module))
        self.assertEqual(GatewayStartupMixin()._start_hosted_room_worker_sync("x"), "service")
        self.assertFalse(rooms.patch_startup(types.ModuleType("gateway.run_startup")))

    def test_only_where_the_gateway_runs(self):
        # The dashboard never loads the gateway's startup code; the fix does not load it either.
        with mock.patch.dict(sys.modules, {}, clear=False):
            sys.modules.pop("gateway.run_startup", None)
            self.assertFalse(rooms.apply())
            self.assertNotIn("gateway.run_startup", sys.modules)

        calls = []
        startup = make_startup(calls)

        with mock.patch.dict(sys.modules, {"gateway.run_startup": startup}):
            self.assertTrue(rooms.apply())

        self.assertIsNone(startup.GatewayStartupMixin._start_hosted_room_worker_sync())

    def test_the_switch_leaves_the_gateway_worker_alone(self):
        calls = []
        startup = make_startup(calls)

        with mock.patch.dict(sys.modules, {"gateway.run_startup": startup}), mock.patch.dict(os.environ, {rooms.SWITCH: "on"}):
            self.assertFalse(rooms.apply())

        self.assertEqual(startup.GatewayStartupMixin._start_hosted_room_worker_sync(), "service")


if __name__ == "__main__":
    unittest.main()
