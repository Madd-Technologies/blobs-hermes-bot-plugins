# hermes-phone

Phone errands for your bots, through your own Twilio number: book a table, get three quotes for a septic pump-out, return
a voicemail. A bot researches first and writes a short call brief; you approve it in the HermesBot app; then a Grok voice
that knows only that brief makes the calls and reports back to the bot.

It calls other people for you. It is not for calling you, and nobody can call in: in this version every inbound call is
turned away.

## How an errand goes

1. You ask a bot ("get quotes from these three septic companies; here's the address and when I'm home"). It researches
   as usual: tank size, likely extras, your calendar.
2. The bot calls `phone_call` with a brief: the goal, an opening line, the facts the caller may give out, the questions
   to ask, what to write down, and who to call. The brief is checked and stored, and you get a push.
3. You read the brief in the app, tick every flagged fact (an address, another phone number, anything on your never-share
   list) or strike it out, and approve. Nothing is dialed before that.
4. The call service calls each number in turn. Every call opens with "Hi, this is an AI assistant calling for <your
   name>. This call is transcribed." Menus get keypad tones, hold music gets silence, and a voicemail gets a short
   scripted message. A call screener (Google Voice, Pixel's Call Screen, the iPhone's) gets the opening line, and the call
   waits for the person to pick up.
5. When the errand is done, the bot gets one fixed line in its main chat and reads the results with `phone_call_report`.
   You get one push.

## What the caller can and can't say

The phone voice is a separate Grok realtime session. The only things it knows are what crosses to the call: your name,
the approved brief for this call, the callback number and your answers to its questions. It has five tools:

- `press_keys`
- `stay_silent`
- `ask_owner`, which asks you in the app, never the bot
- `record_outcome`
- `end_call`

It has no way to reach the bot, your files or the internet, so a pushy caller can't talk it into telling more than the
brief holds.

Checked before you see a brief, and again when you approve it:

- **Never in a brief**, whatever you tick:
  - card numbers
  - Social Security numbers
  - passwords, PINs, one-time and security codes, API keys
  - bank details
  - any money amount (the caller never agrees to pay anything)
