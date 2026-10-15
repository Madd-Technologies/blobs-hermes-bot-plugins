# hermes-drive

Lets the HermesBot app show Google Drive files a bot finds. The app's viewer reads them through this Hermes server, with the
Google sign-in Hermes's google-workspace skill already keeps; nothing is stored on the server or the phone, and no token ever
leaves the server.

## Set up

1. Install it pinned to a commit and enable it:
   `hermes plugins install Madd-Technologies/blobs-hermes-bot-plugins/hermes-drive --ref <commit sha> --enable` (or Settings → Server
   plugins in the HermesBot app copies a message that has the server's own bot do it).
2. The server needs the google-workspace skill's sign-in: `~/.hermes/google_token.json` with a Drive scope (`drive` or
   `drive.readonly`). The plugin only reads it: it refreshes the access token in memory and never writes the file. A bot in a
   profile uses the main home's token.
3. Restart the dashboard: plugin routes and the skill load when it starts.

## What it adds

Routes under `/api/plugins/hermes-drive/` (behind the dashboard's sign-in), read-only Drive calls only:

| Route | Answer |
| --- | --- |
| `GET /status` | `{configured, drive, account}`, from the token file without asking Google |
| `GET /files/{id}` | `{id, name, mime_type, kind, modified, owner, size, web_link}` |
| `GET /files/{id}/preview` | the file, streamed (a `Range` is passed on); Google Docs, Sheets, Slides and Drawings as PDF |
| `GET /files/{id}/original` | the file; Google files as Office (`.docx`, `.xlsx`, `.pptx`; Drawings as PNG) |
| `GET /search?q=&limit=` | `{results: [{id, name, mime_type, kind, modified, owner, size}]}`, best match first |
| `GET /recent?limit=` | the same, last modified first, no folders |

Google exports a native file up to about 10 MB; bigger ones answer 413. Office files kept in Drive (a `.docx`) stream as they
are, which the app shows as a file it can't preview.

Once the sign-in reaches Drive it also gives every bot the read-only skill `hermes-drive:drive` (`skills/drive/`), and adds one
short paragraph to each new session's prompt that points at the skill and says how to link a file the app can open:

    [Budget 2026](/api/plugins/hermes-drive/files/1AbCdEfGhIjKlMnOpQrStUvWxYz/preview)

The skill's script (standard library only) searches, lists recent files, and prints a Google Doc's text (a Sheet as CSV):

    python3 skills/drive/scripts/find.py search <words> [--limit 8]
    python3 skills/drive/scripts/find.py recent [--limit 8]
    python3 skills/drive/scripts/find.py text <id> [--max-chars 8000]

## Tests

    python -m unittest discover -s hermes-drive/tests
