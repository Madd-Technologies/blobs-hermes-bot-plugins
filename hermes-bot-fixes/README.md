# hermes-bot-fixes

Small local fixes for **Hermes 0.21.x** on a host that runs Bot Screens. Nothing in Hermes is edited: each fix wraps a
function Hermes looks up by name when it runs, after checking it is the function it expects (anything else is left alone
and logged), so a newer Hermes that changed it simply goes unpatched.

## What it fixes

- **Screenshots land on the desktop panel.** A `computer_use` capture that names no window takes the frontmost one, and
  Xfce's panels sit above every app, so the screenshot was the top bar instead of the browser. Panels (`xfce4-panel` and
  the other common docks) are now left out of that choice, as Hermes already leaves out desktop and GNOME helper windows.
  A capture that names its window (`app=`) is never second-guessed.
- **The screenshot after an action shows the screen before it.** `capture_after` now waits 0.75 s for the screen to
  settle (set `HERMES_BOT_FIXES_SETTLE_SECONDS`, up to 3).
- **"file is not a database" from a stray TLS record.** A long-running Hermes can write one 24-byte TLS record at offset 5
  of an SQLite file (`SQLit` then `17 03 03 00 13`), and whatever lives there stops: group rooms, kanban, cron ledgers.
  Those bytes are header fields with known values, so a guard puts them back: at start and every 10 minutes it checks the
  SQLite files in the Hermes root and each profile; for that exact signature it backs the file up, repairs a copy, runs
  `integrity_check` on it, and only then writes the 29 header bytes into the original, in place. Files over 512 MB (such
  as `state.db`) are reported, not repaired. Backups (the last 3 per file) and `status.json` live in
  `~/.hermes/hermes-bot-fixes/`.
- **After a compaction, the bot redoes work it handed to a background task.** When a background result (a task finishing,
  or a background process) wakes a bot and that turn gets compacted, Hermes skips the result's row, lands on the person's
  last message and appends it again as "[STILL IN PROGRESS … Continue it; do not start over.]", although the bot had
  already answered it ("running in the background"). The bot then does the whole job again itself. Now, when a plain reply
  came after that message before the wake-up, the result is the task: nothing is restated while it is still in the chat,
  and if it was summarised away the result is restated instead (as a note row, not the person's words). A request still
  being worked on is restated as before.
- **A background task comes back unfinished and the bot finishes it in the chat.** When a `delegate_task` result comes
  back unfinished, Hermes wakes the bot with a note that ends "act on the result or re-dispatch", and a bot may then do
  the rest itself, step after step, in the chat its person talks to it in. The note now ends with a hand-off: queue what
  is left as a kanban card assigned to the bot itself (no step cap, survives restarts, retried, and a blocked card asks
  its person), or re-dispatch it with `delegate_task` when the bot has no kanban tools, then answer in a sentence or two.
  The note names the bot's own profile (a card for an unknown profile is never picked up) and an `idempotency_key`, so a
  result replayed after a restart doesn't queue the work twice. Unfinished means it hit its step cap, timed out, stalled
  or was cut off by a server restart; a stop the person pressed, an error, a cancel or a refusal to start keeps Hermes's
  own note. For the card route a bot needs the kanban toolset (`hermes -p <bot> tools enable kanban`).

## Install

For the **default profile only**: the fixes apply to the whole Hermes process, so every bot it runs is covered.

```sh
hermes plugins install Madd-Technologies/blobs-hermes-bot-plugins/hermes-bot-fixes --ref <commit sha> --force --enable
```

Then restart the dashboard and the messaging gateway.

## Develop

```sh
python -m unittest discover -s hermes-bot-fixes/tests
~/.hermes/hermes-agent/venv/bin/python hermes-bot-fixes/tests/integration_hermes.py [path to a Hermes checkout]
~/.hermes/hermes-agent/venv/bin/python hermes-bot-fixes/tests/integration_compaction.py [path to a Hermes checkout]
~/.hermes/hermes-agent/venv/bin/python hermes-bot-fixes/tests/integration_handoff.py [path to a Hermes checkout]
```
