<p align="center">
  <img src="assets/memex-logo-spacy.png" width="160" alt="Memex Logo" />
</p>

<h1 align="center">Memex</h1>
<h3 align="center">A Knowledge Base for LLMs</h3>

<p align="center">
  The knowledge layer your AI agents are missing.<br/>
  <strong>Ingest anything. Remember everything. Retrieve what matters.</strong>
</p>

<p align="center">
  <a href="./docs/index.md">Documentation</a> &bull;
  <a href="./docs/tutorial/getting-started.md">Quick Start</a> &bull;
  <a href="#claude-code-plugin">Claude Code Plugin</a> &bull;
  <a href="./FAQ.md">FAQ</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/language-Python-blue?style=flat-square" alt="Python" />
  <img src="https://img.shields.io/badge/python-3.12+-blue?style=flat-square&logo=python&logoColor=white" alt="Python 3.12+" />
  <img src="https://img.shields.io/badge/license-Apache%202.0-blue?style=flat-square" alt="Apache 2.0" />
  <img src="https://img.shields.io/badge/version-v1.0.0--rc-green?style=flat-square" alt="v1.0.0-rc" />
  <img src="https://img.shields.io/badge/tests-7,981%20passing-brightgreen?style=flat-square" alt="Tests" />
</p>

> [!IMPORTANT]
> **Memex is in beta.** It is functional and actively used, but expect rough edges, breaking changes between versions, and incomplete documentation. Feedback and bug reports are welcome — run `memex report-bug` or open an issue.

## Vision

Memex exists because organizing knowledge shouldn't be your job. It is a self-organizing, self-reflecting knowledge system: you feed it raw material — articles, meeting notes, documents, web pages — and it extracts the facts, builds the connections, flags the contradictions, and synthesizes what it all means. When you need something, you ask, and the knowledge is already structured, cross-referenced, and ready. No filing. No tagging. No maintenance. Your knowledge compounds on its own so you can focus on the work that actually matters.

Memex is deliberately not an agent. It provides the storage, extraction, and retrieval — your agent of choice provides the synthesis and the judgment to call the right tool at the right moment. This separation of concerns means Memex works with any LLM agent that speaks MCP or REST, rather than locking you into a single interface. Conceptually, Memex overlaps with Andrej Karpathy's [LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f).

---

