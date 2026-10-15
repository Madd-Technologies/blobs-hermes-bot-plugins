# hermes-paperless

Lets the HermesBot app show Paperless-ngx documents a bot finds, and adds a Paperless page to Hermes itself: a tab in the web
dashboard and a page in Hermes Desktop, to search Paperless and look at a document. Everything reads through this Hermes server;
nothing is stored on the server or the phone, and the Paperless token never leaves the server.

## Set up

1. Install it pinned to a commit and enable it:
   `hermes plugins install Madd-Technologies/blobs-hermes-bot-plugins/hermes-paperless --ref <commit sha> --enable` (or Settings → Server
   plugins in the HermesBot app copies a message that has the server's own bot do it).
2. Put Paperless's address and an API token in `~/.hermes/.env` (or the dashboard's environment):
   `PAPERLESS_URL=http://paperless.lan:8000` and `PAPERLESS_TOKEN=...`. The names a Paperless skill often uses work too:
   `PAPERLESS_BASE_URL`, `PAPERLESS_API_URL`, `PAPERLESS_HOST`; `PAPERLESS_API_TOKEN`, `PAPERLESS_API_KEY`.
3. Restart the dashboard: plugin routes and the skill load when it starts.

## What it adds

Routes under `/api/plugins/hermes-paperless/` (behind the dashboard's sign-in):

| Route | Answer |
| --- | --- |
| `GET /status` | `{configured, host}` |
| `GET /documents/{id}` | `{id, title, created, original_file_name, mime_type, page_count}` |
| `GET /documents/{id}/preview` | the document as a PDF, streamed (a `Range` header is passed on) |
| `GET /documents/{id}/thumb` | a picture of the first page |
| `GET /documents/{id}/original` | the file as it was added |
| `GET /search?q=&limit=` | `{count, results: [{id, title, created, correspondent, snippet}]}`, best match first |
| `GET /recent?limit=` | the same, newest first |
| `GET /documents/{id}/preview-data` | `{mime_type, size, data_url}`: the preview as JSON (up to 30 MB), for Desktop |

Once Paperless is set up it also gives every bot on the server the read-only skill `hermes-paperless:paperless`
(`skills/paperless/`), and adds one short paragraph to each new session's prompt that points at the skill and says how to link a
document the app can open:

    [Dishwasher warranty](/api/plugins/hermes-paperless/documents/15/preview)

The app draws such a link as a chip under the reply and opens it in its PDF viewer.

## In Hermes: dashboard tab and Desktop page

- **Web dashboard:** a Paperless tab (after Files): search, newest first until you type, and a viewer beside the list (the
  browser's own PDF viewer; pictures and text too) with "Download original". `dashboard/dist/index.js` and `style.css`, plain JS
  against the dashboard's plugin SDK, colored by the theme's variables.
- **Hermes Desktop:** a Paperless row in the sidebar opens the same page (`desktop/plugin.js`, plain ESM importing only the
  plugin SDK and React, as Desktop's lint requires). A Desktop plugin reads its routes as JSON, so documents come from
  `preview-data`; there is no download there.

## The skill

Plugin skills are not in a prompt's skill index (bots find them with `skills_list`, or through the paragraph above), so a bot
without a Paperless skill of its own can still search, read and link documents. The skill runs one script, standard library only,
that reads Paperless's address and token the same way as the routes (plus the main Hermes home's `.env` for a bot in a profile)
and never prints the token:

    python3 skills/paperless/scripts/find.py search <words> [--limit 8]   # full-text, best match first
    python3 skills/paperless/scripts/find.py recent [--limit 8]
    python3 skills/paperless/scripts/find.py text <id> [--max-chars 8000] # the document's OCR text

Each hit comes with its id, date, sender, where the words matched, and its link ready to paste. Nothing is changed in Paperless.

## Tests

    python -m unittest discover -s hermes-paperless/tests
