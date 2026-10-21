# hermes-voice

Grok voice calls with your bots from the HermesBot app. Optional: install it to turn calls on for this server, uninstall it to
turn them off.

During a call the phone talks to xAI's realtime voice directly. This server only makes the one-time secret the phone opens the
call with, and keeps each bot's call settings. The Grok voice does the talking; when you ask for real work, it hands the
task to the bot through the bot's normal chat, and reads you the bot's answer.

## Needs

- Hermes 0.21.6 or later.
- One of:
  - **The server's Grok sign-in** (the xAI Grok sign-in Hermes already uses for SuperGrok). Calls count against your Grok
    plan's limits.
  - **A bot's own `XAI_API_KEY`** in its `.env`. Calls are billed to that key's xAI team, about $0.08 a minute. With
    zero data retention on for the team, xAI keeps nothing.

A bot that was never set up uses the sign-in when the server has one, otherwise its own key. Each bot can be switched in the
app (bot details, Voice).

## Install

Routes only, so install it once, in the default profile, then restart the dashboard:

```sh
hermes plugins install Madd-Technologies/blobs-hermes-bot-plugins/hermes-voice --ref <commit sha> --enable
```

## What it does

- `GET /api/plugins/hermes-voice/bot?profile=<bot>` and `PUT` the same: a bot's settings (calls on or off, the voice, which
  account pays), kept in `~/.hermes/hermes-voice/bots.json`.
- `POST /api/plugins/hermes-voice/session?profile=<bot>`: a secret for one call.
  - It lasts 60 seconds and opens a single connection; the call itself can run up to xAI's limit (120 minutes).
  - The sign-in is read and refreshed through Hermes's own helper, so Hermes keeps managing it.

The sign-in token and keys never leave this server and are never logged. The plugin stores no audio, transcripts or
conversations, and never asks xAI to keep one.

## Hermes 0.22

Works on Hermes 0.21 and 0.22. The bot routes take `?bot=<bot>` (since 0.1.2; `?profile=` still works on 0.21): Hermes 0.22
refuses `?profile=` for another bot on a plugin it did not ship. The plugin then enters that bot's own profile itself (the
same scope Hermes applies for `?profile=`), so the bot's own xAI key and Grok sign-in are read.

## Develop

```sh
python -m unittest discover -s hermes-voice/tests
```

`tests/integration_hermes.py` loads the plugin through a Hermes checkout's own plugin manager and dashboard routes, against a
throwaway `HERMES_HOME`.

## Credits

Session minting, the persona and the two tools are adapted from [hermes-xai](https://github.com/Julientalbot/hermes-xai)
(MIT); see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
