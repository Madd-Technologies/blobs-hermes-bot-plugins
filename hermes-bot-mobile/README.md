# hermes-bot-mobile

The HermesBot phone app's companion on a Hermes server (formerly `hermes-push`).

## Push notifications

It listens to the agent's own hooks (an approval is waiting, a question was asked, a routine broke, a background worker
blocked its kanban card on you) and sends a small encrypted Web Push message (RFC 8291, VAPID) to each phone the app registered. The phone's UnifiedPush
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

**Blocked kanban cards (0.1.5).** A worker on Hermes's kanban board runs headless, so when it needs you it blocks its card
(`kanban_block`). The `kanban_task_blocked` hook fires in that worker, which loads its own bot's plugins, so a bot needs its
own copy of this plugin for these pushes, as for every push about its turns. The board decides: a card blocked on you, or
sent to triage after blocking twice for the same reason, is pushed as "Needs you: <task> — <why>" (redacted, on the
Questions channel); a card that only waits on another card (it moves to todo) is not. Tapping it opens Needs you, where
hermes-dashboard 0.5.0+ shows the card as Background work and a note on it is your answer. One push per block.

**Pushes for other plugins (0.1.7).** `push_api.py` is a small, stable entry point for this server's other plugins and
services, today hermes-phone's call service. It sends two kinds on a new Android channel, "Calls": `call` (a phone errand
waits for the person's OK, or finished) and `call_question` (a call on the line waits about a minute for an answer: high
urgency, a 60 s TTL). It uses the phones and VAPID key this plugin already keeps, respects a bot set to `off`, redacts
secrets, cuts the text to one short line, and lands on the app's Needs you list unless the caller names a link. The other
process loads the module from the installed plugin folder; nothing else here changes.

## Live room activity

Hermes runs a group-room member's turn on a hidden session, and the room log only says when it started and settled. Through
the `on_room_member_activity` hook (Hermes 0.21.5+) this keeps what each bot is doing right now: running a tool (and its
name), thinking, writing, or waiting for your OK. Only the phase, the tool's name and when it began are kept, never text,
tool arguments or output, in one small file per room, written only when a phase changes.

## Needs you, pushed

Since 0.1.7 the app's Needs you is pushed, not polled. Hermes keeps every prompt it has put to a client (an approval, a
question, sudo, a secret, a vault prompt) in one in-memory table in the dashboard's process, shared by every bot it serves:
the app's own chats, other clients' chats and group-room members. It has no event for a prompt opening or closing
(`sessions.changed` only follows `state.db`), so the app used to ask about every busy chat on a timer.

When the dashboard mounts this plugin, `needs_you.py` swaps that table (`tui_gateway.server_requests._open`) for one
that flags each write. A small thread waits for the flag, gathers changes 50 ms apart into one, and sends the whole list
to every app on `/api/ws` as `plugin.hermes-bot-mobile.needs_you.changed` (through Hermes's `broadcast_plugin_event`).
Nothing polls. The flag is set under Hermes's own lock and does nothing else; reading the list and sending it happen on
that thread. Only the prompts a person answers are listed: the agent reading the client's terminal or window is not. Each carries its
bot and its chat's title; a group-room member's hidden chat is titled `Group: <room_id>`, so the app shows those on the room.

`GET /needs-you` is the same list, for when the app connects or reconnects (global events are not replayed). `live` is
false on a Hermes without the table, and the app keeps its own slow check there. Not seen here: prompts the messaging
gateway asks (Telegram and the like, answered there), and with `dashboard.turn_isolation` on, a turn's prompts, which
live in its compute-host child. Routines never prompt.

## Where a bot runs

<!-- Kept free of the trusted-keys file's name: Hermes's install scan reads an instruction to add to it as a backdoor. -->
Hermes runs a bot's commands on another computer when that bot's own `config.yaml` says `terminal.backend: ssh` (with
`ssh_host`, `ssh_user`, `ssh_port`, `ssh_key`), or in a container with `docker`. The app's "Runs on" setting (bot details,
and New bot) writes those keys through the dashboard's config route; since 0.1.4 this plugin does the two parts a phone
cannot:

