# Pick between note search and memory search

This tutorial walks you through three retrieval tools on the same small corpus: `memex note search` (chunk-level passages), `memex memory search` (extracted facts), and `memex_survey` (panoramic synthesis). By the end you will have run all three against one ingested document, compared the shapes of their results, and have a feel for when each is the right tool.

Plan on 20 minutes from a working Memex install.

## Prerequisites

- **A running Memex server** with PostgreSQL reachable. If you don't have one yet, finish [Get started with Memex](getting-started.md) first.
- **The `memex` CLI on your PATH.** Verify with `memex --version`.
- **An LLM API key configured.** Set `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or your provider's variable so extraction can run. Without one, `memex memory search` will still execute but you will have no facts to find.
- **About 200KB of free disk for the sample note.** The tutorial uses one Markdown document.

## Step 1: Ingest a small corpus

Save a sample research-paper-shaped note to a file. You'll use this as the single source document for every search in the rest of the tutorial.

Create `delivery-window-study.md` somewhere on disk:

```markdown
# A small study of delivery-window scheduling

## Background

Friday afternoon deploys correlate with weekend incidents. The internal
postmortem index shows 23 of 41 production incidents in the last year
started after a deploy shipped between 14:00 and 18:00 on a Friday.

## Method

We retrieved the timestamp of every Tier-1 production incident from the
incident tracker between 2024-01-01 and 2024-12-31. For each incident,
we joined the most recent deploy event within a 6-hour window before
the page fired.

## Findings

Of the 41 Tier-1 incidents in 2024, 23 (56%) followed a Friday-afternoon
deploy. The next-largest category was Monday morning at 7 (17%). The
remaining 11 spread across other windows.

Deploys shipped between 09:00 and 12:00 on Tuesday, Wednesday, or
Thursday correlated with 0 Tier-1 incidents over the same period.

## Recommendation

Move the default deploy window to Tuesday or Wednesday morning. Block
Friday-afternoon deploys behind an explicit override switch with a
manager sign-off.

## Follow-up

