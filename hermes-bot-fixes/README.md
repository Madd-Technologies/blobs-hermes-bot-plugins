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
- **Every answer to another bot arrives twice.** In Bot Mode, a bot that gets "Message from 🤖 …" is told to reply to that
  bot with `message_agent`, but the reply it gives in its chat already goes back to the sender by itself (it is what
  wakes the sender). So the sender got each answer twice, once as that wake-up and once as a new message, and ran one
  short turn after another; its answer to the second one went back too and woke the first bot again. Now a
  `message_agent` to the bot whose message started the turn is not sent: the bot is told its reply goes back on its own
  and gives its answer there. Messages to other bots, and to that bot from any other turn (a wake-up, a routine, the
  person), are sent as before. Only bots on this server are matched; a teammate on another machine is left alone.
- **Other bots' messages run in the main chat.** Hermes runs every `message_agent` from a bot on this server as a turn in
  the recipient's main chat ("Bot Chat"): the chat shows the bot working, your own message waits behind that turn or is
  mixed into it, and the exchange between the bots sits in the chat you talk to the bot in. Now it runs as one turn in
  the recipient's **inbox**, a chat of its own titled **"Bot Inbox"** (made on first use, and continued as
  "Bot Inbox #2" and so on after Hermes compresses it). The recipient answers there, and its answer goes back to the
  bot that asked, as before, as the wake-up in the chat that sent the message. The main chat gets no rows and never
  runs, whether or not it is open. Messages from several bots at once queue on the recipient's turn lock, the same lock
  Hermes's own deliveries and rooms use (`bot_mode.turn_wait_seconds`, then the sender hears that the bot was busy).
  - The inbox is its own conversation: it doesn't share the main chat's history, and a bot can't message other bots
    from it (that needs its main chat), so two bots can't bounce answers back and forth.
  - It runs like `hermes chat -Q`: a dangerous command follows `approvals.single_query_mode` (deny by default) and
    nothing waits for a person.
  - The sender's main chat still runs one short turn when an answer to its own message comes back: that is the bot
    telling you what the other bot said.
  - Unchanged: messages from bots on other machines (peers, relay, another connection), routines and Kanban.
  - To switch it off (say, once Hermes itself does this), put `HERMES_BOT_FIXES_BOT_INBOX=off` in `~/.hermes/.env` and
    restart the dashboard and the messaging gateway; messages then run in the main chat exactly as Hermes runs them.

## Install

In the **default profile**: the fixes apply to the whole Hermes process, so every bot the dashboard and the messaging
gateway run is covered.

```sh
hermes plugins install Madd-Technologies/blobs-hermes-bot-plugins/hermes-bot-fixes --ref <commit sha> --force --enable
```

A bot's turns that run outside the dashboard and the gateway, in a one-shot `hermes -p <bot> chat`, load only that
bot's own plugins, so give each bot its own copy too: the same command with `-p <bot>` after `hermes`. Copies in one
process share the fixes and run one database guard. The inbox is decided where `message_agent` runs, in the sender's
process: the dashboard for a bot whose main chat is open there.

Then restart the dashboard and the messaging gateway.

## Develop

```sh
python -m unittest discover -s hermes-bot-fixes/tests
~/.hermes/hermes-agent/venv/bin/python hermes-bot-fixes/tests/integration_hermes.py [path to a Hermes checkout]
~/.hermes/hermes-agent/venv/bin/python hermes-bot-fixes/tests/integration_compaction.py [path to a Hermes checkout]
~/.hermes/hermes-agent/venv/bin/python hermes-bot-fixes/tests/integration_handoff.py [path to a Hermes checkout]
~/.hermes/hermes-agent/venv/bin/python hermes-bot-fixes/tests/integration_dmreply.py [path to a Hermes checkout]
~/.hermes/hermes-agent/venv/bin/python hermes-bot-fixes/tests/integration_inbox.py [path to a Hermes checkout]
```