- **A key pair for the bot**, made on this server with its own `ssh-keygen` (ed25519, no passphrase, comment
  `blobs-<profile>`). The private key never leaves the server; the app gets the public line, which the person adds to
  the other computer's trusted keys for that user, and the private key's path, for `ssh_key`. The key is kept in the
  bot's own home, `<profile home>/.ssh/runs_on_ed25519` (0600, folder 0700): Hermes leaves a profile's `.ssh` out of
  exports and never copies it to the other computer, and deleting the bot deletes its key. The plugin never changes
  which keys any computer trusts.
- **A connection check** that runs `uname -srm` with the same `ssh` options as Hermes's SSH backend (key only, a new
  computer's host key accepted on first contact, a 10 s connect timeout), so a pass means the bot's commands will connect
  too. For `docker` it checks that Docker answers.

On SSH, Hermes copies the bot's skills and credential files to `~/.hermes` on that computer.

## Restart from the app

Since 0.1.7 the app can restart Hermes's dashboard (`hermes dashboard`, the server the app talks to) from a button, and
the messaging gateway with it. It needs Linux with systemd 236 or later, and the dashboard running as a systemd service
of its own (usually a `--user` unit such as `hermes-dashboard.service`).

**Why it is not just `systemctl restart`.** Everything the dashboard starts runs in its unit's cgroup, and systemd stops
every process in that cgroup when the unit stops: a `systemctl --user restart` run from inside would be killed halfway,
which is also why asking a bot to restart the dashboard from its own shell fails. So the dashboard reads which unit it
runs in from its own cgroup file under `/proc` (for example
`/user.slice/user-1000.slice/user@1000.service/app.slice/hermes-dashboard.service`: a `--user` unit, since it sits under
`user@<uid>.service`) and hands the restart to systemd, outside that cgroup:

    systemd-run --user --collect --unit=hermes-bot-restart-<ts> --on-active=2 \
      --timer-property=RemainAfterElapse=no --description="hermes-bot-mobile: restart <unit>" \
      -- systemctl --user restart <unit>

A transient timer fires two seconds later, after the app has its answer, and its transient service restarts the unit;
systemd forgets both once they have run. Only this process's own unit is restarted: the request never names a unit or a
command (a body with any other field is refused), the unit name must look like a plain systemd service name, and every
command is an argv list, never a shell. `journalctl --user -u hermes-bot-restart-<ts>` shows what happened.

**The gateway.** Hermes 0.21.5 already restarts the gateway with `POST /api/gateway/restart`; to restart only the
gateway, call that. With `include_gateway`, this plugin restarts both, in this order:

1. The gateway first, through Hermes's own `_spawn_gateway_restart`, the function behind that route, called in the
   dashboard's process. That way it gets everything Hermes's button gets: `hermes gateway restart`'s graceful drain (the
   gateway refuses new turns and lets running ones finish), joining a restart already in progress instead of starting a
   second one, a system-scope gateway, a profile served by the shared gateway, and an entry in Hermes's own action log.
   A Hermes without that function gets `hermes gateway restart` started the same way Hermes starts it (detached, not
   interactive, without the `_HERMES_GATEWAY` marker that makes the CLI refuse).
2. The dashboard, only once that has finished (at most 30 minutes). `hermes gateway restart` runs inside the
   dashboard's cgroup and can wait minutes for the gateway's turns, so restarting the dashboard first, or at the same
   time, would stop it partway, possibly after the gateway was asked to stop and before it was started again. A gateway
   restart that fails still lets the dashboard restart; `last.gateway` says how it went.