A 2025-Q1 review will check whether the new window reduces weekend
incidents. We expect a measurable drop within one quarter.
```

Ingest it as a note:

```bash
memex note add --file delivery-window-study.md --title "Delivery-window study"
```

You should see output similar to:

```
Adding Note
[cyan]Ingested:[/cyan] Delivery-window study
note_id: 7a1c... (varies)
note_key: delivery-window-study
```

Memex splits the file into chunks, sends each chunk to the extractor, and persists the resulting memory units. Wait about a minute for extraction to settle before moving on. You can poll progress with `memex stats` if you're impatient.

## Step 2: Search with note search

Note search returns passages from your source documents. It's the right tool when you want to read what the document actually says, in context.

Run a content lookup:

```bash
memex note search "Friday afternoon deploys"
```

You'll see a table like this:

```
Active strategies: semantic, keyword, graph, temporal
Search Results: "Friday afternoon deploys"
┌────────┬─────────────────────────┬────────────────────────────────────┬────────────┐
│ Score  │ Title                   │ Preview                            │ ID         │
├────────┼─────────────────────────┼────────────────────────────────────┼────────────┤
│ 0.847  │ Delivery-window study   │ Background | Findings              │ 7a1c...    │
└────────┴─────────────────────────┴────────────────────────────────────┴────────────┘
```

The preview shows section topics (`Background`, `Findings`) because note search returns chunks tied to the document's section structure. The score is the post-fusion ranking score: semantic similarity blended with keyword (BM25), entity-graph, and temporal channels through reciprocal rank fusion <code-ref path="packages/core/src/memex_core/server/notes.py" lines="154-170" />.

Now ask for *why* a section matched:

```bash
memex note search "Friday afternoon deploys" --reason
```

The `--reason` flag activates skeleton-tree identification — Memex reads the document's heading structure and names which sections are relevant. You should see section IDs and a one-line reasoning string per matched section.

For a synthesised answer grounded in those sections, add `--summarize`:

```bash
memex note search "Friday afternoon deploys" --summarize
```

This costs an LLM call. The answer cites the sections it pulled from.

**What note search is for.** Read this output and ask: did I get the *passage* I wanted? If yes, note search is the right tool. Use it when you have a specific document in mind, or when you need the original phrasing and surrounding context.

## Step 3: Search with memory search

Memory search returns extracted facts, not passages. It's the right tool when you want the *claim* a document makes, decoupled from where the document said it.

Run the same query through memory search:

```bash
memex memory search "Friday afternoon deploys"
```

The output is a different shape:

```
Searching: Friday afternoon deploys
Search Results (3)
┌────────┬─────────────────────────────────────────────────────────┬─────────────────────────┐
│ Type   │ Memory                                                  │ Source                  │
├────────┼─────────────────────────────────────────────────────────┼─────────────────────────┤
│ fact   │ 23 of 41 Tier-1 production incidents in 2024 followed   │ Delivery-window study   │
│        │ a Friday-afternoon deploy.                              │                         │
│ fact   │ Deploys shipped Tuesday–Thursday morning correlated     │ Delivery-window study   │
│        │ with 0 Tier-1 incidents over the same period.           │                         │
│ event  │ A 2025-Q1 review will check whether the new window      │ Delivery-window study   │
│        │ reduces weekend incidents.                              │                         │
└────────┴─────────────────────────────────────────────────────────┴─────────────────────────┘
```

Each row is one *memory unit* — a single claim the extractor lifted out of the document. The `Type` column shows whether the extractor classified the unit as a `fact` (a stable claim), an `event` (something that happened at a time), or another category.

Memory search runs five strategies in parallel — semantic similarity, keyword match (BM25), entity-graph traversal, temporal recency, and mental-model lookup — then fuses their rankings with reciprocal rank fusion <code-ref path="packages/core/src/memex_core/memory/retrieval/engine.py" lines="528-536" />. You can narrow the search by disabling individual strategies:

```bash
memex memory search "Friday afternoon deploys" --no-mental-model
```

This skips the mental-model strategy. Useful if reflection hasn't run yet and you only want raw facts.

**What memory search is for.** Read the output and ask: did I get the *claim* I needed? Memory search synthesises across documents — if three different notes all said "Friday deploys are risky", memory search would return one fact-shaped result, not three duplicate passages. Use it when you want the answer to a question, regardless of which document held it.

## Step 4: Run a broad survey

For panoramic questions — "tell me everything about X", "what do you know about Y" — a single memory search misses angles. `memex_survey` decomposes the query into 3–5 sub-questions, runs them in parallel, deduplicates, and groups results by source note <code-ref path="packages/core/src/memex_core/server/survey.py" lines="20-40" />.

Survey lives on the MCP surface and the HTTP API. **There is no `memex memory survey` CLI command today** — the CLI exposes search and read operations, not the survey decomposition.

To run survey from the command line, call the HTTP endpoint directly. Assuming the server is on port 8000:

```bash
curl -X POST http://localhost:8000/api/v1/survey \
  -H "Content-Type: application/json" \
  -d '{"query": "give me everything about the delivery window study"}'
```

The response is JSON. A trimmed shape:

```json
{
  "query": "give me everything about the delivery window study",
  "sub_queries": [
    "What were the findings of the delivery window study?",
    "What method did the delivery window study use?",
    "What recommendation came out of the delivery window study?"
  ],
  "topics": [
    {
      "note_id": "7a1c...",
      "title": "Delivery-window study",
      "fact_count": 6,
      "facts": [
        {"id": "...", "text": "23 of 41 Tier-1 production incidents...", "fact_type": "fact"},
        {"id": "...", "text": "Deploys shipped Tuesday-Thursday...", "fact_type": "fact"},
        {"id": "...", "text": "A 2025-Q1 review will check...", "fact_type": "event"}
      ]
    }
  ],
  "total_notes": 1,
  "total_facts": 6,
  "truncated": false
}
```

Notice three things. First, the sub-questions are written for you — Memex decomposed the broad ask into focused queries. Second, facts are grouped by source note, not interleaved. Third, with one source note in the vault you get one topic; on a real vault you would see several.

From an MCP-aware agent (Claude Code, Hermes), the same call is `memex_survey(query="give me everything about the delivery window study")`. The arguments match the HTTP body field-for-field.

**What survey is for.** Read the output and ask: did I get a *map* of the topic? Survey is the right tool when the user's question is wide — "everything about X" — and a single search would only catch one slice.

## Step 5: Filter memory search by date

Temporal filtering changes how each tool behaves. The CLI exposes only one knob: `--reference-date`, which anchors relative date phrases ("last week", "yesterday") to a specific point in time. The HTTP and MCP surfaces additionally expose `after` and `before` as hard date filters <code-ref path="packages/common/src/memex_common/schemas.py" lines="299-302" />.

Try `--reference-date` first. Suppose you want to resolve "last quarter" against the start of 2025:

```bash
memex memory search "what happened last quarter" --reference-date 2025-01-01
```

The query expander reads "last quarter" relative to `2025-01-01` (so: Q4 2024) instead of relative to today. Without `--reference-date`, it would anchor to now.

For hard date filtering with `after` / `before`, use the HTTP API:

```bash
curl -X POST http://localhost:8000/api/v1/memories/search \
  -H "Content-Type: application/json" \
  -d '{
    "query": "Friday afternoon deploys",
    "after": "2024-06-01T00:00:00Z",
    "before": "2024-12-31T23:59:59Z"
  }'