- **Flagged for your tick:**
  - anything on your never-share list (matched loosely: case, spacing and punctuation don't hide it)
  - street addresses
  - email addresses
  - other phone numbers
  - birth dates, licence numbers, gate, door and alarm codes
- **Numbers:**
  - US and Canada only.
  - Never premium (900/976), N11 or Caribbean area codes, extensions or letters.
  - Never a number on your do-not-call list, which also grows when someone asks not to be called again.
- **The approval keeps a hash of the brief.** The service refuses to dial a brief that changed afterwards.
- **Limits:**
  - calling hours
  - a daily cap
  - one call at a time
  - a time limit per call (Twilio's own `TimeLimit` a minute past it)
  - a hold limit
  - at most three questions to you per call
- **Test stage first.** Until you switch to live, only your test numbers are dialed.

What came back from a call reaches the bot labeled as words from a third party, as data to weigh and never as
instructions.

**The limit of all this:** these checks guard the path through the bot's tools. A bot that can run any command as the same
user could read the Twilio keys from `~/.hermes/.env` or edit the plugin's files. Keep Hermes's command approvals on for
any bot you give this plugin.

## Needs

- Hermes 0.21.6 or later.
- Your own Twilio account and a voice number. Each person brings their own; nothing is shared.
  - Turn on only the United States and Canada under Voice Geographic Permissions.
  - A trial account works for the test stage, but it can only call verified numbers and plays Twilio's trial notice.
  - Expect some calls to be blocked while the number is new, mostly to mobile numbers (see "Blocked calls" below).
- One of these, for the Grok voice. A bot's [hermes-voice](../hermes-voice) settings pick which, when it has them.
  - the server's Grok sign-in
  - the bot's own `XAI_API_KEY`
- A public HTTPS address that reaches the call service, such as [Tailscale Funnel](https://tailscale.com/kb/1223/funnel).
- [hermes-bot-mobile](../hermes-bot-mobile) 0.1.7 or later, for the approval and question pushes. This is optional, but
  without it you have to open the app to find out that a brief is waiting.

## Install

1. **Twilio keys** go in `~/.hermes/.env` (the default profile's), never in a bot's chat. In the HermesBot app, the default
   bot's Details → Keys adds them as keys of your own.

   ```sh
   HERMES_PHONE_TWILIO_ACCOUNT_SID=AC...
   HERMES_PHONE_TWILIO_AUTH_TOKEN=...
   HERMES_PHONE_TWILIO_PHONE_NUMBER=+1...
   # optional: an API key instead of the auth token for REST calls (the auth token still checks Twilio's signatures)
   HERMES_PHONE_TWILIO_API_KEY=SK...
   HERMES_PHONE_TWILIO_API_SECRET=...
   ```

   Use these names, not Hermes's plain `TWILIO_ACCOUNT_SID` and the rest: Hermes's SMS channel turns itself on when it finds
   those, and a bot could then text from your number with no approval (its telephony skill reads them too).

2. **The plugin**, in the default profile. That serves the app's routes, and gives the default bot the two tools. Then
   restart the dashboard.

   ```sh
   hermes plugins install Madd-Technologies/blobs-hermes-bot-plugins/hermes-phone --ref <commit sha> --enable
   ```

   Hermes loads plugins per profile, so for another bot also run `hermes -p <bot> plugins install …`, and add the bot to
   `bots` in the settings. Only the bots listed there can ask for calls (the default bot is listed to start with).

3. **The call service** runs as its own process: Hermes's plugin routes can't hold Twilio's audio WebSocket. Copy
   `hermes-phone.service` to `~/.config/systemd/user/`, set the two paths (Hermes's own Python, and the plugin's
   `run.py`), then:

   ```sh
   systemctl --user daemon-reload && systemctl --user enable --now hermes-phone.service
   ```

   It listens on `127.0.0.1:8796` only, and writes no access log (every Twilio path carries a call's token).

4. **The public address.** For example, with Funnel on port 8443, so 443 stays free for anything else:

   ```sh
   tailscale funnel --bg --https=8443 8796
   ```

   Set `public_url` to `https://<machine>.<tailnet>.ts.net:8443`. In the Twilio console, set the number's "A call comes
   in" webhook to `<public_url>/twilio/voice`. That makes inbound calls get turned away cleanly instead of hearing
   Twilio's error message.

5. **Check the line** with a real call to your own phone, before any bot makes one: `tests/twilio_check.py` (see Develop).

6. **Settings**, through the app or `PUT /api/plugins/hermes-phone/settings`:
   - `name`, which the caller says it calls for
   - `never_share`
   - `do_not_call`
   - `test_numbers`
   - `public_url`

   Keep `stage` at `test` until a few calls to your own numbers sound right.

## Blocked calls

US carriers score every call, and a personal Twilio account's calls go out with the lowest caller-ID attestation (SHAKEN/STIR
"C"): a full "A" needs a business profile in Twilio's Trust Hub. A new number with a C and a few short calls in a row looks
like a robocaller, so some calls are turned away before they ring, mostly to mobile numbers. The call record says so (SIP
603 or 608) rather than "busy". Businesses' landlines and office phone systems block far less.

- Register the number, free, at [Free Caller Registry](https://www.freecallerregistry.com), which reaches the analytics
  companies behind AT&T, T-Mobile and Verizon.
- Keep calls few and spread out while the number is new; the daily cap and one call at a time help.
- With a business, Twilio's Trust Hub (SHAKEN/STIR, CNAM, Voice Integrity) raises the attestation to A.

## Settings

| Setting | Default | |
| --- | --- | --- |
| `name` | | The name the caller gives ("calling for Alex"). Required. |
| `never_share` | `[]` | Labeled values a call must not say (a home address, a personal cell). Stored here, never sent back to the app, only a hint. |
| `do_not_call` | `[]` | Numbers never dialed. |
| `bots` | `["default"]` | Bots that may ask for calls. |
| `stage` | `test` | `test` dials only `test_numbers`; `live` dials any approved number. |
| `hours` | `[8, 21]` | Calling hours, in the server's local time. |
| `daily_cap` | `10` | Calls started per 24 hours. |
| `max_minutes` / `hold_minutes` | `20` / `15` | Per call, and on hold. |
| `transcripts` | `true` | Keep transcripts, and say so in the opening. |
| `ask_owner` | `true` | Let a call ask you a question it can't answer from the brief. |
| `public_url` / `port` | `""` / `8796` | Where Twilio reaches the service, and where it listens locally. |

## What it does

- **Tools** (toolset `phone`):
  - `phone_call(brief)` stores a brief for your approval.
  - `phone_call_report(errand_id?, transcript?)` reads outcomes.
- **Routes** under `/api/plugins/hermes-phone/`, behind the dashboard's sign-in:
  - `GET status`
  - `GET` and `PUT settings`
  - `GET errands`
  - `GET errands/{id}`
  - `POST errands/{id}/approve`, `decline` and `cancel`
  - `POST errands/{id}/calls/{call_id}/hangup` and `answer`
- **The call service** (`service.py`) dials approved calls, checks Twilio's signature on every webhook and on the audio
  stream, and bridges Twilio's μ-law audio straight to Grok's `audio/pcmu`, with no transcoding. When an errand finishes, it
  tells the bot through Hermes's own Bot Chat delivery.
- **State** lives in `~/.hermes/hermes-phone/`. The folder is private to the user (0700, files 0600):
  - the settings
  - one file per errand, with its brief, outcomes and transcripts
  - the numbers called
  - the service's heartbeat

No audio is recorded. Twilio and xAI each see the call while it happens, under their own terms.

## Develop

```sh
python -m unittest discover -s hermes-phone/tests
```

The tests use fakes for Twilio's REST API, its Media Stream and Grok's socket. `tests/integration_hermes.py` loads the
plugin through a Hermes checkout's own plugin manager, validator and dashboard. It also starts the call service, against a
throwaway `HERMES_HOME` with fake keys.

`tests/twilio_check.py` makes one real call through your Twilio number to a phone you name, with the call service as it
runs on a server but on a throwaway `HERMES_HOME`: answer it and play the company (push it for more than the brief holds,
put it on hold with music). It needs the public address up (for example `tailscale funnel --bg --https=8443 8796`) and an
env file with your Twilio keys, `CHECK_CALL` and `XAI_API_KEY` (or `--mint-cmd`); its docstring has the details.

`tests/live.py` runs the same persona and call brain against real Grok. Run it before the first real call, and after any
change to the persona, the brief or the brain (on the sign-in, it counts against the Grok plan's limits):

- `redteam` plays a pushy or tricky other party, typed or spoken:
  - asking for the instructions
  - pushing for the address
  - pretending to be you
  - asking for a deposit on a card
  - a spoken "system message"
  - asking whether you're human
  - fishing through `ask_owner`
  - another language
  - do-not-call
- `hold` plays a menu, then hold music and a recording, then a person. `--after-opening` plays a person who puts the call
  on hold after the opening line.

Automatic checks fail a case, and anything that needs judgment is listed for review. Read the transcripts as well.

What these runs taught us about xAI's realtime voice shapes `brain.py`:

- **One running transcript.** Transcripts arrive about once a second, with or without commits, and each one carries the
  call's transcript so far, so the brain works from the words each one adds.
- **A commit starts a reply,** even with voice detection off, so hold clears the buffer instead.
- **Stale audio items.** Audio from an item begun while voice detection was off is never committed once detection is
  switched on, so the buffer is cleared at those points.

What real calls taught us about Twilio:

- **Status callbacks aren't retried.** One lost to a tunnel hiccup or a restart is made up for by looking the call up.
- **The final status callback carries `SipResponseCode`**, which tells a carrier block (603, 608) from a busy line.
- **Answering-machine detection reports a call screener as a machine**, so screeners are recognized by what they say.
