# Retrieve data from the CLI

This guide shows you how to pull content back out of Memex from the
command line. You will run four kinds of retrieval: memory search for
discrete facts, note search for source documents, the vault summary for
a broad sweep, and the entity walk for relationships. Pick the section
that matches what you want.

## Prerequisites

- A running Memex server with content already ingested.
- A working `memex` CLI on your `PATH`.
- A configured active vault (see `memex vault list` to confirm).

Run `memex --help` if any of those is missing.

## Memory search — find a fact

Memory search ranks atomic facts and events across the knowledge graph.
Reach for it when you remember roughly *what* you know but not *which
document* it came from.

```bash
memex memory search "What did we decide about JWT rotation cadence?"
```

The output is a table of memory units with a type tag and a short
preview. Each row points at one fact.

### Narrow the search

Scope to one or more vaults with `--vault` (repeat the flag for several,
or pass `*` for all):

```bash
memex memory search "deploy windows" --vault project-alpha --vault project-beta
memex memory search "deploy windows" --vault "*"
```

Cap the result count with `--limit` (default `5`):

```bash
memex memory search "deploy windows" --limit 20
```

Resolve relative dates ("last week", "the Q4 review") against a fixed
point with `--reference-date`. Pass an ISO-8601 timestamp; the CLI
attaches UTC:

```bash
memex memory search "what we shipped last quarter" --reference-date 2026-04-01
```

Drop retrieval strategies you do not want with the `--no-*` flags. They
turn off semantic vector search, keyword (BM25), entity-graph walk,
temporal scoring, and mental-model retrieval respectively:

```bash
memex memory search "JWT rotation" --no-temporal --no-mental-model
```

By default Memex hides units flagged as stale. Surface them too with
`--include-stale`:

```bash
memex memory search "old build tooling" --include-stale
```

Tighten by classifier:

```bash
memex memory search "vault routing" --intent permanent --risk none
```

`--intent` accepts `permanent`, `durable`, or `ephemeral`. `--risk`
accepts `none`, `sensitive`, `private`, or `safety`.

### Get a synthesized answer

Memex can summarize the top results for you:

```bash
memex memory search "rotation cadence" --answer
```

Add `--json` for machine-readable output, `--minimal` for unit IDs only,
or `--compact` for one line per result.

## Note search — find a source document

Note search returns whole documents ranked against your query. Reach for
it when you want the paragraph in context, not the extracted fact.

```bash
memex note search "deployment pipeline migration"
```

The result table shows score, title, a preview built from block
summaries, and the note ID. Use `memex note view <ID>` to read the full
body.

### Useful flags

Cap the count with `--limit`:

```bash
memex note search "deployment pipeline" --limit 10
```

Scope to vaults:

```bash
memex note search "deployment pipeline" --vault project-alpha
```

Explain *why* each note matched with `--reason`. Memex walks the note's
section tree and points at the relevant headings:

```bash
memex note search "deployment pipeline" --reason
```

Ask for a synthesized answer grounded in the matched sections:

```bash
memex note search "deployment pipeline" --summarize
```

`--summarize` implies `--reason`.

Turn off a retrieval strategy you do not want with `--no-semantic`,
`--no-keyword`, `--no-graph`, or `--no-temporal`. Resolve relative dates
with `--reference-date <iso-8601>`. Enable query expansion (the server
rewrites the query before searching) with `--expand`.

Note search does not currently expose `--include-stale`, an intent or
risk filter, or `--after`/`--before` date windows on the search itself.
For date-bounded discovery, list notes first (`memex note list --after
2026-01-01 --before 2026-04-01`) and search the result set yourself.

## Vault summary — broad sweep

When you want a panoramic view rather than a single answer, ask the
vault for its summary. The summary is pre-built by reflection — themes,
inventory, narrative — and you read it on demand:

```bash
memex vault summary
```

That prints the summary for your active vault. Name a different one as a
positional argument:

```bash
memex vault summary project-alpha
```

The first time you call this on a vault you may see *"No summary exists
for this vault yet."* Build one with `--regenerate`:

```bash
memex vault summary project-alpha --regenerate
```

`--compact` strips the panel chrome and prints only the narrative plus
theme names. `--json` returns the full structured summary.

The broader multi-query survey (decomposes your question into
sub-questions, runs each, merges) is currently MCP-only — call
`memex_survey(query=...)` from an MCP client. There is no `memex memory
survey` command.

## Entity walk — follow relationships

When you want to know *who or what is connected to whom*, walk the
entity graph.

### List entities

```bash
memex entity list
```

The table ranks entities by mention count and shows type and ID. Cap the
list with `--limit` (default `50`) and filter by type with `--type`:

```bash
memex entity list --limit 20 --type Person
memex entity list --type Organization
```

`--type` accepts `Person`, `Organization`, `Location`, `Concept`,
`Technology`, `File`, or `Misc`.

Search by name with `--query`:

```bash
memex entity list --query "Acme Corp"
```

Pass `--slim` to drop per-entity descriptions when you only need names
and IDs.

### Show mentions

Pull the memory units that mention an entity. You can pass a name or a
UUID — Memex tries to disambiguate by exact match, then by single hit:

```bash
memex entity mentions "Acme Corp"
memex entity mentions a3611e98-f618-...
```

Cap with `--limit`. Surface hidden units with `--include-stale`,
`--include-superseded`, or `--include-deprioritized`. These three flags
each open a different door — stale is staleness flag, superseded is
confidence decay, deprioritized is the suppression list. Pass all three
for an audit-shaped view.

### Show related entities

Find the strongest counterparts — entities that co-occur most often:

```bash
memex entity related "Acme Corp"
```

The table is sorted by co-occurrence count, top 20. Add `--json` if you
want to pipe the edges into another tool.

## Verification

Confirm a known fact appears in at least one of the surfaces:

```bash
memex memory search "<topic you ingested>"
memex note search "<topic you ingested>"
```

Both should return rows when the topic is in the vault. If memory search
is empty but note search has hits, extraction has not yet caught up —
wait for the background reflection cycle or check `memex memory reflect`.

## See also

- [Tutorial: Getting started](../tutorials/getting-started.md)
- [How-to: Deprioritize memory units](deprioritize-units.md)
- [Reference: CLI commands](../reference/cli-commands.md)
- [Explanation: retrieval strategies](../explanation/retrieval-strategies.md)
