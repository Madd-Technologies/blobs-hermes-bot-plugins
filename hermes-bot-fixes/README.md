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
```