```

The response only contains memory units whose `event_date` falls in the window. For the delivery-window-study note above, all three units fall inside this range — but the filter is the same one that would exclude a 2023 note.

`memex_survey` and `memex_memory_search` (MCP) both expose the same `after` / `before` / `reference_date` parameters. The shape is identical to the HTTP API.

## Step 6: Compare the three responses

You now have three results from the same corpus. Look at each one and notice what changed.

| | `memex note search` | `memex memory search` | `memex_survey` |
|---|---|---|---|
| **Granularity** | Chunks / sections | Atomic facts | Facts grouped by source |
| **What it returns** | Passages with section names | Standalone claims | A map of the topic |
| **Best for** | "Find the passage where X is said" | "What's the claim about X?" | "Give me everything about X" |
| **Surface** | CLI + MCP + HTTP | CLI + MCP + HTTP | MCP + HTTP (no CLI) |
| **Temporal filters** | `--reference-date` (CLI); `after`/`before` (API) | `--reference-date` (CLI); `after`/`before` (API/MCP) | `after`/`before`/`reference_date` (API/MCP) |
| **LLM calls** | 0 (default); 1 with `--summarize` | 0 (default); 1 with `--answer` | 1 to decompose; 1 per sub-question if reranker is LLM-backed |

A practical decision rule:

- **Specific lookup → note search.** "Show me the section about deploy windows."
- **Single fact, narrow question → memory search.** "What's the rate of Friday-deploy incidents?"
- **Wide topic, multiple angles → survey.** "Tell me everything about delivery windows."

When you're unsure, run both `memex memory search` and `memex note search` in parallel. The default modes are cheap — no LLM call — and their outputs answer different questions about the same query.

## What you built

You ingested one document, ran three retrieval tools against it, and saw three different result shapes for the same query string. You know how to:

- Pull passages from a source document with `memex note search`.
- Pull extracted claims from across notes with `memex memory search`.
- Pull a panoramic map of a topic with `memex_survey` over HTTP or MCP.
- Anchor relative dates with `--reference-date` and bound results with `after` / `before` from the API.

The next time someone asks you a question about your vault, the choice between these three is the first decision: passage, claim, or map?

## Next steps

- [How-to: Retrieve data](../how-to/retrieve-data.md) — option matrix for the full retrieval surface.
- [Reference: CLI commands](../reference/cli-commands.md) — every flag on `memex note search` and `memex memory search`.
- [Reference: MCP tools](../reference/mcp-tools.md) — exact `memex_memory_search`, `memex_note_search`, and `memex_survey` signatures.
- [Explanation: Retrieval strategies](../explanation/how-memex-works/retrieval.md) — how the five TEMPR strategies and reciprocal rank fusion work together.

## See also

- [Tutorial: Get started with Memex](getting-started.md)
- [How-to: Retrieve data](../how-to/retrieve-data.md)
- [Reference: MCP tools](../reference/mcp-tools.md)
- [Explanation: Retrieval strategies](../explanation/how-memex-works/retrieval.md)
