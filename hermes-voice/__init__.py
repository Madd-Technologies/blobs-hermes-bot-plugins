"""hermes-voice: Grok voice calls with your bots from the HermesBot app.

Standalone plugin with routes only (`dashboard/plugin_api.py`): no hooks, tools or skills, so one copy in the default profile
serves every bot. The phone asks for a one-time secret, opens the call with xAI itself, and hands each task to the bot through
the bot's normal chat. See `voice.py`.
"""

from __future__ import annotations

import logging

log = logging.getLogger("hermes-voice")


def register(ctx) -> None:
    log.info("hermes-voice: voice call routes for the HermesBot app under /api/plugins/hermes-voice/")
