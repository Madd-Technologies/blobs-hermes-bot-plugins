# hermes-bot-plugins

Server plugins for [Hermes Agent](https://github.com/NousResearch/hermes-agent) that back the **HermesBot** phone app:
a Grok-Bot-style client for Hermes "Bot Mode". Each folder is one standalone Hermes plugin.

| Plugin | What it does | Needs |
| --- | --- | --- |
| [`hermes-bot-mobile`](hermes-bot-mobile) | The app's companion: Web Push notifications when a bot needs you (approvals, questions, finished work), no pushes for the chat you have open, live activity of bots in group rooms (running a tool, thinking, writing, waiting for your OK), restarting the dashboard (and the gateway) from the app, and Taildropping a file from the server to one of your devices. Formerly `hermes-push`, whose registered phones it carries over. | A UnifiedPush distributor on the phone (e.g. ntfy). Room activity needs Hermes 0.21.5+. Restarting needs the dashboard to run as a systemd service. Taildrop needs Tailscale on the server and Hermes 0.21.5+. |
| [`hermes-paperless`](hermes-paperless) | Streams Paperless-ngx documents to the app's viewer; a skill so bots can search, read and link documents; a Paperless tab in the dashboard and a page in Hermes Desktop. | `PAPERLESS_URL` and `PAPERLESS_TOKEN` (or `PAPERLESS_TOKEN_FILE`) in `~/.hermes/.env`. |
| [`hermes-drive`](hermes-drive) | Streams Google Drive files to the app's viewer (Google Docs, Sheets and Slides as PDF); a skill so bots can search, read and link files. Read-only. | The google-workspace skill's sign-in (`~/.hermes/google_token.json`) with a Drive scope. |
| [`hermes-dashboard`](hermes-dashboard) | Optional. A dashboard per bot in the app: collections the bot designs (its own fields, a status, history) shown as panels, four `dashboard_*` tools, and a skill that teaches it to track anything and keep it fresh with a routine. | Nothing. Install it for each bot that should keep one (see its README). |
| [`hermes-bot-fixes`](hermes-bot-fixes) | Local fixes for Hermes 0.21.x on a Bot Screen host: screenshots skip the desktop panel and wait for the screen to settle, a guard repairs SQLite files whose header a stray TLS record overwrote (checked on a copy first), compaction stops handing a bot back a request it already answered, unfinished background work goes to a new background worker instead of the chat, a bot answers another bot's message once, in its reply, and another bot's message runs in the recipient's inbox chat ("Bot Inbox") instead of its main chat. | Nothing. Install it for the default profile and for each bot; bots need the kanban toolset for the card route. |
| [`hermes-voice`](hermes-voice) | Optional. Grok voice calls with your bots from the app: the phone talks to xAI directly with a one-time secret this server makes from its Grok sign-in or a bot's own xAI key, and the Grok voice hands real work to the bot through its normal chat. Keeps each bot's call settings (on or off, voice, which account pays). | Hermes 0.21.6+, and the server's xAI Grok sign-in or a bot's `XAI_API_KEY`. Install it once, in the default profile. |
| [`hermes-phone`](hermes-phone) | Optional. Phone errands for your bots through your own Twilio number (bookings, quotes, returning a voicemail): the bot writes a call brief, you approve it in the app, and a Grok voice that knows only that brief makes the calls and reports back. US and Canada, outbound only. | Hermes 0.21.6+, your own Twilio account and number, the Grok sign-in or an `XAI_API_KEY`, a public HTTPS address for its call service (e.g. Tailscale Funnel). See its README. |
| [`hermes-kanban`](hermes-kanban) | Optional. Kanban that sorts itself out: a blocked card wakes the bot that created it, in its own "Kanban desk" chat, to answer it, re-plan or hand part of it to another bot, and only what it can't decide reaches you; a card that keeps blocking asks you once; a flaky block is retried once; a card's files (and its parent cards') are copied into its workspace so a worker in Docker can read them; and `kanban_card_*` hooks for other plugins. | Hermes 0.21.6+. Install it in the default profile and in each bot that works cards. |

Nothing here stores your documents, files or call audio, and no token ever leaves your server.

## Install

Pin an exact commit (`--ref` takes the full 40-character SHA), then restart the dashboard: plugin routes, hooks and skills
load when Hermes starts.

```sh
hermes plugins install Madd-Technologies/blobs-hermes-bot-plugins/hermes-bot-mobile --ref <commit sha> --enable
hermes plugins install Madd-Technologies/blobs-hermes-bot-plugins/hermes-paperless --ref <commit sha> --enable
hermes plugins install Madd-Technologies/blobs-hermes-bot-plugins/hermes-drive --ref <commit sha> --enable
```

To update, run the same command with the newer commit and `--force`. The HermesBot app's Settings → Server plugins shows
which of these your server has, at which version, and copies a message that has your server's own bot install or update
them.

Moving from `hermes-push`: install `hermes-bot-mobile`, then `hermes plugins disable hermes-push`. On its first start
`hermes-bot-mobile` copies `~/.hermes/hermes-push/` (registered phones, settings, push keys) into its own folder, so phones
keep getting pushes without registering again; the old folder is left as it was.

## Develop

Each plugin has its own tests, run with the Python of a Hermes install (or any Python 3.11+ with `fastapi`, `httpx`,
`pyyaml` and `cryptography`). Hermes 0.21.6+ ships no PyYAML: it reads YAML through its own `hermes_yaml` (ruamel), so a
plugin that reads YAML imports `hermes_yaml` when it is there and PyYAML only on older Hermes. A bare `import yaml` fails on
a current server:

```sh
python -m unittest discover -s hermes-bot-mobile/tests
python -m unittest discover -s hermes-paperless/tests
python -m unittest discover -s hermes-drive/tests
python -m unittest discover -s hermes-dashboard/tests
python -m unittest discover -s hermes-bot-fixes/tests
python -m unittest discover -s hermes-voice/tests
python -m unittest discover -s hermes-phone/tests
python -m unittest discover -s hermes-kanban/tests
```

`hermes plugins validate <folder>` runs Hermes's own checks on a plugin.

## License

MIT, see [LICENSE](LICENSE).
