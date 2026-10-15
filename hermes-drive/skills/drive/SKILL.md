---
name: drive
description: "Find, read and show the user's Google Drive files (Docs, Sheets, Slides, PDFs, pictures): search by words, read a document's text, and link it so the HermesBot app opens it in its viewer."
version: 0.1.0
author: Madd Technologies
license: MIT
metadata:
  hermes:
    tags: [Google Drive, Docs, Sheets, documents, HermesBot]
---

# Google Drive files

Use this when the user asks about something in their Google Drive: a doc, a sheet, slides, a PDF, a picture, "the budget",
"that contract I shared". This server can search and read Drive with the user's Google sign-in, and the HermesBot app can show
the files.

Everything goes through one script that uses the sign-in itself. Run it with the terminal tool:

## Find

    python3 "${HERMES_SKILL_DIR}/scripts/find.py" search <words> [--limit 8]
    python3 "${HERMES_SKILL_DIR}/scripts/find.py" recent [--limit 8]

`search` is Drive's full-text search (names and contents, best match first); `recent` lists the last modified files. Each hit
comes with its kind, date, owner and a ready-made link. Try other words or fewer of them before saying nothing was found.

## Read

    python3 "${HERMES_SKILL_DIR}/scripts/find.py" text <id> [--max-chars 8000]

Prints the text of a Google Doc, Slides or a plain-text file, or a Sheet as CSV, so you can answer questions about it. Quote only
what the user needs. PDFs and pictures have no text here: show their link instead.

## Show

To show a file, put its link in your reply exactly as the script gives it:

    [Budget 2026](/api/plugins/hermes-drive/files/1AbCdEfGhIjKlMnOpQrStUvWxYz/preview)

The app draws that as a chip and opens the file in its own viewer, streamed through this server (Google Docs, Sheets and Slides
as PDFs). Do not download the file, save it, or print a path to it, and do not give a drive.google.com address instead.

## Rules

- Never read, print or quote google_token.json or any token: the script uses the sign-in itself.
- Read-only: this skill does not change, move, share, upload or delete files, and never touches Gmail or Calendar.
- If the script says Drive is not set up, refused or can't be reached, tell the user what it said; don't work around it.
