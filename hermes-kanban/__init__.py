"""hermes-kanban: kanban that sorts itself out.

- A card that blocks wakes the bot that created it, in its "Kanban desk" chat, to answer it, re-plan or set up a card for
  another bot; only what that bot cannot decide goes to the person, as a question on the card. A flaky (transient) block is
  retried once. A card that keeps blocking is not handed to a bot again: the person is asked once (``supervisor.py``).
- A card's files, and the files of the cards it waits for, are copied into its workspace before its worker starts, so a
  worker whose terminal runs in Docker can read them (``files.py``).
- Hooks other plugins can register for, in the process named: ``kanban_card_blocked`` and ``kanban_card_looping`` (the
  card's worker), ``kanban_card_files_staged`` (the dispatcher), ``kanban_card_woke`` and ``kanban_card_retried`` (the
  gateway), ``kanban_card_done`` (the process that completed the card). See the README for their arguments.

Where it runs: a card blocks inside its worker, which loads its bot's own plugins, so every bot that works cards needs its own
copy; the dispatcher and the desk runner (``desk.py``) run in the messaging gateway, which loads the default profile's. Install
it for the default profile and for each bot that works cards. ``HERMES_KANBAN_DESK=off`` stops the wakes and retries.
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger("hermes-kanban")


def gateway_process(argv=None) -> bool:
    """Whether this process is the messaging gateway: started as ``hermes gateway run`` (with any flags between)."""
    words = [word for word in (sys.argv if argv is None else argv)[1:] if not word.startswith("-")]

    return any(word == "gateway" and words[index + 1:index + 2] == ["run"] for index, word in enumerate(words))


def emit(name: str, **fields) -> None:
    """Fires one of this plugin's own hooks for other plugins. Never raises."""
    try:
        from hermes_cli.lifecycle import has_hook, invoke_hook

        if has_hook(name):
            invoke_hook(name, **fields)
    except Exception:
        log.debug("hermes-kanban: the %s hook failed", name, exc_info=True)


def guarded(name: str, fn):
    def hook(**kw):
        try:
            fn(**kw)
        except Exception:
            log.exception("hermes-kanban: %s failed", name)

    return hook


def register(ctx) -> None:
    from . import desk, files, supervisor

    watch = supervisor.Supervisor(emit=emit)
    stager = files.Stager(emit=emit)

    def on_completed(task_id=None, board=None, assignee=None, run_id=None, summary=None, **_):
        emit("kanban_card_done", task_id=task_id, board=board, assignee=assignee, run_id=run_id, summary=summary)

    ctx.register_hook("kanban_task_blocked", guarded("the blocked-card hook", watch.on_blocked))
    ctx.register_hook("kanban_task_claimed", guarded("staging a card's files", stager.on_claimed))
    ctx.register_hook("kanban_task_completed", guarded("the completed-card hook", on_completed))

    runner = "off"

    if gateway_process() and desk.enabled():
        desk.Desk(emit=emit).start()
        runner = "started"

    log.info("hermes-kanban: hooks registered, desk runner %s (%s)", runner, "a2a" if desk.through_gateway() else "one-shot turns")
