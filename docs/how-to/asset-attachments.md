# Attach files to a note

Some notes need more than text. A meeting note wants the whiteboard photo; a research note wants the source PDF; a voice memo is the note. This guide shows you how to attach those files to a note, list them, fetch them back, and remove them.

Assets are stored alongside a note's markdown in the file store at `assets/<vault-name>/<note-id>/<filename>`. They travel with the note (export, vault migration, deletion) and are addressable by path, not by UUID.

## Prerequisites

- A running Memex server you can reach with `memex` CLI.
- An existing note. Get its UUID from `memex note list` or `memex note find "<title>"`. You cannot attach assets to a note that does not exist yet.
- The asset files on your local disk.

If you have not configured the CLI against your server, do that first: see [How-to: Configure Memex](configuring-server/default-model.md).

## Procedure

### Option A — attach during note creation

If you are creating the note right now and have the asset to hand, do both in one call:

```bash
memex note add "Whiteboard from Q2 planning" \
  --title "Q2 planning whiteboard" \
  --asset ~/Pictures/whiteboard.jpg \
  --asset ~/Documents/agenda.pdf
```

Repeat `--asset` once per file. The note is created, the files are uploaded, and the response shows the note UUID. Save the UUID — every later command needs it.

### Option B — attach to an existing note

Use `memex note assets add` with the note's UUID and one `--asset` per file:

```bash
memex note assets add 8f3c2a91-4d5e-4b1a-9c8f-1e2d3f4a5b6c \
  --asset ~/Pictures/whiteboard.jpg \
  --asset ~/Documents/agenda.pdf
```

The CLI reports how many files it added and how many it skipped as duplicates. A file is treated as a duplicate when its filename already exists on that note <code-ref path="packages/core/src/memex_core/services/notes.py" lines="527-536" />.

### List the assets on a note

```bash
memex note assets list 8f3c2a91-4d5e-4b1a-9c8f-1e2d3f4a5b6c
```

You get a table with filename, the full asset path, and the guessed MIME type. Add `--json` if you are scripting and want structured output.

The asset path is the value you pass to the get and delete commands — copy it from this table.

### Download an asset

Fetch one asset to stdout, or to a file with `--output`:

```bash
memex note assets get assets/personal/8f3c2a91.../whiteboard.jpg --output ./local-copy.jpg
```

Fetch several at once into a directory with `--output-dir`:

```bash
memex note assets get \
  assets/personal/8f3c2a91.../whiteboard.jpg \
  assets/personal/8f3c2a91.../agenda.pdf \
  --output-dir ./downloads/
```

You cannot combine `--output` with more than one asset path — the CLI refuses and tells you to use `--output-dir` instead.

### Delete an asset

Pass the note UUID first, then one or more asset paths:

```bash
memex note assets delete 8f3c2a91-4d5e-4b1a-9c8f-1e2d3f4a5b6c \
  assets/personal/8f3c2a91.../whiteboard.jpg
```

Deletion removes the file from the store and from the note's asset list. Memory units extracted from the note's text are not touched.

### Resize an image for an agent (MCP only)

If an MCP client (Claude Code, Hermes) fetches an asset and the file is too large to forward inline, the client itself calls `memex_resize_image` on the cached path it received from `memex_get_resources` <code-ref path="packages/mcp/src/memex_mcp/server.py" lines="574-611" />. Allowed input formats are PNG, JPEG, WEBP, and GIF. There is no `memex` CLI command for resize — the operation lives inside the agent's MCP session because it needs the per-session asset cache to be valid.

## Verification

Walk through the round trip once on a throwaway file to confirm the wiring:

1. `memex note add "asset roundtrip test" --asset /tmp/marker.txt` — note the UUID it prints.
2. `memex note assets list <uuid>` — `marker.txt` appears with its full `assets/<vault>/<uuid>/marker.txt` path.
3. `memex note assets get <path> --output /tmp/marker-back.txt` — the file comes back; `diff /tmp/marker.txt /tmp/marker-back.txt` is silent.
4. `memex note assets delete <uuid> <path>` — the CLI reports `Deleted 1 asset(s)` and the next `list` is empty.

If any step fails, jump to troubleshooting.

## Troubleshooting

**"Asset not found" when adding.** The CLI checks the local path before upload. Use an absolute path or run the command from the directory that holds the file. Glob expansion (`*.png`) does not work with `--asset` — repeat the flag for each file.

**"Skipped N duplicate(s)" when adding.** A file with that filename is already attached. Either rename your local file before re-uploading, or delete the existing asset first with `memex note assets delete`.

**Empty list after a successful add.** Confirm you are looking at the same note. The UUID is mandatory and easy to mistype — copy it from the `add` response, do not retype.

**Agent reports "Resource exceeds max size".** MCP `memex_get_resources` refuses files larger than 50 MB <code-ref path="packages/common/src/memex_common/asset_cache.py" lines="57-58" />. For images, the agent should call `memex_resize_image` on the cached path before forwarding. For PDFs and other non-image assets above that limit, download with the CLI (`memex note assets get`) and feed the local file to the agent another way; MCP will not stream the bytes back.

**"Too many paths requested".** `memex_get_resources` accepts at most 50 paths per call <code-ref path="packages/common/src/memex_common/asset_cache.py" lines="58" />. Batch into smaller calls.

**MIME type shown as `-`.** The CLI guesses MIME from the filename suffix using Python's `mimetypes` library; an unknown extension shows a dash. The asset still uploads and downloads correctly — the dash is cosmetic, not an error. Add the right extension to the filename before uploading if you want the table to populate.

**Resize rejects an image.** `memex_resize_image` only accepts paths returned by `memex_get_resources` from the current session, and only PNG / JPEG / WEBP / GIF inputs <code-ref path="packages/common/src/memex_common/asset_resize.py" lines="21" />. Arbitrary filesystem paths are refused on purpose — the cache boundary is the security model.

## See also

- [Tutorial: Getting started](../tutorials/getting-started.md)
- [How-to: Ingest data](ingesting-data.md)
- [Reference: MCP tools](../reference/mcp-tools.md)
- [Explanation: architecture overview](../explanation/how-memex-works/high-level-architecture.md)