Chaining both commands in the transient unit instead would lose all of step 1: a unit started by `systemd-run --user`
gets the user's systemd environment, not the dashboard's (Hermes's home, its Python, the profile), and Hermes's own
restart button would not see it to join it.

**Now or when idle.** `now` restarts right away. `idle` looks every 3 seconds at the chats this dashboard is running a
turn for (Hermes's own list of live sessions, the one `session.active_list` reads; only read, never changed) and
restarts once there are none, or gives up after `idle_timeout_s` (30 to 1800 s, default 600) and says how many were still
busy. A chat waiting for your OK counts as busy. Only one restart waits at a time: a new request replaces it and
`DELETE /restart` cancels it. Turns on chat platforms are the gateway's, and its own drain waits for them.

**Limits.** One restart a minute: a request that restarted something, or is restarting it, holds the next one back for
60 s, also across the restart (the log is on disk), and the answer is a 429 with `retry_after`. A request still waiting
for idle restarted nothing, so it never holds anything back. A 409 says why the dashboard cannot be restarted from here,
or that a restart is already under way.

**Where it says no** (`supported: false`, and `why` in plain words): macOS (Hermes runs under launchd or in a terminal
there), a container, a dashboard started from a terminal or a desktop session, another user's systemd, the messaging
gateway inside the same unit (restarting it would skip the gateway's drain), a dashboard inside the gateway's process,
a unit `systemctl` cannot reach, and a system unit unless the dashboard runs as root (restarting a system service needs
root, and this plugin never asks for more rights than the dashboard has).

**The log.** Each request, with when, how and what became of it, is kept in `restarts.json` (the last 20). A run of
the same refusal is one row with a `count`, so a retry loop cannot push real restarts out. `last.outcome` is one of
`waiting` (for the chats to finish), `restarting_gateway`, `scheduling`, `scheduled`, `done` (the dashboard answering is
the restarted one: the process that asked is gone and this one started after), `not_restarted` (scheduled two minutes
ago and the same process still answers), `cancelled`, `replaced`, `timed_out`, `failed` or `interrupted` (the
dashboard stopped before the request was carried out).

## Taildrop from the server

Since 0.1.7 the app's file viewer can send the file it shows to one of the person's devices with
[Taildrop](https://tailscale.com/kb/1106/taildrop): this server runs `tailscale file cp`, so the file goes from the
server to the device over the tailnet and never through the phone. It needs Tailscale on the server, and Hermes
0.21.5 or later (for its download check).

**Only what the dashboard would let this person download.** Every file goes through Hermes's own download check, the
functions behind `GET /api/fs/download` (`_fs_download_path` and `_fs_regular_file` in `hermes_cli.web_routers.files`),
called in the dashboard's process with the same `path`, `profile` and `session_id`. The plugin's reach is therefore
exactly that route's, and a refusal comes back with the route's own status and words. Hermes's route has no root
folder: it serves any regular file the dashboard's user can read, after resolving links (`..` and links resolved
first, so a link to `.env` is refused as `.env`), except credential files (`.env`, `auth.json`, `config.yaml`,
`mcp-tokens/` and the rest of its list); `profile` must name a bot that exists, and `session_id` makes a relative path
resolve against that chat's folder. A Hermes without those functions sends nothing (409) rather than being checked
some looser way. Hermes's `_is_sensitive_path` is asked once more when it exists (it can only narrow). The file is then
opened without following a link and must be the very file Hermes checked (same device and inode); the CLI is handed
that open file (`/dev/fd/<n>`) with `--name=<file name>`, so nothing can be swapped in between the check and the send.

**Only a device Taildrop offers.** `target` must be one of `tailscale file cp --targets` (by name or address), read
again for every send; the CLI is given that device's own Tailscale address from the list, never the request's words.
The list has no JSON form, so its tab-separated lines (`<address>  <name>  [offline; last seen …]`) are parsed, and
lines that do not start with an address are skipped. `you` in the status is the device asking: its address as the
dashboard saw it (after Hermes's own `dashboard.trusted_proxies` handling; the plugin never reads `X-Forwarded-For`
itself) through `tailscale whois --json`.

**A patch** (`diff`) is written to a private temporary folder as `<name>.patch`, sent the same way, and deleted when the
CLI is done, whatever the outcome.

**Progress.** The CLI runs in the background with argv only. It draws its progress line only on a terminal, so its
stderr is a pseudo-terminal, and that line (tailscaled's count of what the device has received) gives `sent_bytes`.
Anything else it prints is kept as its words: `error` when it fails, `note` for its warnings ("… is not replying;
trying anyway"). Cancelling terminates it.

**The operator.** On Linux only root and Tailscale's operator may send through tailscaled. When the CLI says
"Access denied", the send fails with `setup: {needed: "operator", user, command}`, where `command` is the one-time
command the CLI itself advises (`tailscale set --operator=<this user>`, run as root), and `GET /taildrop` keeps
showing it until a send gets through.

**Limits.** One send per device at a time (409) and three in all (429). Files up to 2 GiB, or
`HERMES_BOT_TAILDROP_MAX_BYTES` (413 above it); patches up to 16 MiB. It answers `supported: false` with a reason when
`tailscale` is not installed, tailscaled is not running or not reachable by this user, Tailscale is not connected, or
Taildrop is off for this server.

**The log.** The last 20 sends are kept in `taildrop.json`: when, the device, the file's name, its size, the outcome
and the CLI's message. Never a path or the contents.

## Routes

Under `/api/plugins/hermes-bot-mobile/`, behind the dashboard's sign-in:

| Route | What |
| --- | --- |
| `GET /status` | registered phones, settings, the server's VAPID public key |
| `POST /devices`, `DELETE /devices/{id}` | register or remove a phone |
| `PUT /settings` | each bot on or `off` (`all` and `needs_you` both mean on) |
| `PUT /presence` | what the phone has on screen (no pushes for it) |
| `POST /test` | send a test push |
| `GET /needs-you` | `{live, version, requests: [{id, method, session_id, session_key, profile, title, created_at, params}]}`, oldest first; pushed as `plugin.hermes-bot-mobile.needs_you.changed` when it changes |
| `GET /rooms/{room_id}/activity` | `{members: {member_id: {phase, tool, since}}, now}` |
| `GET /runs-on/key?profile=` | the bot's public SSH line (`null` before one is made) and its private key's path |
| `POST /runs-on/key` | `{profile}`: makes the bot's key pair if it has none |
| `POST /runs-on/check` | `{profile, backend: ssh\|docker, host, user, port, key}`: `{ok, system}` or `{ok: false, reason, detail}` |
| `GET /restart` | read only: `{supported, why, unit, scope, pid, started_at, now, busy: [{id, session, title, profile, status}] \| null, gateway: {route, restart, same_unit}, last, retry_after}` |
| `POST /restart` | `{when: now\|idle, idle_timeout_s: 30-1800 (600), include_gateway: false}`: 202 `{accepted, id, mode, unit, include_gateway}`; 409 `{detail}`, 429 `{detail, retry_after}` |
| `DELETE /restart` | cancels the restart waiting for idle: `{cancelled: true, id}`; 404 when none waits, 409 once it is under way |
| `GET /taildrop` | `{supported, why, targets: [{name, ip, online, detail, you}], you: {name, os, ip} \| null, setup, max_bytes, sending}` |
| `POST /taildrop` | `{target, path?, profile?, session_id?, diff?, name?}` (a `path` or a `diff`): 202 `{id, state: running, …}`; the download route's own 400/403/404, 409, 413, 422, 429 |
| `GET /taildrop/{id}` | `{id, state: running\|done\|failed\|cancelled, sent_bytes, total_bytes, error, note, setup, name, target, …}` |
| `DELETE /taildrop/{id}` | stops a running send: `{cancelled: true, id}`; 404 unknown, 409 already finished |

## Data

`~/.hermes/hermes-bot-mobile/` (the Hermes root, not a profile: one phone, many bots): `devices.json`, `settings.json`,
`vapid.json`, `presence.json`, `restarts.json`, `taildrop.json` (all 0600) and `rooms/<room_id>.json`, plus
`gateway-restart.log` when a Hermes without its own gateway spawner restarts the gateway. On first start it copies `~/.hermes/hermes-push/`
there if this folder has none of its own files yet, so phones registered with `hermes-push` keep getting pushes. A bot's
SSH key is in that bot's own home (see Where a bot runs).

## Tests

    python -m unittest discover -s hermes-bot-mobile/tests

`tests/relay_check.py <ntfy url>` checks that a push relay passes messages through byte for byte;
`tests/integration_hermes.py` loads the plugin through a real Hermes plugin manager, and with Hermes 0.21.5+ blocks a real
kanban card and checks its push. The unit tests fake systemd; `tests/integration_restart.py` is the end-to-end check of
restarting on a Linux server: it restarts a throwaway transient unit of its own (never Hermes) and lists the checks to
make on the real dashboard. The Taildrop tests fake the CLI with its real output formats; with `HERMES_AGENT` set to a
Hermes 0.21.5+ checkout they also ask Hermes's real download route and the plugin the same questions
(`tests/parity_taildrop.py`) and expect the same answers. `tests/integration_taildrop.py` is the end-to-end check on a
Linux server with Tailscale (read only, or `--send <device>` for one small file). `tests/integration_needs_you.py`, with
`HERMES_AGENT` set to a Hermes 0.21.5+ checkout, opens and answers a real approval request through Hermes's gateway and
checks that a connected client was pushed both changes.
