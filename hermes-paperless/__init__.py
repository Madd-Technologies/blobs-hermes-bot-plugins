"""hermes-paperless: lets the HermesBot app show Paperless-ngx documents a bot finds.

Standalone plugin. Its dashboard API (`dashboard/plugin_api.py`) streams a document from Paperless to the app's viewer through
this server, with the server's Paperless token; nothing is stored here or on the phone. When Paperless is set up, it also ships
the `paperless` skill (`skills/paperless/`: search and read documents with a script that keeps the token to itself) and adds one
short paragraph to each new session's prompt, so the bots know the skill is there and how to link a document for the app.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("hermes-paperless")

SKILL_MD = Path(__file__).resolve().parent / "skills" / "paperless" / "SKILL.md"


def register(ctx) -> None:
    from .paperless import LINK_HINT, SKILL_DESCRIPTION, SKILL_HINT, SKILL_NAME, load_settings

    settings = load_settings()

    if settings is None:
        log.info("hermes-paperless: PAPERLESS_URL / PAPERLESS_TOKEN not set; the routes will say so and bots are not told about it")
        return

    # Older Hermes has no plugin skills: the prompt paragraph then only says how to link.
    hint = LINK_HINT
    register_skill = getattr(ctx, "register_skill", None)

    if register_skill is not None:
        try:
            register_skill(SKILL_NAME, SKILL_MD, SKILL_DESCRIPTION)
            hint = LINK_HINT + SKILL_HINT
        except Exception as error:  # a skill that can't load must not take the routes down with it
            log.warning("hermes-paperless: the paperless skill was not registered: %s", error)

    register_section = getattr(ctx, "register_system_prompt_section", None)

    if register_section is not None:
        register_section("hermes-paperless.links", hint, max_chars=len(hint) + 40)

    log.info("hermes-paperless: Paperless at %s", settings.host)
