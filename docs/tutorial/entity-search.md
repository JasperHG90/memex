# Explore the entity graph

This tutorial walks you through Memex's entity graph by answering one
relationship question end to end: *who does Alice work with?* You will
ingest four short notes about a small team, list the entities Memex
extracted, look at one entity's mentions, then walk to its co-occurring
neighbours. By the end you will know which surface answers which kind of
question — and where the entity graph stops being the right tool.

Entities are the canonical nouns Memex pulls out of your notes —
people, organizations, concepts, technologies — together with the edges
between them. They are global: Alice in two vaults is one entity. They
carry no per-fact reranking weight; they are the *connection* layer,
not the *retrieval* layer. The distinction matters for Step 6.

## Prerequisites

- A running Memex server. Run `memex server status` to confirm.
- A working `memex` CLI on your `PATH`. Run `memex --help` to confirm.
- An active vault you do not mind writing four small notes into.
  Run `memex vault list` to see your vaults. The active one has the
  marker beside it; pass `--vault <name>` on every command below if you
  want to use a different one.
- Background extraction enabled (the default). Ingest writes the note;
  a background job extracts facts and entities a few seconds later.

If `memex memory reflect` is something you have already triggered or
disabled in this vault, the timings in Step 2 still hold — entity
extraction runs at write time, not in reflection.

## Step 1 — Ingest four notes that share entities

You need a small graph to walk. Paste these four commands one at a time.
Each note names two or three people and one piece of shared work:

```bash
memex note add \
  --title "deploy-pipeline-handoff" \
  --tag tutorial-entity-graph \
  "Alice and Bob own the deploy pipeline this quarter. They are migrating it off Jenkins."

memex note add \
  --title "analytics-shipment-q1" \
  --tag tutorial-entity-graph \
  "Bob and Carol shipped the analytics dashboard last quarter. Carol led the schema review."

memex note add \
  --title "design-review-notes" \
  --tag tutorial-entity-graph \
  "Alice, Carol, and Diana met to review the new ingestion design. Diana raised the backpressure question."

memex note add \
  --title "oncall-rotation-update" \
  --tag tutorial-entity-graph \
  "Bob is moving off oncall next month. Diana picks up the rotation."
```

Each command prints a confirmation line with the note's UUID. The four
notes mention four people — Alice, Bob, Carol, Diana — and three
shared activities — the deploy pipeline, the analytics work, the
ingestion design. That is the graph you will walk.

## Step 2 — Let extraction catch up

Entity extraction runs in the background. On a healthy local server it
finishes in a few seconds per note. Give it a moment, then check that
the four notes are visible:

```bash
memex note recent --limit 4
```

You should see all four titles you just added. If the list is short,
re-run after a few seconds — the note exists immediately, but the
entities are not yet linked.

A useful sanity check before you list entities: pull mentions for one
note title via memory search.

```bash
memex memory search "deploy pipeline handoff"
```

If the search returns at least one fact, extraction has run for that
note. If memory search is empty for a note that note search returns,
extraction has not caught up. Wait, then continue.

## Step 3 — List the entities Memex extracted

Now ask Memex what nouns it pulled out of those four notes:

```bash
memex entity list --slim
```

You should see a table with `Name`, `Type`, `Mentions`, and `ID`. The
four people will be ranked among the top rows, each tagged with type
`Person`. The activities — `deploy pipeline`, `analytics`,
`ingestion` — show up as `Concept` or `Technology` rows depending on
how the extractor classified them.

`--slim` drops the per-entity description column. You want it here
because you only need names and types right now. Without it the table
is heavier and the rows are wider.

Two filter flags are worth knowing for later:

```bash
memex entity list --type Person --limit 10
memex entity list --query Alice
```

The first restricts to one entity type — useful when a vault has
hundreds of concept entities and you want only the people. The second
searches by name; pass the noun you remember rather than the exact
canonical spelling. Memex matches by trigram similarity, so close
matches still hit.

Copy Alice's UUID from the `ID` column. You will paste it into the
next two commands. Whenever this tutorial says `<alice-id>`, substitute
that UUID. The `memex entity mentions` and `memex entity related`
commands also accept the name `Alice` directly — Memex resolves names
to UUIDs for you — but using the UUID removes any ambiguity if your
vault has another `Alice`.

## Step 4 — Show one entity's mentions

Mentions are the memory units that name Alice. Each mention links back
to the note it came from, so you can trace any claim to its source.

```bash
memex entity mentions <alice-id>
```

You should see three rows: one for the deploy-pipeline note, one for
the design-review note, and possibly a third if the extractor split a
sentence into two facts. Each row shows the memory-unit text on the
left, the source note title in the middle, and the date on the right.

By default Memex hides three categories of unit from this list:

