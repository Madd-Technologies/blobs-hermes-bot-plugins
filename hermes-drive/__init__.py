"""hermes-drive: lets the HermesBot app show Google Drive files a bot finds.

Standalone plugin. Its dashboard API (`dashboard/plugin_api.py`) streams a Drive file to the app's viewer through this server,
with the Google sign-in Hermes's google-workspace skill keeps (`google_token.json`, read only; see `drive.py`); nothing is
stored here or on the phone. When that sign-in has Drive access, it also ships the `drive` skill (`skills/drive/`: search and
read files with a script that keeps the token to itself) and adds one short paragraph to each new session's prompt, so the bots
know the skill is there and how to link a file for the app.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger("hermes-drive")

SKILL_MD = Path(__file__).resolve().parent / "skills" / "drive" / "SKILL.md"


def register(ctx) -> None:
    from .drive import LINK_HINT, SKILL_DESCRIPTION, SKILL_HINT, SKILL_NAME, load_credentials

    credentials = load_credentials()

    if credentials is None or not credentials.drive:
        log.info("hermes-drive: no Google sign-in with Drive access (google_token.json); the routes will say so and bots are not told about it")
        return

    # Older Hermes has no plugin skills: the prompt paragraph then only says how to link.
    hint = LINK_HINT
    register_skill = getattr(ctx, "register_skill", None)

    if register_skill is not None:
        try:
            register_skill(SKILL_NAME, SKILL_MD, SKILL_DESCRIPTION)
            hint = LINK_HINT + SKILL_HINT
        except Exception as error:  # a skill that can't load must not take the routes down with it
            log.warning("hermes-drive: the drive skill was not registered: %s", error)

    register_section = getattr(ctx, "register_system_prompt_section", None)

    if register_section is not None:
        register_section("hermes-drive.links", hint, max_chars=len(hint) + 40)

    log.info("hermes-drive: Google Drive for %s", credentials.account or "the signed-in account")
