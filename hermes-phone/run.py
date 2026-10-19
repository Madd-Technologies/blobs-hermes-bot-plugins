"""Starts the call service from the installed plugin folder, as the plugin package `hermes_phone`.

Run with Hermes's own interpreter (so Hermes's sign-in helper and Bot Chat delivery import):

    <hermes python> ~/.hermes/plugins/hermes-phone/run.py

The folder is loaded as a package instead of being put on `sys.path`, so module names here (`store`, `brief`, …) never
shadow Hermes's own.
"""

import importlib
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent

if __name__ == "__main__":
    sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != HERE]
    package = types.ModuleType("hermes_phone")
    package.__path__ = [str(HERE)]  # type: ignore[attr-defined]
    sys.modules["hermes_phone"] = package
    importlib.import_module("hermes_phone.service").main()