- **Stale units** — archived or deleted.
- **Superseded units** — confidence-decayed below the surfacing
  threshold.
- **Deprioritized units** — explicitly suppressed by an outcome.

If you want the audit-shaped view — every unit ever tagged with the
entity — pass all three flags:

```bash
memex entity mentions <alice-id> \
  --include-stale \
  --include-superseded \
  --include-deprioritized
```

For this tutorial the defaults are fine. None of your four notes have
been suppressed.

## Step 5 — Walk to Alice's co-occurring entities

This is the step that answers *who does Alice work with?* Co-occurrence
is the count of how often two entities are mentioned in the same memory
unit. The cached counts mean the lookup is constant-time, no matter how
big your vault gets.

<code-ref path="packages/core/src/memex_core/memory/sql_models.py" lines="1066-1151" />

```bash
memex entity related <alice-id>
```

The table sorts neighbours by `Strength` — the raw co-occurrence
count. Reading from the corpus you ingested, you should see something
close to:

| Related Entity      | Strength | ID                |
|---------------------|----------|-------------------|
| Bob                 | 2        | …                 |
| Carol               | 1        | …                 |
| Diana               | 1        | …                 |
| deploy pipeline     | 1        | …                 |
| ingestion design    | 1        | …                 |

Bob appears twice because Alice shares two notes with him: the deploy
pipeline and (indirectly via the design review) the wider rotation.
Carol and Diana appear once each — they share only the design review.

Read this list as: *Alice has been mentioned alongside these entities
this often, across every memory unit ever extracted in this vault.* It
is a corpus-wide counter. It is **not** "the current best understanding
of who Alice works with". A high count means lots of historical
co-mention; it does not say the relationship is current.

<code-ref path="packages/mcp/src/memex_mcp/server.py" lines="2815-2840" />

For the relationship question you started with, the top row is the
answer: Alice's strongest counterpart in this vault is Bob. The second
tier — Carol and Diana — are real but weaker links. If you want
currency rather than corpus frequency, follow up with `memex entity
mentions` on Bob: that view excludes superseded and deprioritized
units by default, so it reflects the live record.

```bash
memex entity mentions <bob-id>
```

Three mentions, two of them shared with Alice. The relationship is
real and current.

## Step 6 — Know when the entity graph is the wrong tool

You answered a relationship question by walking the graph. That worked
because the question was *structural*: "who appears alongside X". The
entity graph is engineered for exactly this — fast, exact, traversal-
shaped.

Use a different surface when the question is *content-shaped*:

- *"What did Alice and Bob decide about the deploy pipeline?"* — That
  is a fact-retrieval question. Use `memex memory search`. The entity
  graph tells you they are connected; memory search tells you the
  decision they reached.
- *"Show me notes mentioning the deploy pipeline."* — That is a
  document-retrieval question. Use `memex note search`. Note search
  ranks source documents; the entity graph ranks the nouns inside them.
- *"Give me a panoramic view of everyone on this team."* — That is a
  synthesis question. Use the MCP-only `memex_survey` from an agent
  client. Survey decomposes the question, runs many sub-queries, and
  merges the answers.

A rough rule: if you need a single fact, use memory search. If you need
a single document, use note search. If you need to see *who connects to
whom*, use the entity graph. If you need a panoramic narrative, use
survey.

## What you built

You ingested four notes about a small team, watched Memex extract the
people and shared work as entities, listed those entities, looked at
the memory units that mention one of them, and walked the co-occurrence
graph to identify Alice's strongest counterpart. You also know when
the graph stops being the right answer.

The walk you just made — `entity list` → `entity mentions` →
`entity related` — is the same one an MCP-driven agent makes via
`memex_list_entities` → `memex_get_entity_mentions` →
`memex_get_entity_cooccurrences`. The CLI and MCP surfaces are
parallel views of the same graph.

## Clean up

The four tutorial notes are still in your vault. Find them by title and
remove them if you like:

```bash
memex note find deploy-pipeline-handoff
memex note find analytics-shipment-q1
memex note find design-review-notes
memex note find oncall-rotation-update
memex note delete <note-id>
```

`memex note find` returns the note's UUID; pass that UUID to
`memex note delete`. The entities themselves stay — they may be linked
to notes you have not deleted. `memex entity delete Alice` removes the
entity and all its joins, but you usually do not need to: entities with
no surviving mentions simply stop showing up at the top of
`entity list`.

## See also

- [Tutorial: Get started with Memex](getting-started.md)
- [How-to: Retrieve data from the CLI](../how-to/retrieve-data.md)
- [Reference: CLI commands](../reference/cli-commands.md)
- [Explanation: retrieval strategies](../explanation/how-memex-works/retrieval.md)
