"""Loads the plugin through Hermes's own plugin manager and checks the computer_use and compaction fixes on Hermes's real code.

Not part of the unit tests (it needs a Hermes checkout). Run it with the Hermes venv, from anywhere:

    ~/.hermes/hermes-agent/venv/bin/python hermes-bot-fixes/tests/integration_hermes.py [path to a Hermes checkout]

It points HERMES_HOME at a throwaway folder, so the real `~/.hermes` is never read or written.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else str(Path.home() / ".hermes" / "hermes-agent"))

home = Path(tempfile.mkdtemp(prefix="hermes-bot-fixes-it-"))
os.environ["HERMES_HOME"] = str(home)
(home / "plugins").mkdir()
shutil.copytree(PLUGIN, home / "plugins" / "hermes-bot-fixes", ignore=shutil.ignore_patterns("tests", "__pycache__"))
(home / "config.yaml").write_text("plugins:\n  enabled:\n    - hermes-bot-fixes\n")

from hermes_cli.plugins import get_plugin_manager  # noqa: E402

manager = get_plugin_manager()
manager.discover_and_load(force=True)
plugin = manager._plugins.get("hermes-bot-fixes")
assert plugin is not None, "Hermes did not load the plugin"
assert getattr(plugin, "error", None) in (None, ""), plugin.error
assert manager._hooks.get("pre_tool_call"), "pre_tool_call not registered"

import tools.computer_use.cua_backend_capture as capture_module  # noqa: E402
import tools.computer_use.tool as tool_module  # noqa: E402

assert getattr(capture_module._select_capture_target, "__hermes_bot_fixes__", False), "panel fix not applied"
assert getattr(tool_module._maybe_follow_capture, "__hermes_bot_fixes__", False), "settle fix not applied"
import agent.context_compressor as compressor_module  # noqa: E402

assert getattr(compressor_module.ContextCompressor._find_inflight_user_task.__func__, "__hermes_bot_fixes__", False), "compaction fix not applied"
assert getattr(compressor_module.ContextCompressor._reappend_inflight_user_task, "__hermes_bot_fixes__", False), "compaction fix not applied"

panel = {"app_name": "xfce4-panel", "title": "xfce4-panel", "pid": 11, "window_id": 0x1200003, "off_screen": False, "z_index": 0}
chrome = {"app_name": "Google-chrome", "title": "Example Domain - Google Chrome", "pid": 22, "window_id": 0x1600004, "off_screen": False, "z_index": 0}

with mock.patch.object(capture_module.sys, "platform", "linux"), mock.patch.object(capture_module, "_linux_x11_active_window_id", lambda: None):
    picked = capture_module._select_capture_target([panel, chrome], app_requested=False)
    named = capture_module._select_capture_target([panel, chrome], app_requested=True)

print("unnamed capture picks:", picked["app_name"])
print("named capture picks:", named["app_name"])
assert picked["window_id"] == chrome["window_id"]
assert named["window_id"] == panel["window_id"]  # a named capture is never second-guessed

# The settle wrapper calls Hermes's real follow-up; a failed action takes no screenshot and so never waits.
from tools.computer_use.backend import ActionResult  # noqa: E402

failed = ActionResult(ok=False, action="click", message="no such element")
slept = []
with mock.patch("time.sleep", slept.append):
    out = tool_module._maybe_follow_capture(SimpleNamespace(), failed, True)
print("failed action, waited:", slept)
assert slept == []
print("OK")
shutil.rmtree(home, ignore_errors=True)
