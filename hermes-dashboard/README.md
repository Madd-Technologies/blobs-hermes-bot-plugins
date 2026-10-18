# hermes-dashboard

Optional. A dashboard for each bot in the HermesBot app. Bots design **collections** (their own typed fields, a status,
history), add and update **records** in them, and build **panels** (views) over them; tapping a pinned bot opens its
panels, with its chat one tap away. Records that need you light the amber dot on the bot's face and show in Needs you.
In the app you change a record's status or fields, mark it done and leave the bot a note; you never add records or edit
panels yourself: the bot does, and reshapes them when you ask.

## What it adds

- **Four tools** for bots, in the `dashboard` toolset:
  - `dashboard_collection`: create, update (add, relabel or retire fields; set its buttons), retire, share with another bot
    (read or write), list, describe.
  - `dashboard_record`: upsert by `source_key` (batches of 100), update, close, reopen, note, get.
  - `dashboard_query`: filter, sort, count/sum/avg, group, with history.
  - `dashboard_view`: set, remove, list, reorder panels on its own dashboard.
- **Live by code, the model only for judgment.** Panels stay fresh without model calls wherever possible:
  - **live panels** read a source when someone looks (a sqlite file in the bot's folder, a JSON URL, a calendar, or a
    script that prints JSON), cached briefly, with no records and no job;
  - **computed panels** (number, chart, table, list, board) work out figures from records on read;
  - **script feeds**: a Hermes `no_agent` cron job runs the bot's script, which writes records with `write.py` (below);
  - **change-gated jobs**: a Hermes `monitor` script (`write.py pending`) wakes the bot's model only when something new
    is waiting; a record still waiting is retried once per `--retry-after` window.
  Each panel says how it is kept, and the tools hint when a bot rewrites the same values or a summary panel too often.
- **Background work: the kanban board on each bot's dashboard.** Hermes's kanban board (`~/.hermes/kanban.db`) is where a
  bot queues work for a new background worker, and a worker that needs you blocks its card (it runs headless and can't
  ask). Whenever the app reads dashboards (at most every 15 s), the open cards assigned to each bot that has this plugin
  are copied into that bot's own **Background work** collection and panel (Queued, Running, Blocked, In review, Done),
  with no job and no model. A **blocked card needs you**: it lights the amber dot and shows in Needs you, with why it is
  blocked. **A note you add on its record is your answer**: it is posted on the card as a comment (signed
  `user (HermesBot app)`) and the card is unblocked, so a new worker picks it up and reads it; the note also works on a
  card still running (the next worker reads it). A finished card's record closes with its summary. The board is the
  truth, but a record you swiped done stays done until its card moves again. Nothing else on the board is changed.
- **`write.py`**: the stdlib command a bot's scripts use (`upsert`, `update`, `close`, `reopen`, `note`, `query`,
  `pending`). It always acts as the profile the job runs as (`HERMES_HOME`); nothing can name another bot.
- **The `hermes-dashboard:dashboard` skill**: turning "keep track of X for me" into a collection and panels, kept fresh by a
  routine that delivers nowhere but the collection; respecting your edits and notes.
- **A short prompt line** so bots know the dashboard is there.
- **Routes for the app** under `/api/plugins/hermes-dashboard/`, behind the dashboard's sign-in:

| Route | What |
| --- | --- |
| `GET /status` | version, schema, how many dashboards |
| `GET /bots` | per dashboard: open records that need you (each once), views, last change |
| `GET /bots/{profile}` | that dashboard's views, computed (a number or chart that adds up a field also says its `unit`, so money and percent figures read `$1,240` and `96%`), their collections' fields, how each is kept, and a version; `?since=<version>` answers `unchanged` without computing; `?fresh=1` reads live sources now |
| `GET /bots/{profile}/needs-you` | the records on it that need you |
| `GET /records/{id}` | a record, its collection, its history and notes, how to head it, its buttons and any job under way |
| `PATCH /records/{id}` | `{"values": {...}}` changed by you |
| `POST /records/{id}/notes` | `{"text": "..."}`; on a kanban card's record also posted on the card (and a blocked card unblocked), answered as `card` |
| `POST /records/{id}/close`, `/reopen` | done, or not done |
| `POST /records/{id}/actions/{button}` | `{"session": "..."}`: you pressed a button; answers the message for the bot |

**Field types:** text, longtext, number, money, percent, date, datetime, checkbox, status (ordered stages, some done),
choice, phone, email, url, link, rating, draft (text the bot wrote for you to send or use, shown in full and editable).
**Buttons:** up to four per collection, each a job the bot does when you press it (Send reply), shown while its filter
matches; the app opens a side chat with the bot and sends it the job and the record, and the record waits on the bot
(no amber) until the bot writes it, or 30 minutes pass. The bot acts only then, with Hermes's approvals as usual.
**Panel kinds:** list, checklist, board (by a status or choice), table, number
(with the previous period), chart (per day, week or month), summary, stat. Row signals are for list, checklist, board and
table; `include_closed` for list, checklist and table (a board keeps done records for `closed_within_days`). A kind drops
keys it doesn't use. Filters are a small structured language
(`{field, op, value}`, `all`/`any`/`not`), never SQL. **Your edits win**: a field you changed is left alone by later bot
writes unless the bot overrides it on purpose.

## Install

Hermes loads user plugins per profile (each bot's own `plugins/` folder and `plugins.enabled`). Install it in the default
profile, which serves the app's routes, and in each bot that should keep a dashboard:

```sh
hermes plugins install Madd-Technologies/blobs-hermes-bot-plugins/hermes-dashboard --ref <commit sha> --enable
hermes -p <bot> plugins install Madd-Technologies/blobs-hermes-bot-plugins/hermes-dashboard --ref <commit sha> --enable
```

Then restart the dashboard (and the messaging gateway, if one runs): plugins load when Hermes starts. A bot's existing chats
learn about the dashboard after `/compress`, or when you name the skill in a message.

## Data

`~/.hermes/hermes-dashboard/dashboard.db` (the Hermes root, whichever bot writes; SQLite in WAL mode, 0600): collections,
sharing, records, every change and note, views, and which record mirrors which kanban card (`kanban_cards`, schema 5).
0.1's per-bot JSON panels are moved into it on first start (kept as a
backup). Nothing leaves the server.

## Tests

    python -m unittest discover -s hermes-dashboard/tests
    ~/.hermes/hermes-agent/venv/bin/python hermes-dashboard/tests/integration_kanban.py [path to a Hermes checkout]
