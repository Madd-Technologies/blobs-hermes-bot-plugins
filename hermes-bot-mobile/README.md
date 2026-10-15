# hermes-bot-mobile

The HermesBot phone app's companion on a Hermes server (formerly `hermes-push`).

## Push notifications

It listens to the agent's own hooks (an approval is waiting, a question was asked, a routine broke) and sends a small encrypted Web Push message (RFC 8291, VAPID) to each phone the app registered. The phone's UnifiedPush
distributor, for example [ntfy](https://ntfy.sh), delivers it; no Google or Apple account is involved. Only what needs the
person is pushed: never that a reply finished or a routine ran (since 0.1.2). Nothing is pushed for the chat the phone has
on screen. Each push names the chat it is about ("Juniper · Plan the week", the side chat's title; just the bot for its main
chat) or the routine that broke. Command text is never put in a push.

Tapping a push opens what it is about: the exact chat (`hermesbot://bot/<profile>?chat=<session id>`, the bot's main chat
or a side chat, also when a subagent asked), the group room for a room member (`hermesbot://room/<room id>`, or the app's
Needs you list when the room cannot be told), and the bot's routines for a routine.

## Live room activity

Hermes runs a group-room member's turn on a hidden session, and the room log only says when it started and settled. Through
the `on_room_member_activity` hook (Hermes 0.21.5+) this keeps what each bot is doing right now: running a tool (and its
name), thinking, writing, or waiting for your OK. Only the phase, the tool's name and when it began are kept, never text,
tool arguments or output, in one small file per room, written only when a phase changes.

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

## Data

`~/.hermes/hermes-bot-mobile/` (the Hermes root, not a profile: one phone, many bots): `devices.json`, `settings.json`,
`vapid.json`, `presence.json` (all 0600) and `rooms/<room_id>.json`. On first start it copies `~/.hermes/hermes-push/`
there if this folder has none of its own files yet, so phones registered with `hermes-push` keep getting pushes.

## Tests

    python -m unittest discover -s hermes-bot-mobile/tests

`tests/relay_check.py <ntfy url>` checks that a push relay passes messages through byte for byte;
`tests/integration_hermes.py` loads the plugin through a real Hermes plugin manager.
