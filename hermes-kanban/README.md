# hermes-kanban

Kanban that sorts itself out. Hermes's kanban board runs cards in background workers, but when a card blocks, nobody is
told: it sits on the board until someone happens to look, its worker's files may not be readable where it runs, and a card
that keeps blocking keeps costing model runs. This plugin adds what was missing, using only Hermes's own kanban hooks and
board code. Works on Hermes 0.21.6 and 0.22.

## What it does

- **A blocked card goes back to the bot that set it up.** When a card blocks because it needs an answer or is missing
  something (`needs_input`, `capability`, or no kind at all), the bot that created it is woken in its own **Kanban desk**
  chat with the card, why it blocked and the latest comments. That bot knows why the card exists, so it can often answer
  it and unblock it, re-plan, or make a card for another bot and link it as a parent (the blocked card then waits for it).
  What only the person can decide, it leaves on the card as a comment starting with "Question for you:", which the app
  shows on the blocked card in Needs you. A card a person made goes to its own bot instead; a card no bot on the server
  made or works is left to the person.
- **A card that keeps blocking asks you once.** Hermes already moves a card to triage, where no bot can unblock it, when it
  blocks a second time for the same kind of reason. Such a card is never handed to a bot again; the person is asked through
  the push and Needs you that cover triaged cards. A card's bots get two wakes (for two different blocks) before the person
  is asked instead.
- **A flaky block is retried once.** A card that blocks as `transient` (a timeout, a site that was down) used to wait for a
  person like any other. It is now unblocked once, ten minutes later, with a comment saying so, and no model run. If it
  blocks the same way again, Hermes moves it to triage.
- **A card's files reach its worker.** Hermes tells a worker about its card's attachments by their path on the server, which
  a worker whose terminal runs in Docker (or on another machine) cannot open, and it doesn't mention the files of the cards
  it waits for at all. Right before a worker starts, the card's files and its parent cards' files are copied (never moved)
  into the workspace, under `.kanban-files/<card>/` (parents' under `from-<parent>/`), and one comment on the card lists
  them by their path from the workspace, which Docker mounts at `/workspace`. Files over 64 MB (or 256 MB in all) stay
  where they are and the comment says so. A git worktree workspace is left alone.
- **Hooks for other plugins.** Register for them with `ctx.register_hook` (they are Python-plugin hooks; Hermes's shell
  hooks only take its own event names). Every one gets `task_id` and `board`:

  | Hook | Fires in | Also gets |
  | --- | --- | --- |
  | `kanban_card_blocked` | the process that blocked the card (usually its worker) | `title`, `assignee`, `created_by`, `kind`, `recurrences`, `reason`, `route` (`wake`, `retry`, `loop` or `skip`), `target` |
  | `kanban_card_looping` | the same, once per card | the same |
  | `kanban_card_files_staged` | the dispatcher (the gateway) | `workspace`, `files` (paths from the workspace), `left` |
  | `kanban_card_woke` | the gateway | `target`, `ok`, `reply` (what the bot said it did) |
  | `kanban_card_retried` | the gateway | `retried` |
  | `kanban_card_done` | the process that completed the card | `assignee`, `run_id`, `summary` |

## How it runs

A card blocks inside its worker, which exits right after, so the worker only writes a small request file under
`~/.hermes/hermes-kanban/requests/`. A runner in the messaging gateway (the process that also runs the kanban dispatcher)
reads them every few seconds and carries them out, after checking that the card is still blocked:

- On **Hermes 0.21** the wake is a one-shot turn in the bot's "Kanban desk" chat (`hermes -p <bot> chat -c "Kanban desk" -Q`),
  taken under the bot's turn lock like any bot-to-bot delivery, with that bot's own environment. A one-shot turn follows
  `approvals.single_query_mode` for dangerous commands and cannot wait for a person; the desk's work needs neither.
- On **Hermes 0.22** it goes through the gateway's bot-to-bot door (`a2a.forward`) into one desk conversation per bot, from
  "Kanban desk". Hermes titles that chat from its first message ("[Kanban desk] Card …"). A wake retried after a restart
  reuses its input id, so the bot never gets it twice.

Each outcome, with the bot's reply, is a line in `~/.hermes/hermes-kanban/desk.log`; which blocks were already handed over
is in `woken.json` next to it. `HERMES_KANBAN_DESK=off` (in the gateway's environment) stops wakes and retries; the files and
hooks keep working.

## Install

In the **default profile** (for the dispatcher and the desk runner in the gateway) and in **each bot that works cards**:
Hermes loads a worker's plugins from its own bot, and a card blocks inside its worker.

```sh
hermes plugins install Madd-Technologies/blobs-hermes-bot-plugins/hermes-kanban --ref <commit sha> --force --enable
hermes -p <bot> plugins install Madd-Technologies/blobs-hermes-bot-plugins/hermes-kanban --ref <commit sha> --force --enable
```

Then restart the messaging gateway and the dashboard. A woken bot acts on cards with its kanban tools when it has them
(`hermes -p <bot> tools enable kanban`), and with `hermes kanban` in its terminal otherwise.

## Develop

```sh
python -m unittest discover -s hermes-kanban/tests
~/.hermes/hermes-agent/venv/bin/python hermes-kanban/tests/integration_kanban.py [path to a Hermes checkout]
```

The integration test loads the plugin through Hermes's plugin manager in a throwaway `HERMES_HOME` and drives real cards
through Hermes's board code (no model runs). To try a real desk turn, run a gateway on a throwaway home with a model, block
a card (`hermes kanban block <card> "<reason>" --kind needs_input`) and read `desk.log`. A throwaway home has no dependency
generation for Hermes's published launcher; set `HERMES_KANBAN_HERMES_CLI` to the venv's `hermes` there.