**[Requirements](#requirements)** · **[Features](#features)** · **[Quick Start](#-quick-start)** · **[Claude Code Plugin](#claude-code-plugin)** · **[See it in action](#see-it-in-action)** · **[Documentation](#-documentation)** · **[Releasing](#releasing)** · **[FAQ](./FAQ.md)**

---

## Requirements

1. Python 3.12+ (3.13 tested in CI)
2. [uv](https://docs.astral.sh/uv/) >= 0.10.0
3. PostgreSQL with pgvector

## Features

<table>
<tr>
<td width="33%" valign="top">
<p>📥 <strong>Ingest Anything</strong><br>
<sub>Markdown, PDF, Word, PowerPoint, Excel, Outlook emails, web pages, or entire directories. Conversion via MarkItDown &amp; PyMuPDF. Pluggable note templates, asset management, and batch CLI operations.</sub></p>
</td>
<td width="33%" valign="top">
<p>🧠 <strong>Five-Strategy Retrieval (TEMPR)</strong><br>
<sub>Semantic, Keyword, Graph, Temporal, and Mental Model — five strategies run in parallel, fused via Reciprocal Rank Fusion. MMR diversity filtering prunes near-duplicates.</sub></p>
</td>
<td width="33%" valign="top">
<p>🌳 <strong>Hierarchical Page Index</strong><br>
<sub>Documents are split into a structured TOC with section summaries, token estimates, and node IDs. Read a 50-page PDF section by section instead of dumping everything into context.</sub></p>
</td>
</tr>
<tr>
<td valign="top">
<p>🔄 <strong>Incremental Extraction</strong><br>
<sub>Update a note and Memex diffs the content, only re-extracting changed blocks. Unchanged facts, entities, and embeddings are preserved — fast for living documents.</sub></p>
</td>
<td valign="top">
<p>⚔️ <strong>Contradiction Detection</strong><br>
<sub>New facts get scored against the existing graph. The FSFM linter watches for graph pressure between contradicting units; when the signal escalates, an LLM lint pass proposes a winner you can apply or reverse with one command. Retrieval favors current information.</sub></p>
</td>
<td valign="top">
<p>🪞 <strong>Reflection &amp; Mental Models</strong><br>
<sub>A 7-phase background loop (P0–P6) synthesizes observations into versioned mental models per entity, tracks trends across versions, and surfaces stable patterns. Memex evolves from raw facts into structured understanding over time.</sub></p>
</td>
</tr>
<tr>
<td valign="top">
<p>🏦 <strong>Vaults</strong><br>
<sub>Isolate knowledge by project, team, or topic. Policy-based ACL (reader/writer/admin) with vault-scoped API keys. Cross-vault read access for shared knowledge. Auto-generated vault summaries with 3-tier regeneration.</sub></p>
</td>
<td valign="top">
<p>☁️ <strong>Cloud-Native Storage &amp; Assets</strong><br>
<sub>Notes and file assets (images, PDFs, audio) stored via fsspec — swap between local disk, S3, and GCS with a config change. PostgreSQL + pgvector for metadata and vector search.</sub></p>
</td>
<td valign="top">
<p>🤖 <strong>AI Agent Integration</strong><br>
<sub>First-class MCP support for Claude Code, Claude Desktop, and any MCP-compatible client. ~55 MCP tools* with progressive disclosure, staleness flags on search results, note relation links, survey-based query decomposition, stdio/HTTP/SSE transports, slim Docker image decoupled from core. <em>*surface is mid-refactor; tool count moves a few up or down release-to-release.</em></sub></p>
</td>
</tr>
<tr>
<td valign="top">
<p>🌐 <strong>REST API &amp; Webhooks</strong><br>
<sub>FastAPI server with NDJSON streaming, OpenAPI docs, policy-based auth, vault-scoped API keys, rate limiting, and CORS extension support.</sub></p>
</td>
<td valign="top">
<p>🧬 <strong>Lineage &amp; Provenance</strong><br>
<sub>Trace any mental model back through observations to the original source document. Full bidirectional provenance traversal with depth control.</sub></p>
</td>
<td valign="top">
<p>🦊 <strong>Firefox Extension</strong><br>
<sub>One-click save of articles, PDFs &amp; web pages. Readability extraction, Markdown conversion, inline image capture. AES-GCM encrypted API key storage.</sub></p>
</td>
</tr>
<tr>
<td valign="top">
<p>📂 <strong>Folder Sync</strong><br>
<sub>Sync Obsidian vaults or any local folder to Memex. Multi-format support, asset upload, frontmatter skip markers, SQLite state tracking, watchdog/polling watch modes, archive-on-delete.</sub></p>
</td>
<td valign="top">
<p>🐦‍🔥 <strong>Pluggable Inference Backends</strong><br>
<sub>Swap built-in ONNX embedding &amp; reranking models for any LiteLLM provider — OpenAI, Gemini, Cohere, Ollama. Inverse-sigmoid logit transform preserves retrieval scoring.</sub></p>
</td>
<td valign="top">
<p>🔭 <strong>OpenTelemetry Observability</strong><br>
<sub>Distributed tracing with Arize Phoenix — session IDs on spans, operation names on DSPy LLM calls, background reflection jobs tracked across tracing sessions.</sub></p>
</td>
</tr>
<tr>
<td valign="top">
<p>🔑 <strong>KV Store</strong><br>
<sub>Namespaced key-value store for structured facts, preferences, and conventions. Semantic search via embeddings, exact key lookup, namespace filtering (global, user, project, app).</sub></p>
</td>
<td valign="top">
<p>🧩 <strong>Claude Code Plugin</strong><br>
<sub>One-step persistent memory across all projects. Token-budgeted session briefing, /remember, /recall, and /learnings skills, data-driven session hooks, progressive session notes, and Memex MCP server — bundled as a Claude Code plugin.</sub></p>
</td>
<td valign="top">
<p>📋 <strong>Audit Logging</strong><br>
<sub>Append-only audit trail tracking actions, actors, resource IDs, and session IDs. Non-blocking background dispatch backed by the metastore.</sub></p>
</td>
</tr>
<tr>
<td valign="top">
<p>⚖️ <strong>Memory Worth &amp; Curation</strong><br>
<sub>Every memory unit carries a Memory Worth score that rises with useful outcomes and falls with unhelpful ones. An FSFM-inspired composite blends graph pressure, low MW, staleness, and entity dormancy to flag candidates for deprioritization — never deletion. Restore at any time.</sub></p>
</td>
<td valign="top">
<p>🧹 <strong>Maintenance Linter</strong><br>
<sub>A periodic linter scans the vault for fact-state issues — composite candidates, high-MW units under graph pressure, low-credibility contradictions. Escalations get a pre-attached LLM-proposed winner you can apply or reverse with one command.</sub></p>
</td>
<td valign="top">
<p>🔬 <strong>Operator Diagnostics</strong><br>
<sub>The `memex diagnostics` CLI inspects the embedding manifold, retrieval ranking signals, vault summary stats, and pending lint backlog. Pair with the OpenTelemetry traces and Prometheus metrics for a full operator view.</sub></p>
</td>
</tr>
<tr>
<td valign="top">
<p>🧭 <strong>Procedural Memory (Cases)</strong><br>
<sub>Beyond facts — capture <em>how your team does things</em>. Submit a worked episode as a <strong>case</strong> (a note with a job) and Memex links it to a reusable <strong>procedure</strong> or drafts a new one; <strong>strategies</strong> pick a procedure for a context. Assignment-judged, draft-then-activate via the lint queue, with success/failure counters per procedure.</sub></p>
</td>
</tr>
</table>

<details>
<summary><strong>Feature details</strong></summary>

### Ingest anything

Feed Memex from any source — plain text, Markdown, PDFs, Word docs, PowerPoint, Excel, Outlook emails, web pages, or entire directories. File conversion is handled automatically via [MarkItDown](https://github.com/microsoft/markitdown) and [PyMuPDF](https://pymupdf.readthedocs.io/). Background and batch ingestion modes let you import large document collections without blocking. Pluggable note templates (built-in, global, and project-local `.toml` files) provide consistent structure for different note types.

```bash
memex note add "Quick inline note"
memex note add --file ./research-papers/        # directory of PDFs
memex note add --url https://example.com/article
memex note add --file report.md --asset diagram.png --background
```

#### Firefox extension

A [Firefox extension](./packages/firefox-extension/) for one-click capture of articles, PDFs, and web pages directly into your Memex vaults. Content is extracted client-side via Mozilla Readability and converted to Markdown — bypassing bot detection and paywalled content that server-side scraping can't reach. API keys are encrypted at rest with AES-GCM.

![firefox](./assets/firefox-extension.png)

### Five-strategy retrieval (TEMPR)

Every search runs five independent retrieval strategies in parallel and fuses them with Reciprocal Rank Fusion — no single strategy has to be "right":

| Strategy | What it finds |
|:---------|:--------------|
| **Semantic** | Conceptually similar facts via pgvector cosine distance |
| **Keyword** | Exact term matches via PostgreSQL full-text search |
| **Graph** | Entity-linked facts via NER, phonetic matching, and co-occurrence traversal |
| **Temporal** | Recent facts via exponential time-decay scoring |
| **Mental Model** | High-level synthesized insights from the reflection engine |

Post-fusion, MMR diversity filtering prunes near-duplicates using a hybrid cosine + entity Jaccard kernel. Optional `after`/`before` date bounds and `tags` filters let you scope any search.

### Hierarchical page index

Long documents are split into a structured table of contents with section-level summaries, token estimates, and unique node IDs. Read a 50-page PDF section by section instead of dumping the entire document into context. The page index powers skeleton-tree reasoning (`--reason`) and targeted answer synthesis (`--summarize`).

### Incremental extraction

When you update a note (via `note_key`), Memex diffs the content against the previous version and only re-extracts changed blocks. Unchanged facts, entities, and embeddings are preserved — saving LLM calls and keeping ingestion fast for living documents.

### Contradiction detection and note relations

New facts get scored against the existing graph as they ingest. Typed `MemoryLink` rows — `contradicts`, `weakens`, `reinforces`, and the causal types — feed into the FSFM composite that the maintenance linter runs over each vault. When graph pressure escalates a finding, a second LLM lint pass reads both contradicting units (along with their source dates, credibility, and authority) and proposes a winner with a confidence score. Approve via `memex lint apply`; reverse with `memex lint reverse` if the verdict turns out wrong. Search results include inline `related_notes` (notes sharing entities) and typed `links` (contradicts, reinforces, temporal, causes) for relationship discovery without additional queries.

### Reflection and mental models

A background 7-phase reflection loop (P0–P6) periodically reviews entities with new evidence, synthesizes observations, and builds versioned mental models. Trends between versions surface as stable patterns. Over time, Memex evolves from a collection of raw facts into structured understanding — "The team consistently prioritizes performance over feature velocity" emerges from dozens of individual meeting notes.

### Vaults

Isolate knowledge by project, team, or topic. Each vault is a self-contained scope for notes, memories, entities, and mental models. Policy-based access control (reader/writer/admin) with vault-scoped API keys lets you grant fine-grained permissions. Use `read_vault_ids` for cross-vault read access without write permissions.

Each vault includes an auto-generated natural language summary describing topics, themes, and statistics. Summaries regenerate automatically via a 3-tier strategy (on ingestion after cooldown, periodic background refresh, and on-demand via CLI or API). Use `memex vault summary` to view or regenerate a vault's summary.

### Cloud-native storage and assets

The file store uses [fsspec](https://filesystem-spec.readthedocs.io/) for backend-agnostic storage. Swap between local disk, Amazon S3, and Google Cloud Storage with a config change. File assets (images, PDFs, audio) are stored alongside notes and served through MCP as native content types (Image, Audio, File). The CLI and MCP tools support listing, retrieving, adding, and deleting assets per note.

```yaml
server:
  file_store:
    type: s3            # or 'gcs', 'local'
    root: my-bucket/memex
```

### AI agent integration

First-class support for Claude Code, Claude Desktop, and any MCP-compatible client. Install the [Claude Code plugin](#claude-code-plugin) for one-step setup across all projects. ~55 MCP tools* with progressive disclosure (3-stage tool discovery by default) cover the full API surface. Search results include staleness flags (fresh/aging/stale/contested) and inline note relation links for relationship discovery. A slim Docker image (`docker/mcp/Dockerfile`) enables containerized MCP deployment with HTTP transport.

<sub>*The MCP surface is mid-refactor; the tool count moves a few up or down release-to-release. See [MCP Tools reference](./docs/reference/mcp-tools.md) for the current inventory.</sub>

### REST API and webhooks

A full FastAPI server with NDJSON streaming, OpenAPI docs, policy-based auth (reader/writer/admin) with vault-scoped API keys, rate limiting, and outgoing webhook subscriptions for event-driven integrations (`ingestion.completed`, `reflection.completed`).

### Lineage and provenance

Trace any mental model back through observations to the original source document. Full bidirectional provenance traversal (`upstream`, `downstream`, `both`) with configurable depth and child limits.

### Folder sync

Sync a folder of Markdown notes (and PDFs, Word docs, Excel, PowerPoint, Outlook emails, and more) to Memex with `memex note sync`. Incremental sync tracks state locally — only changed files are re-processed. Deleted files are archived by default (preserving data, excluding from retrieval). Background batch mode, continuous watch mode (event-driven or polling), and a layered TOML config (`note-sync.toml`) make it easy to keep an Obsidian vault or any notes folder in sync.

```bash
memex note sync init ~/notes          # create default config
memex note sync run ~/notes           # sync changed files
memex note sync watch ~/notes         # continuous sync
```

### Pluggable inference backends

Swap the built-in ONNX embedding and reranking models for any LiteLLM-supported provider (OpenAI, Gemini, Cohere, Ollama, etc.) via config. An inverse-sigmoid logit transform on LiteLLM reranker scores preserves the retrieval engine's scoring semantics.

### OpenTelemetry observability

Distributed tracing via Arize Phoenix. Session IDs propagate across spans, DSPy LLM calls get operation names, and background reflection jobs are tracked across tracing sessions.

### KV store

A lightweight namespaced key-value store for structured facts, preferences, and conventions. Keys use namespace prefixes (`global:`, `user:`, `project:<id>:`, `app:<id>:`) for scoping. Each entry gets an embedding for semantic search, enabling fuzzy lookup alongside exact key access. Ideal for storing agent preferences, project conventions, and user facts that persist across sessions.

### Claude Code plugin

Give Claude Code persistent memory across all projects with a single plugin install. The plugin bundles the Memex MCP server, `/remember`, `/recall`, and `/learnings` slash commands, and session lifecycle hooks with intelligent context injection. A token-budgeted session briefing (`memex briefing`) replaces raw data dumps with a curated knowledge index — KV facts, vault summary, top entities with trend indicators, and available vaults — all within a configurable 1000 or 2000 token budget. Data-driven pre-compact nudges reference actual session stats (write counts, edit spirals, commits), and a progressive session note persists context across compaction boundaries via `note_key`. No per-project configuration needed.

### Audit logging

An append-only audit trail backed by the metastore. Every significant action (ingestion, deletion, status change, reflection) is logged with the actor, resource ID, action type, and session ID. Dispatch is non-blocking — audit writes happen in the background without impacting request latency.

### Procedural memory (cases)

Most memory in Memex is *declarative* — facts, events, and observations about what is true. Procedural memory is the other half: *how your team does things*. The `deploy` verb that means "staging" here, the way this repo wants its PRs, the fix that worked last time.

It rides on the same substrate as everything else. A **case** is a note — the exact same Markdown-note row, stored and extracted the same way — but with `role='case'` and a job: it records a *worked episode* (Trigger / Situation / Actions / Outcome + Lesson) instead of describing the world. Submitting one (`memex case submit`) files the note, then runs an assignment step that links it to an existing **procedure** or drafts a new one, and the case's outcome bumps that procedure's success/failure counters. Procedures — and the **strategies** that pick between them for a context — live on a dedicated plane, surface as compact index cards in the session briefing, and graduate from `draft` to `published` through the maintenance lint queue.

The one-line distinction: **a note says what is true; a case says what you did and how it turned out.** Same storage, different job — and the case is what teaches Memex the procedure. (This is distinct from the KV store, which holds a user's *stated* one-line preferences and conventions; the procedural plane holds recipes Memex *distils* from real worked episodes.)

</details>

## 🚀 Quick Start

> [!NOTE]
> Features like AI-generated answers, fact extraction, and reflection require an LLM API key. By default, Memex uses Gemini and needs `GEMINI_API_KEY` set in your environment. See [Set the default model](./docs/how-to/configuring-server/default-model.md) for other model providers.

### 1. Set up postgres

Download e.g. the [Postgres app](https://postgresapp.com/), or use docker for just the database: `docker compose up -d postgres` (see `docker-compose.yaml` in this repository).

### 2. Install
Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/) (>= 0.10.0).

```bash
uv tool install --refresh "memex-cli[server] @ git+https://github.com/JasperHG90/memex.git@latest#subdirectory=packages/cli"
```

It's easiest to just alias the `uv tool` command: `alias memex="uv tool run --from memex-cli memex"`

### 3. Initialize
Sets up your local storage and configuration.

```bash
memex config init
```

### 4. Start the Server
Memex requires a running API server for all operations.

```bash
# In a separate terminal
memex server start -d
```

### 5. Ingest
Feed it knowledge.

```bash
# Isolate notes with vaults
memex vault create notes --description "Notes about things"

# Inline note
memex note add -v notes "Memex provides long-term memory that evolves."

# Capture a webpage
# Goes to the 'global' vault
memex note add --url "https://docs.python.org/3/tutorial/"

# Point it to local files
# Supports: MD, PDF, docx, xlsx, outlook, pptx
memex note add --file /path/to/file.md --vault notes
```

### 6. Search
Ask questions.

```bash
memex memory search "How does Python handle memory management?"
```

## See it in action

### Claude Code Plugin

Give Claude Code persistent memory across all projects — no per-project setup needed.

```bash
# Add the Memex marketplace
claude plugin marketplace add JasperHG90/memex

# Install the plugin
claude plugin install memex@memex
```

Or from inside Claude Code: `/plugin marketplace add JasperHG90/memex` then `/plugin install memex@memex`.

The plugin provides slash commands — `/remember`, `/recall`, `/learnings`, `/ingest`, `/lint`, `/handoff`, `/continue`, and more — token-budgeted session briefing, data-driven lifecycle hooks, and the Memex MCP server. See [packages/claude-code-plugin](./packages/claude-code-plugin/) for details.

#### Updating the claude code plugin

To update the claude code plugin, first execute `claude plugin marketplace update`, then `claude plugin update memex@memex` to update the claude code plugin.

#### Overriding defaults

- By default, the claude code plugin uses the MCP server from tag `latest`. To override this, you can specify a project-level memex MCP server in your project's `.mcp.json`.
- To override individual memex settings (e.g. MEMEX_BASE_URL), add these to './claude/settings.json', e.g.

```json
{
  "env": {
    "MEMEX_SERVER_URL": "http://host.docker.internal:8000"
  }
}
```

![Claude Code using Memex for long-term memory](assets/memex_claude_code.gif)

### Memory Search
Search across your knowledge base with TEMPR multi-strategy retrieval.

![Memory search showing results for Python memory management](assets/memex_cli_memory.gif)

### Memory Search with AI Answer
Get synthesized answers from your memories using `--answer`.

![Memory search with AI-generated answer](assets/memex_cli_memory_answer.gif)

### Note Search with Reasoning
Find relevant documents with LLM-powered relevance reasoning using `--reason`.

![Note search with reasoning annotations](assets/memex_cli_docs.gif)

### Entity Explorer
Browse and explore entities extracted from your knowledge base.

![Entity list and related entity exploration](assets/memex_cli_entities.gif)

### System Stats
Monitor your Memex instance at a glance.

![System statistics overview](assets/memex_cli_stats.gif)

### URL Ingestion
Capture web content directly into your knowledge base.

![Ingesting a URL into Memex](assets/memex_cli_ingest.gif)

## 📚 Documentation

Comprehensive guides and references live in [`docs/`](./docs/index.md). The tree follows [Diátaxis](https://diataxis.fr/) — tutorial, how-to, reference, explanation.

### Tutorials — learn by doing
- [Getting started](./docs/tutorial/getting-started.md): Install, configure, ingest, and search from scratch.
- [Build an agent](./docs/tutorial/build-an-agent.md): Wire a Python agent to Memex over REST or MCP.
- [Claude Code integration](./docs/tutorial/claude-code-integration.md): Walk through the plugin end-to-end.
- [Which models work well](./docs/tutorial/which-models-work-well.md): Pick embedding, reranking, and LLM providers.
- [Memory Worth and deprioritization](./docs/tutorial/memory-worth-and-deprioritization.md): See curation in action.
- [Contradiction detection](./docs/tutorial/contradiction-detection.md): Land a conflicting fact and watch the linter resolve it.
- [Note search vs memory search](./docs/tutorial/note-search-vs-memory-search.md): Pick the right retrieval mode.
- [Entity search](./docs/tutorial/entity-search.md): Explore the entity graph by hand.

### How-to guides — get a job done
- [Ingest data](./docs/how-to/ingesting-data.md): CLI, API, folder sync, and batch modes.
- [Retrieve data](./docs/how-to/retrieve-data.md): Search, browse, and filter.
- [Use the KV store](./docs/how-to/key-value-store.md): Namespaced preferences, project bindings, and conventions.
- [Attach assets to notes](./docs/how-to/asset-attachments.md): Images, PDFs, audio.
- [Deprioritize units](./docs/how-to/deprioritize-units.md): Demote without deleting.
- [Reconsolidate an entity](./docs/how-to/reconsolidate.md): Re-run the 7-phase loop for a single entity.
- [Submit cases and derive procedures](./docs/how-to/submit-cases.md): Turn a worked episode into a reusable procedure.
- [Trace lineage](./docs/how-to/trace-lineage.md): Walk from mental model back to source.
- [Run diagnostics](./docs/how-to/diagnostics.md): Manifold, retrieval, lint backlog.
- [Backup and export](./docs/how-to/backup-and-export.md): Get your data out at any time.
- [Configure note templates](./docs/how-to/note-templates.md): Pluggable `.toml` templates.
- [Organize with vaults](./docs/how-to/vaults.md): Isolate by project, team, or topic.
- [Apply lint findings](./docs/how-to/linting.md): Approve or reverse LLM-proposed winners.
- [Configure the server](./docs/how-to/configuring-server/default-model.md): Default model, embedding and reranking models, API keys, low-resource setup, deployment.
- [Integrate with Claude Code](./docs/how-to/integrations/claude-code.md): Plugin install and overrides.
- [Integrate with the Hermes plugin](./docs/how-to/integrations/hermes-plugin.md): Hermes Agent memory provider.
- [Wire Prometheus](./docs/how-to/observability/prometheus.md) and [Arize Phoenix](./docs/how-to/observability/arize-phoenix.md): Metrics and traces.
- [Install the Firefox plugin](./docs/how-to/firefox-plugin.md): One-click web capture.

### Reference — look it up
- [CLI commands](./docs/reference/cli-commands.md)
- [API routes](./docs/reference/api-routes.md): REST surface.
- [MCP tools](./docs/reference/mcp-tools.md): ~55 tools across asset, note, search, entity, KV, vault, outcome, curation, lint, diagnostics, procedural.
- [Configuration options](./docs/reference/configuration-options.md)
- [Data model](./docs/reference/data-model.md): The full database schema.
- [Failure modes](./docs/reference/failure-modes.md): What blocks, what degrades gracefully.
- [Observability](./docs/reference/observability.md): Metrics catalog and span attributes.
- [Evaluation results](./docs/reference/evaluation-results.md): Internal suites and agent-integration scenarios (external benchmarks run occasionally).

### Explanation — understand the why
- [Design principles](./docs/explanation/design-principles.md): P1–P13.
- [Session briefings](./docs/explanation/session-briefings.md): How briefings get composed across harnesses.
- [Mental-model observations](./docs/explanation/mental-model-observations.md): The read-only projection contract.
- [How Memex is evaluated](./docs/explanation/how-memex-is-evaluated.md): The internal eval framework.
- [High-level architecture](./docs/explanation/how-memex-works/high-level-architecture.md): Layers and package map.
- [Memory types](./docs/explanation/how-memex-works/memory-types.md): Notes, memory units, KV entries, observations.
- [Extraction](./docs/explanation/how-memex-works/extraction.md): How raw text becomes structured memory.
- [Retrieval](./docs/explanation/how-memex-works/retrieval.md): TEMPR, RRF, composition, MMR.
- [Synthesis and reflection](./docs/explanation/how-memex-works/synthesis-and-reflection.md): The 7-phase loop.
- [Feedback](./docs/explanation/how-memex-works/feedback.md): Memory Worth, outcome counters, FSFM scoring.
- [Note lifecycle](./docs/explanation/how-memex-works/note-lifecycle.md): Active, superseded, archived, appended.
- [Procedural memory plane](./docs/explanation/how-memex-works/procedural-memory.md): Cases, procedures, strategies — recipes distilled from worked episodes.

> **Found a bug?** Run `memex report-bug` to open a pre-filled GitHub issue.

## Releasing

Memex uses [semver](https://semver.org/) with unified versions across all Python packages. TypeScript packages are bumped alongside.

### How to determine the version bump

Look at the conventional commits since the last tag:

| Commit type | Bump | Example |
|---|---|---|
| `fix:` | **patch** (0.0.x) | `fix(core): handle null embeddings` |
| `feat:` | **minor** (0.x.0) | `feat(core): add entity graph` |
| `feat!:` or `BREAKING CHANGE:` | **major** (x.0.0) | `feat!: change API response format` |

### Release workflow

```bash
# 1. Check what changed since last tag
git log --oneline $(git describe --tags --abbrev=0 2>/dev/null || echo HEAD~10)..HEAD

# 2. Bump all versions, commit, and tag
just release 0.1.0

# 3. Push (triggers the release workflow)
git push && git push --tags
```

The `release.yaml` GitHub Action automatically builds all artifacts and creates a GitHub Release with auto-generated release notes.

## Evaluation

Memex's evaluation centers on an **internal suite** that grows with the system, not on a published leaderboard. It runs two layers:

- **Retrieval and extraction regression** — hand-verified scenarios assert that a specific query returns the right facts and ranks the right units. Seven suites run in CI in seconds against a snapshot-cached vault.
- **Agent integration** — the same scenarios, answered by a *real agent* driving Memex's tool surface (Claude Code over MCP, Hermes over the plugin). This measures whether the agent picks the right tool, cites honestly, and routes each write to the right place. It is the layer under active expansion.

This is where the work goes. See [How Memex is evaluated](./docs/explanation/how-memex-is-evaluated.md) for the framework and [`packages/eval`](./packages/eval/README.md) to run it.

### External benchmarks (occasional)

Memex also carries tooling to score against published long-memory datasets — [LoCoMo](https://arxiv.org/abs/2402.17753) and LongMemEval. These run on demand, not on a cadence, so read any numbers as point-in-time snapshots rather than a tracked result. An early LoCoMo run (first conversation, 47 of 50 QA pairs after excluding 3 image-only questions; answering model Claude Opus 4 via Claude Code, judge Gemini 3 Flash, 0–1 graded scale):

| Category | Count | Mean Score |
|---|---|---|
| Single-Hop | 9 | 0.944 |
| Multi-Hop | 9 | 1.000 |
| Open Domain | 3 | 1.000 |
| Temporal | 15 | 1.000 |
| **Non-adversarial** | **36** | **0.986** |
| Adversarial (unweighted) | 11 | 0.773 |

Retrieval stayed cheap on that run — a median 4,609 tokens per question, about 4.5% of total token usage, with the rest being agent overhead. Full methodology, retrieval-efficiency analysis, and per-question detail live in the [evaluation results reference](./docs/reference/evaluation-results.md).

## 🏗️ Architecture

Memex is built as a monorepo:
- **`packages/core`**: The brain. Extraction, Retrieval (TEMPR), Reflection, services, FastAPI server.
- **`packages/cli`**: The interface. Typer CLI commands, including `memex note sync` for folder-based note synchronization.
- **`packages/mcp`**: The bridge. FastMCP server for AI agent integration.
- **`packages/common`**: The foundation. Shared models, config, and exceptions.
- **`packages/eval`**: The eval harness. Internal regression and agent-integration suites, plus external-benchmark tooling (LoCoMo, LongMemEval).
- **`packages/claude-code-plugin`**: The plugin. Claude Code plugin for cross-project memory integration.
- **`packages/firefox-extension`**: The capture. Firefox extension for web content ingestion.

## Acknowledgements

Memex builds on ideas and code from these projects:

- **[Hindsight](https://github.com/vectorize-io/hindsight)** — the Hindsight retention engine formed the basis for Memex's memory system (extraction, retrieval, and reflection).
- **[PageIndex](https://github.com/VectifyAI/PageIndex)** — inspired the hierarchical page index used for structured note retrieval.

## License

[Apache 2.0](LICENSE.txt). See [NOTICES](NOTICES) for third-party attributions.
