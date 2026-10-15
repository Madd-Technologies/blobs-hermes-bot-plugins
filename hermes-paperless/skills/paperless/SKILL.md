---
name: paperless
description: "Find, read and show the user's Paperless-ngx documents (receipts, bills, letters, contracts, manuals): search by words, read a document's text, and link it so the HermesBot app opens it in its viewer."
version: 0.1.0
author: Madd Technologies
license: MIT
metadata:
  hermes:
    tags: [Paperless, documents, receipts, HermesBot]
---

# Paperless documents

Use this when the user asks about their own documents: a receipt, a bill, a letter, a contract, a manual, "that warranty", "my
lease". The documents live in their Paperless-ngx; this server can search and read them, and the HermesBot app can show them.

Everything goes through one script that reads Paperless's address and token itself. Run it with the terminal tool:

## Find

    python3 "${HERMES_SKILL_DIR}/scripts/find.py" search <words> [--limit 8]
    python3 "${HERMES_SKILL_DIR}/scripts/find.py" recent [--limit 8]

`search` is Paperless's full-text search (titles and the documents' text, best match first); `recent` lists the newest. Each hit
comes with its id, date, sender and a ready-made link. Try other words or fewer of them before saying nothing was found.

## Read

    python3 "${HERMES_SKILL_DIR}/scripts/find.py" text <id> [--max-chars 8000]

Prints the document's text (Paperless's OCR) so you can answer questions about it: an amount, a date, an expiry, a clause.
Quote only what the user needs.

## Show

To show a document, put its link in your reply exactly as the script gives it:

    [Dishwasher warranty](/api/plugins/hermes-paperless/documents/15/preview)

The app draws that as a chip and opens the document in its own viewer, streamed through this server. Do not download the file,
save it, or print a path to it, and do not give a Paperless web address instead.

## Rules

- Never read, print or quote the Paperless token or the `.env` file: the script reads them itself.
- Read-only: this skill does not change, tag, upload or delete documents.
- If the script says Paperless is not set up, refused the token or can't be reached, tell the user what it said; don't work
  around it.
