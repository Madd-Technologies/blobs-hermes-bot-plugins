---
name: dashboard
description: Track and show things for your person on their HermesBot app dashboard (collections you design, panels over them, or panels that read a live source), kept up to date by code wherever possible and by your model only when something needs judgment. Use when they ask you to track, watch, collect, show or keep an eye on something, to show or change a panel, or to answer what is open or waiting.
---

# Your dashboard

Your person has a dashboard for you in the HermesBot app: they open it from your chat (and from your face on Home when you are pinned). It shows **panels**
(views) over **collections** you design and keep, or over a **live source** you point a panel at. Records that need them
light an amber dot on your face and show in their Needs you list. They can open a record, change it, mark it done, mute it
and leave you a note; they never add records or change panels themselves. You do that, and you reshape panels when asked.

Tools: `dashboard_collection`, `dashboard_record`, `dashboard_query`, `dashboard_view`. Scripts use `write.py` (below).

## Keeping a panel live: read this first

**A panel stays up to date by code. Your model is used only when something needs judgment** (writing a draft, deciding
what needs your person, summarising a document, choosing what to do). A routine that wakes you on a timer just to look
again and rewrite a panel costs a full model run every time, even when nothing changed. Before you build any panel, decide
how it stays fresh, taking the **first way that works**:

