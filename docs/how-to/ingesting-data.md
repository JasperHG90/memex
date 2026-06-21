# Ingest data into Memex

Pick the mode that matches what you have in front of you. A single sentence you want to keep? Use the inline form. A folder of PDFs from a research dump? Use file or directory ingest. A live notes folder you keep editing? Use sync. Driving Memex from your own Python program? Use the API.

Memex runs the same pipeline behind all four routes — format conversion, chunking, fact extraction, embedding, linking. The only thing that changes is how you hand the content to it.

## Prerequisites

- A running Memex server. Start one with `memex server start`.
- A vault to write into. Create one with `memex vault create <name>` (or pass an existing vault to `--vault`).

## Add a single note inline

For a thought, a quote, a meeting takeaway — anything you can type in one sitting.

```bash
memex note add "We chose Postgres over SQLite because we need pgvector and FTS in the same store."
```

The CLI prints the new note's UUID and the count of memory units the extractor pulled out:

```
Adding Note
Note added successfully! UUID: 7d2a9c4f-...
Extracted 3 memory units.
```

Add structure with the metadata flags:

```bash
memex note add "Q2 OKR: ship the retrieval evaluation harness." \
  --title "Q2 OKR — retrieval eval" \
  --tag planning --tag okr \
  --date 2026-04-01 \
  --vault work
```

Useful flags:

- `--vault, -v` — name or UUID of the vault. Defaults to the active write vault from your config.
- `--key, -k` — stable identifier for the note. Re-running with the same key updates the existing note rather than creating a new one. Use this when you want repeatable ingestion (a daily log, a recurring report).
- `--tag` — repeatable. One tag per flag.
- `--title, -t` — note title. Defaults to `Quick Note` for inline content.
- `--date` — ISO 8601 publish date. Backdates the note's facts so they sort correctly in temporal retrieval.
- `--author` — author name written to the note's metadata.
- `--user-notes, -n` — your own commentary about the note. Extracted with `source_context='user_notes'` so it's distinguishable from the body.
- `--description, -d` — short summary.
- `--background, -b` — queue the work and return a job ID instead of waiting.

## Add a file or a folder

Point `--file` at any supported file and Memex handles the format conversion server-side.

```bash
memex note add --file ./research-notes.md --vault research
```

Supported formats: Markdown, PDF, DOCX, PPTX, XLSX, CSV, JSON, XML, HTML, plain text, and Outlook mail (`.msg`, `.eml`). PDFs go through PyMuPDF and pull in embedded images as assets; everything else goes through Microsoft MarkItDown.

To ingest a whole directory, point `--file` at the folder:

```bash
memex note add --file ./customer-interviews/ --vault research
```

Memex walks the directory recursively, skips hidden files (anything starting with `.`), and uploads every remaining file. The CLI waits for the whole batch to finish before returning.

For directories with a lot of files, queue the work in the background so your terminal frees up:

```bash
memex note add --file ./customer-interviews/ --background --vault research
```

The CLI prints a job ID you can poll:

```
Accepted. Ingestion running in background.
```

To attach an image or PDF to a specific note (rather than ingest the file as a note in its own right), use `--asset` with a single-file `--file`:

```bash
memex note add --file ./report.md --asset ./diagram.png --vault research
```

`--asset` requires a single file as `--file`; pointing `--file` at a directory while passing `--asset` is rejected.

## Append to an existing note

Extending a note is not the same as ingesting it again. `memex note append` adds a delta to an existing note in place — only the new chunks invoke the LLM, and the rest of the note stays untouched.

```bash
memex note append --key daily-2026-05-21 \
  --delta "Afternoon update: the migration finished at 14:32 UTC."
```

You can identify the note by `--key` (preferred — survives re-ingest) or by a positional UUID. Pass `--delta` for a one-liner, `--delta-file` to read from disk, or pipe through stdin:

```bash
cat afternoon-log.md | memex note append --key daily-2026-05-21
```

The `--joiner` flag controls how the delta attaches to the parent body: `paragraph` (default, blank line), `newline`, or `none`.

## Sync a folder continuously

For a notes folder you keep editing — an Obsidian vault, a journal, a research directory — `memex note sync` keeps Memex aligned with what's on disk. Memex tracks every file in a SQLite database under your folder and only re-ingests what changed.

Initialise the sync config:

```bash
memex note sync init ~/my-notes
```

This drops a `note-sync.toml` in the folder. Edit it to set the target vault, asset rules, or exclusions (the file is documented inline).

Run the first full sync:

```bash
memex note sync run ~/my-notes --full
```

`--full` re-scans everything regardless of state. After the first run you can omit it — Memex only processes new, modified, or deleted files.

Preview without writing:

