# hermes-bot-mobile

The HermesBot phone app's companion on a Hermes server (formerly `hermes-push`).

## Push notifications

It listens to the agent's own hooks (an approval is waiting, a question was asked, a routine broke) and sends a small encrypted Web Push message (RFC 8291, VAPID) to each phone the app registered. The phone's UnifiedPush
distributor, for example [ntfy](https://ntfy.sh), delivers it; no Google or Apple account is involved. What needs the
person is pushed (an approval, a question, a routine that broke), never that a turn finished or a routine ran (since
0.1.2). Nothing is pushed for the chat the phone has on screen. Each push names the chat it is about ("Juniper · Plan the
week", the side chat's title; just the bot for its main chat) or the routine that broke. Command text is never put in a
push.

Since 0.1.3, a bot set to `all` (the app's Notifications: Everything) also pushes the start of each reply it gives in one of
the person's chats, the way a messaging app shows a message: "Juniper", then its first words ("The quote came back at
$640…"), from Hermes's `post_llm_call` hook. Only chats the person has in the app, Hermes Desktop or the dashboard (platform
`tui`); never another bot's message to this one, a room member, a subagent, a routine's run or its summary, a heartbeat, or
a chat platform with its own notifications. Not while the app is in front (Home shows the reply). A newer reply in a chat
replaces that chat's last one. Replies go under their own Android channel, "Replies", so the phone's lock-screen settings
can hide their words; secrets are redacted with Hermes's own redaction first. `needs_you` keeps only what needs you.

Tapping a push opens what it is about: the exact chat (`hermesbot://bot/<profile>?chat=<session id>`, the bot's main chat
or a side chat, also when a subagent asked), the group room for a room member (`hermesbot://room/<room id>`, or the app's
Needs you list when the room cannot be told), and the bot's routines for a routine.

## Live room activity

Hermes runs a group-room member's turn on a hidden session, and the room log only says when it started and settled. Through
the `on_room_member_activity` hook (Hermes 0.21.5+) this keeps what each bot is doing right now: running a tool (and its
name), thinking, writing, or waiting for your OK. Only the phase, the tool's name and when it began are kept, never text,
tool arguments or output, in one small file per room, written only when a phase changes.

## Where a bot runs

Hermes runs a bot's commands on another computer when that bot's own `config.yaml` says `terminal.backend: ssh` (with
`ssh_host`, `ssh_user`, `ssh_port`, `ssh_key`), or in a container with `docker`. The app's "Runs on" setting (bot details,
and New bot) writes those keys through the dashboard's config route; since 0.1.4 this plugin does the two parts a phone
cannot:

- **A key pair for the bot**, made on this server with its own `ssh-keygen` (ed25519, no passphrase, comment
  `blobs-<profile>`). The private key never leaves the server; the app gets the public line, for `~/.ssh/authorized_keys`
  on the other computer, and the private key's path, for `ssh_key`.
- **A connection check** that runs `uname -srm` with the same `ssh` options as Hermes's SSH backend (key only, a new
  computer's host key accepted on first contact, a 10 s connect timeout), so a pass means the bot's commands will connect
  too. For `docker` it checks that Docker answers.

On SSH, Hermes copies the bot's skills and credential files to `~/.hermes` on that computer.

## Routes

Under `/api/plugins/hermes-bot-mobile/`, behind the dashboard's sign-in:

| Route | What |
| --- | --- |
| `GET /status` | registered phones, settings, the server's VAPID public key |
| `POST /devices`, `DELETE /devices/{id}` | register or remove a phone |
| `PUT /settings` | each bot on or `off` (`all` and `needs_you` both mean on) |
| `PUT /presence` | what the phone has on screen (no pushes for it) |
| `POST /test` | send a test push |
| `GET /rooms/{room_id}/activity` | `{members: {member_id: {phase, tool, since}}, now}` |
| `GET /runs-on/key?profile=` | the bot's public SSH line (`null` before one is made) and its private key's path |
| `POST /runs-on/key` | `{profile}`: makes the bot's key pair if it has none |
| `POST /runs-on/check` | `{profile, backend: ssh\|docker, host, user, port, key}`: `{ok, system}` or `{ok: false, reason, detail}` |

## Data

`~/.hermes/hermes-bot-mobile/` (the Hermes root, not a profile: one phone, many bots): `devices.json`, `settings.json`,
`vapid.json`, `presence.json` (all 0600), `rooms/<room_id>.json` and `ssh/<profile>/id_ed25519` (0600, folders 0700). On
first start it copies `~/.hermes/hermes-push/` there if this folder has none of its own files yet, so phones registered
with `hermes-push` keep getting pushes.

## Tests

    python -m unittest discover -s hermes-bot-mobile/tests

`tests/relay_check.py <ntfy url>` checks that a push relay passes messages through byte for byte;
`tests/integration_hermes.py` loads the plugin through a real Hermes plugin manager.
