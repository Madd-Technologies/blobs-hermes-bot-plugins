# hermes-bot-plugins

Server plugins for [Hermes Agent](https://github.com/NousResearch/hermes-agent) that back the **HermesBot** phone app:
a Grok-Bot-style client for Hermes "Bot Mode". Each folder is one standalone Hermes plugin.

| Plugin | What it does | Needs |
| --- | --- | --- |
| [`hermes-bot-mobile`](hermes-bot-mobile) | The app's companion: Web Push notifications when a bot needs you (approvals, questions, finished work), no pushes for the chat you have open, and live activity of bots in group rooms (running a tool, thinking, writing, waiting for your OK). Formerly `hermes-push`, whose registered phones it carries over. | A UnifiedPush distributor on the phone (e.g. ntfy). Room activity needs Hermes 0.21.5+. |
| [`hermes-paperless`](hermes-paperless) | Streams Paperless-ngx documents to the app's viewer; a skill so bots can search, read and link documents; a Paperless tab in the dashboard and a page in Hermes Desktop. | `PAPERLESS_URL` and `PAPERLESS_TOKEN` (or `PAPERLESS_TOKEN_FILE`) in `~/.hermes/.env`. |
| [`hermes-drive`](hermes-drive) | Streams Google Drive files to the app's viewer (Google Docs, Sheets and Slides as PDF); a skill so bots can search, read and link files. Read-only. | The google-workspace skill's sign-in (`~/.hermes/google_token.json`) with a Drive scope. |
| [`hermes-dashboard`](hermes-dashboard) | Optional. A dashboard per bot in the app: collections the bot designs (its own fields, a status, history) shown as panels, four `dashboard_*` tools, and a skill that teaches it to track anything and keep it fresh with a routine. | Nothing. Install it for each bot that should keep one (see its README). |
| [`hermes-bot-fixes`](hermes-bot-fixes) | Local fixes for Hermes 0.21.x on a Bot Screen host: screenshots skip the desktop panel and wait for the screen to settle, and a guard repairs SQLite files whose header a stray TLS record overwrote (checked on a copy first). | Nothing. Install it for the default profile. |

Nothing here stores your documents or files, and no token ever leaves your server.

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
`pyyaml` and `cryptography`):

```sh
python -m unittest discover -s hermes-bot-mobile/tests
python -m unittest discover -s hermes-paperless/tests
python -m unittest discover -s hermes-drive/tests
python -m unittest discover -s hermes-dashboard/tests
python -m unittest discover -s hermes-bot-fixes/tests
```

`hermes plugins validate <folder>` runs Hermes's own checks on a plugin.

## License

MIT, see [LICENSE](LICENSE).