```bash
memex note sync run ~/my-notes --dry-run
```

For continuous sync, watch the folder:

```bash
memex note sync watch ~/my-notes
```

Watch mode picks up file-system events through `watchdog`. On a network drive or NFS mount where events don't fire, switch to polling:

```bash
memex note sync watch ~/my-notes --mode poll
```

Skip a specific note by adding `agents: skip` to its YAML frontmatter. Route a note to a different vault with `vault: <name>` in the frontmatter — Memex archives the old version and re-ingests into the new vault. Both keys are configurable in `note-sync.toml`.

Useful flags on `sync run`:

- `--full` — ignore sync state, re-scan everything.
- `--dry-run` — report what would change without touching Memex.
- `--background, -b` — submit a batch job and return immediately. Check progress with `memex note sync job <job_id>`.
- `--no-handle-deletes` — leave Memex alone when local files disappear.
- `--hard-delete` — permanently remove notes when local files disappear (default is to archive them).

## Drive ingestion from Python

For programmatic use, talk to `MemexAPI` directly. The relevant method is `MemexAPI.ingest(note, vault_id=..., background=False)` <code-ref path="packages/core/src/memex_core/api.py" lines="1173-1182" />. It takes a `NoteInput` <code-ref path="packages/core/src/memex_core/api.py" lines="173-211" /> and returns a dict with the new note ID and extracted unit IDs.

```python
import asyncio
from memex_core.api import MemexAPI, NoteInput
from memex_core.config import MemexConfig

async def main() -> None:
    config = MemexConfig.load()
    async with MemexAPI.from_config(config) as api:
        note = NoteInput(
            name='Migration retro',
            description='Notes from the post-migration retro on 2026-05-21.',
            content=b'The migration completed at 14:32 UTC. Three issues hit: ...',
            tags=['retro', 'migration'],
            note_key='retro-2026-05-21',
        )
        result = await api.ingest(note, vault_id='work')
        print(result['note_id'], result['unit_ids'])

asyncio.run(main())
```

`background=True` is rejected by the in-process API — it's accepted only for signature parity with the HTTP wrapper. For background work, talk to the server through `RemoteMemexAPI` or the `/ingestions/batch` REST endpoint instead.

For file or URL ingestion from Python, use `api.ingest_from_file(path, vault_id=...)` <code-ref path="packages/core/src/memex_core/api.py" lines="1156-1171" /> or `api.ingest_from_url(url, vault_id=...)` <code-ref path="packages/core/src/memex_core/api.py" lines="1139-1154" />.

## Verify your content is in

Search for any phrase that should appear in what you ingested:

```bash
memex note search "post-migration retro" --vault work
```

The result table shows matching notes with their titles, scores, and IDs. If you remember the title fragment, `memex note find` does trigram matching on titles only — faster when you already know what you're looking for:

```bash
memex note find "retro" --vault work
```

To see what extracted from a specific note:

```bash
memex note view <note-id>
```

## Troubleshooting

**Unsupported file format.** Memex covers Markdown, PDF, DOCX, PPTX, XLSX, CSV, JSON, XML, HTML, plain text, and Outlook (`.msg`, `.eml`). Anything else — images on their own, audio, video, archives — fails extraction. Convert to one of the supported formats first, or attach the file as an asset to a Markdown note with `--asset`.

**A single file is taking too long.** Large PDFs and dense PPTX decks can take a minute or more because PyMuPDF and MarkItDown both walk the file structure before extraction starts. Add `--background` to queue the work and free your terminal. The CLI prints the job ID; check progress through your server logs or, for sync jobs, with `memex note sync job <job_id>`.

**Folder sync is skipping files I want.** Three things cause silent skips. First, the file extension is not in `include_extensions` in `note-sync.toml` — add it there. Second, the file or one of its parent directories matches an entry in `[sync.exclude].base` or `extends_exclude`. Third, the file has `agents: skip` in its YAML frontmatter (configurable as `frontmatter_skip_key` / `frontmatter_skip_value`). Run `memex note sync status ~/my-notes` to see which files Memex sees as changed and which it ignores.

**Vault not found.** `--vault` accepts a vault name or a UUID. Names are case-sensitive and must match exactly what you used with `memex vault create`. List the vaults you have with `memex vault list`. If you meant the default vault, drop `--vault` entirely — Memex uses the `write_vault` from your config (`~/.config/memex/config.yaml`).

## See also

- [Tutorial: Get started with Memex](../tutorial/getting-started.md)
- [How-to: Attach files to a note](asset-attachments.md)
- [Reference: CLI commands](../reference/cli-commands.md)
- [Explanation: About the extraction pipeline](../explanation/how-memex-works/extraction.md)
