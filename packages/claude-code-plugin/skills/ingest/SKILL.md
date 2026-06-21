---
name: ingest
description: "Pull the content of a local file or a web page (and its assets) into Memex as a note — verbatim or summarized. Use when the user says 'ingest this file/page', 'save this URL to memory', 'add this document to Memex', or hands you a path or link to capture. This stores the raw content as a note (extraction runs server-side) — NOT a gated how-to case (`/extract-case`), and NOT a single shape-routed fact (`/remember`)."
argument-hint: "[path | url] [verbatim|summary]"
---

# /ingest — Capture a file or web page into Memex

You have been invoked via the `/ingest` slash command.

Retrieve the content of what `$ARGUMENTS` points at (plus any assets), then store it via `memex_add_note`. The server ingests and extracts memory units from the note as normal, so the content becomes searchable memory — this is a real ingest, not a dead note.

**Distinct from neighbours:** `/extract-case` is gated and only files reusable how-to *cases*; `/remember` routes a single item by shape. `/ingest` stores a document's *raw content* (verbatim or summarized) as a note, assets attached.

## 1. Resolve the input and retrieve content

`$ARGUMENTS` names a path or URL (and optionally a `verbatim`/`summary` mode token).

- **A URL** → fetch the page with `WebFetch` to get its text/markdown. If the page has **binary assets you want to keep** (images, PDFs, diagrams), `WebFetch` cannot return them — download them with a short `Bash` command (`curl -fsSL <asset-url> -o /tmp/<name>` or `wget`) to local paths, and collect those absolute paths.
- **A local file** → read it with `Read` (read images/PDFs directly too); collect their absolute paths as assets.
- **A local directory** → read the relevant files; ingest as one note (or ask if it should be several).
- **Empty `$ARGUMENTS`** → ask which file or URL to ingest.

## 2. Choose verbatim vs summarized

- Honor an explicit `verbatim` or `summary` token in `$ARGUMENTS`.
- Otherwise default by size: short content → **verbatim**; long content → **summarize** to the key insight (the `markdown_content` field is meant to be concise). Verbatim storage of a large document works but is the non-default path — prefer a faithful summary unless the user asked for verbatim.

## 3. Store via `memex_add_note`

```text
memex_add_note(
  title=<concise title>,
  markdown_content=<verbatim content or summary>,
  description=<one-sentence summary>,
  author="claude-code",
  tags=["ingest", <1-3 topic tags>],
  supporting_files=[<absolute paths of any assets>],
)
```

- The `PreToolUse` hook auto-injects ambient tags and defaults `background=true` / `vault_id` — don't hand-set those.
- Set `note_key` only if the note should be updatable later via `memex_append_note`.

## 4. Report

State the note title, the vault, whether it was stored verbatim or summarized, and which assets were attached.

## Note on scrape fidelity

This path uses `WebFetch`/`Read` for retrieval. For heavyweight document ingestion the CLI's server-side scrape pipeline (`memex note add --url <url>` / `--file <path>`, which uses trafilatura) can extract page content more faithfully — mention it if the page scraped poorly. Extraction into memory units runs either way.