| Way | Use when | Model runs |
| --- | --- | --- |
| **1. Live panel** | the data already exists somewhere you can read (a file in your Hermes folder, a sqlite database you keep, a JSON URL, a calendar, or a script's output) | none |
| **2. Computed panel** | the data is in a collection: counts, totals, averages, trends, what's open or due | none |
| **3. Script feed** | data has to be fetched and turned into records (mail, voicemail, orders, tickets, files to review, prices, rows of a sheet) | none |
| **4. Change-gated job** | a record needs judgment (a draft, triage, a decision) | only when something new is waiting |
| 5. Timed model routine | nothing above fits | every run: say why you chose it |

Tell your person in one line which way you chose ("This reads the calendar file live; no routine." / "A script checks the
inbox every 5 minutes; I only wake up to draft a reply when a new email needs one.").

Limits: a script feed runs no more often than every 5 minutes; a model job is change-gated, or runs at most hourly; a run
that finds nothing new writes nothing. When you notice one of your routines only moves data, propose the cheaper way and
convert it once they agree, keeping what it tracks.

### 1. Live panels: no records, no job

Give `dashboard_view` `set` a `source` instead of a `collection`, with a list, table, number or chart `kind`. The plugin
reads the source when your person opens the dashboard (cached for `ttl` seconds, default 60) and never otherwise.

- `{"kind": "sqlite", "path": "state/ledger.db", "query": "SELECT subject, received FROM mail ORDER BY received DESC LIMIT 20"}`:
  a file inside your Hermes folder, one read-only SELECT.
- `{"kind": "json", "url": "http://127.0.0.1:8123/api/states", "rows": "data.items"}`: `rows` is the path to the list.
- `{"kind": "ics", "path": "calendars/me.ics", "days": 7}` or a calendar `url`: rows have title, start, end, location, url,
  all_day.
- `{"kind": "script", "script": "disk.py"}`: a script in your `scripts/` folder that prints JSON (a list of objects, or
  `{"rows": [...]}`), in 10 seconds or less. It runs with your `HERMES_HOME`; it reads its own secrets, never put a token in
  the view.

Columns become fields with inferred types (numbers, `YYYY-MM-DD` dates, ISO datetimes, urls); fix one with `fields:
{"start": "datetime"}`. Then `config` works as for collections (`show`, `columns`, `aggregate`, `filter`, `sort`, and
`show.link` for a url a row opens). The `set` answer shows the first rows: check them. A source that fails keeps showing its
last good rows, marked with when they were read. Live panels can't light the amber dot; what needs your person belongs in a
collection (a script feed can fill it).

### 2. Computed panels: let the plugin do the arithmetic

`number`, `chart`, `table`, `list`, `board` and `checklist` panels are computed from records whenever they are shown. A count,
a total, a weekly trend or "what's due this week" never needs a routine, and you never rewrite a `stat` or `summary` panel
to show figures a collection already has (`stat` is for figures with no source at all; `summary` is a few words of judgment).

### 3. Script feeds: records kept by code

Write a small stdlib Python script in your `scripts/` folder that reads the source and writes records through the plugin's
writer, test it once by hand, then schedule it with `cronjob_manage` `create`: `script: "feed-inbox.py"`, `no_agent: true`,
`deliver: "local"`, and a schedule of 5 minutes or more. No model runs.

```python
# scripts/feed-inbox.py: new mail into the support-emails collection, by message id.
import json, os, subprocess
WRITE = os.path.join(os.environ["HERMES_HOME"], "plugins", "hermes-dashboard", "write.py")
messages = fetch_new_messages()  # your own code: an API, IMAP, a file, another script
records = [{"source_key": m["id"], "values": {"customer": m["from"], "subject": m["subject"], "received": m["date"]}} for m in messages]
print(subprocess.run(["python3", WRITE, "upsert", "support-emails"], input=json.dumps(records), text=True, capture_output=True).stdout)
```

`write.py` commands: `upsert <collection>` (stdin: a list of `{source_key, values}`), `update <record_id>` (stdin: values),
`close`/`reopen <record_id>`, `note <record_id> <text>`, `query <collection> [--where JSON]`, `pending` (below). It checks
everything the tools do, your person's edits still win, and the record's history says your script did it. It always acts
as you (the profile the job runs as); there is no way to write as another bot. Then record how the collection is kept:
`dashboard_collection` `update` `feed: {"how": "script", "job": "<cron id>", "every": "5m"}`, which your person sees.

### 4. Change-gated jobs: wake only when there is something to judge

For the part that needs you (drafting replies, filing new documents, triage), create an agent job with `cronjob_manage` and a
`monitor`: a script whose output Hermes compares with the last run, skipping your model entirely when it hasn't changed.
`write.py pending` prints exactly that, a stable list of records waiting for you:

```sh
# scripts/pending-drafts.sh
python3 "$HERMES_HOME/plugins/hermes-dashboard/write.py" pending support-emails --where '{"all": [{"field": "status", "op": "eq", "value": "New"}, {"field": "reply", "op": "empty"}]}'
```

The job's prompt says what to do with the records listed in the "MONITOR CHANGE DETECTED" block (for example "write a draft
reply for each and update the record"). A record still waiting after `--retry-after` (default 6h) shows up again once per
window, so a run that failed is retried a few times a day, not every tick. Record it as `feed: {"how": "model_on_change",
"job": "<cron id>"}`.

### 5. Timed model routines: last resort

Only when nothing above fits: `cronjob_manage` `create` with a self-contained prompt, `skills: ["hermes-dashboard:dashboard"]`,
`deliver: "local"`, hourly or slower, and `feed: {"how": "model_timer", ...}`. Say why in your reply.

## Setting something up

When they ask you to keep track of something, work out (asking only what you can't tell):

1. **The source** and how you'll read it, and check you can read it now.
2. **How it stays fresh**: the first way from the table above that works.
3. For a collection: **what a record is** and its **fields**. Pick the right types: `phone` gives them a Call button,
   `email` a Mail button, `link` is a document (a Google Doc, Sheet or Slides file, a Drive, server or Paperless file, a
   ticket page) shown as a file card that opens where it lives, `date`/`datetime` read as "Fri", `money`, `rating`,
   `checkbox`, `choice`, `draft` for something you wrote for them to send.
4. **A status** with stages ending in done ones (New → Replied → Done), and **what needs them**: the rule that lights the
   amber dot.

Then, in the same turn: create the collection (or test the source), write the records as they are now (each with a
**`source_key`** from the source, so writing it again updates rather than duplicates), set the panels, schedule the script
feed or change-gated job, record the `feed`, and tell them what you track, what will need them and how it stays fresh.

## Choosing panels

- `list`: rows, newest or soonest first. `show: {title, subtitle, meta, link}` picks the fields.
- `checklist`: things they do; ticking one closes it.
- `board`: columns by a status or choice field, for work that moves through stages.
- `table`: a few columns side by side, when comparing matters.
- `number`: one figure (count, sum, average, max), against the last period.
- `chart`: counts or sums per day, week or month, for trends.
- `summary`: a few lines of judgment; never a place to keep numbers up to date.
- `stat`: figures you set yourself, only for data with no source.

Three to six panels that matter beat many. Put what needs them first.

Rows can carry a **signal**, a small green, amber or red dot that answers one question at a glance (has this order
shipped? is this invoice paid?). Give the view `signals`, a list of `{when: <filter>, tone: good|warn|bad}`; the first that
matches a row is its dot, and done or muted rows carry none. For example: good when `shipped` is true, bad when it is
false and `due` is `within_days` 1, warn otherwise. The plugin computes it every time the panel is read, so keep the
fields true and the dots follow; never run a job just to recolor rows. A red row that also needs them shows the red dot.

## Drafts and buttons: offering to act

When you could do the next step for them (reply to an email, book the vendor, file the document), offer it:

- A **`draft`** field holds what you wrote for them to send or use. The record shows it in full with an Edit link; set `to`
  to the field naming who it is for. If they edited it, their version is the one to send.
- A **button** (`actions` on the collection, up to 4) is the next step as a verb: `{"id": "send", "label": "Send reply",
  "prompt": "Reply in the same thread with the draft reply, exactly as written.", "when": {"field": "status", "op": "eq",
  "value": "New"}, "draft": "reply"}`. A button that sends a draft shows only once the draft is written.
- **Never do the step until they press the button.** Pressing it opens a side chat with you that starts with
  `[Dashboard button "<label>" · <record>]`. Do exactly that job (Hermes asks them to approve as usual), then finish on the
  record: update the status and add a note saying what you did, or why you couldn't.

## Reviews and approvals: anything that needs their sign-off

Whenever you have something your person should look at or approve (a report, a quote, a contract, a draft post, an invoice,
photos), give it a record in a review collection: a `link` field to the document where it lives, a short summary of what to
look at, a status such as For review → Changes requested → Approved → Done with `needs_you` on For review, and buttons for
what happens next in your words ("Approve and publish", "Request changes"). If you don't know what approving should do, ask
once. One review collection can hold every kind of document you produce for them.

## Background work: your kanban cards

If you queue work on Hermes's kanban board (`kanban_create`, assigned to yourself), the plugin copies your open cards into
a **Background work** collection on your dashboard by itself. Don't write to it, reshape it or retire it: it follows the
board. A blocked card needs your person there; their note on its record is posted on the card as their answer and unblocks
it, so the next worker reads it. To answer a card yourself, use the kanban tools, not `dashboard_record`.

## Keeping it right

- **One way in:** the tools for you, `write.py` for your scripts. Never open `dashboard.db` or import the plugin's code.
- **Your person's changes win.** If they changed a field, your later writes leave it alone (the result says which were
  kept). Read their changes and notes with `dashboard_query` `include_history: true` and act on them. Only pass
  `override_person` when they ask.
- **One owner per source.** If another bot already tracks the same mail, documents or tasks, don't track them too: link to
  its record, or ask your person which of you owns it.
- Never reopen something they closed unless it truly needs them again; say why in a note.
- A record they **muted** (`muted_until` ahead of now) is theirs to come back to: keep it up to date, but don't raise it.
- Never put passwords, tokens, keys or card numbers in a record or a view. It is shown on a phone.
- Reshape on request ("split it by project", "add a weekly chart", "track the follow-up date too").
- Share with another bot when the work is shared (`dashboard_collection` `share`).
- Answer "what's waiting on me?" from `dashboard_query`, not from memory.
