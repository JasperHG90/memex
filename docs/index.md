# Memex Documentation

Memex is a long-term memory system for LLMs and humans. It ingests text, extracts structured facts and entities, retrieves them with multi-strategy search, and synthesises higher-order understanding through background reflection.

This documentation follows the [Diátaxis](https://diataxis.fr/) framework — four modes, each serving a different reader need. Pick the mode that matches what you're trying to do.

> **Refactor in progress.** This site is being rewritten per [`.temp/documentation/PLAN.md`](../.temp/documentation/PLAN.md) v3. Many pages below are placeholders; the existing pages under [`how-to/`](how-to/), [`reference/`](reference/), and [`explanation/`](explanation/) carry the current canonical content until each new page is filled in.

---

## Tutorials

Learn by doing. Step-by-step walkthroughs that end in a verifiable result.

| Guide | What you'll learn |
|:------|:------------------|
| [Get started with Memex](tutorial/getting-started.md) | Install Memex, start the server, create a vault, ingest your first note, and run a search. |
| [Build an agent against the REST API](tutorial/build-an-agent.md) | Wire a custom (non-Claude-Code, non-Hermes) agent to Memex via HTTP. |
| [Integrate Memex with Claude Code](tutorial/claude-code-integration.md) | Install the plugin, bind a vault to a project, save and recall memories. |
| [Walk through Memory Worth and deprioritization](tutorial/memory-worth-and-deprioritization.md) | Record outcomes, watch MW shift, see FSFM auto-band a low-MW unit, restore it. |
| [Walk through contradiction detection](tutorial/contradiction-detection.md) | Ingest conflicting notes, observe weakens/contradicts links, apply an LLM-proposed winner. |
| [Pick between note search and memory search](tutorial/note-search-vs-memory-search.md) | Decide when to use `memex_memory_search` versus `memex_note_search` versus `memex_survey`. |
| [Explore the entity graph](tutorial/entity-search.md) | Traverse entities and cooccurrences to answer relationship questions. |
| [Choose models for extraction, reflection, and embeddings](tutorial/which-models-work-well.md) | Swap extraction, reflection, and reranker models; observe the latency / cost / quality trade-offs. |

---

## How-to guides

Recipes for a specific job. Assumes Memex is already installed.

### Integrations

| Guide | Job |
|:------|:----|
| [Configure the Claude Code plugin](how-to/integrations/claude-code.md) | Plugin install + per-project vault binding + env-var toggles. |
| [Configure the Hermes plugin](how-to/integrations/hermes-plugin.md) | Wire Memex as Hermes' memory backend. |

### Observability

| Guide | Job |
|:------|:----|
| [Scrape Memex metrics with Prometheus](how-to/observability/prometheus.md) | Point a Prometheus server at `/api/v1/metrics`. |
| [Export Memex traces to Arize Phoenix](how-to/observability/arize-phoenix.md) | Configure OpenTelemetry export. |

### Server configuration

| Guide | Job |
|:------|:----|
| [Secure Memex with an API key](how-to/configuring-server/api-key.md) | Set up scoped API keys for read/write/admin access. |
| [Use a different reranker or embedding model](how-to/configuring-server/reranker-and-embedding-models.md) | Swap the built-in ONNX models for a LiteLLM-backed provider. |
| [Set a default LLM model](how-to/configuring-server/default-model.md) | Pick a model globally or override per pipeline stage. |
| [Run Memex on a low-resource host](how-to/configuring-server/low-resource.md) | Tame concurrency, batch sizes, and embedding cache. |
| [Deploy Memex behind a connection pooler](how-to/configuring-server/deployment.md) | Multi-worker + PgBouncer session-mode caveats. |

### Working with content

| Guide | Job |
|:------|:----|
| [Ingest data via the CLI and Python API](how-to/ingesting-data.md) | Inline notes, files, URLs, folder sync, Python batch ingestion. |
| [Attach files to a note](how-to/asset-attachments.md) | Images, PDFs, and other binary assets. |
| [Retrieve data from the CLI](how-to/retrieve-data.md) | Memory search, note search, survey, entity traversal — all from the terminal. |
| [Use the key-value store](how-to/key-value-store.md) | Set preferences, project conventions, procedures across sessions. |
| [Organise work with vaults](how-to/vaults.md) | Create vaults, route notes via frontmatter, migrate between vaults. |
| [Use note templates](how-to/note-templates.md) | Pick a template, register a new one. |

### Curation

| Guide | Job |
|:------|:----|
| [Deprioritize and restore memory units](how-to/deprioritize-units.md) | Non-destructive downweight via `memex memory deprioritize` + `restore`. |
| [Re-run reflection for one entity](how-to/reconsolidate.md) | Targeted reconsolidation under advisory lock. |
| [Review and apply lint proposals](how-to/linting.md) | Walk the lint ledger; apply LLM-proposed winners. |
| [Trace lineage and unit history](how-to/trace-lineage.md) | Follow a fact back to its source note and through its versions. |
| [Inspect Memex with the diagnose CLI](how-to/diagnostics.md) | UMAP, retrieval breakdown, vault summaries, lint dashboard. |

### Operations

| Guide | Job |
|:------|:----|
| [Capture pages with the Firefox plugin](how-to/firefox-plugin.md) | Web clipper for articles and PDFs. |
| [Back up and export a vault](how-to/backup-and-export.md) | Postgres + FileStore snapshot; full-vault export. |

---

## Reference

Look up an answer.

| Page | Contents |
|:-----|:---------|
| [API routes](reference/api-routes.md) | Every HTTP endpoint, request/response schema, status codes. |
| [CLI commands](reference/cli-commands.md) | Every `memex` subcommand, flag, argument. |
| [MCP tools](reference/mcp-tools.md) | All ~40 MCP tools, parameters, usage notes. |
| [Configuration options](reference/configuration-options.md) | Every configuration key, type, default, env-var mapping. |
| [Data model](reference/data-model.md) | The 14-table relational schema and its primary relationships. |
| [Observability metrics](reference/observability.md) | Every metric and trace the server emits. |
| [Failure modes](reference/failure-modes.md) | What fails open, what blocks, what to do. |
| [Evaluation results](reference/evaluation-results.md) | Latest internal-suite results and external benchmarks. |

---

## Explanation

Understand the why.

### How Memex works

| Page | Topic |
|:-----|:------|
| [High-level architecture](explanation/how-memex-works/high-level-architecture.md) | Layers, packages, dependency graph. |
| [Memory types](explanation/how-memex-works/memory-types.md) | The cognitive-science taxonomy and the agent-facing split (episodic / semantic / procedural / cross-context). |
| [About extraction](explanation/how-memex-works/extraction.md) | Chunking, entity resolution, dedup, embeddings, write-time classifier. |
| [About retrieval](explanation/how-memex-works/retrieval.md) | TEMPR, RRF fusion, the composition chain, MMR diversity, exploration. |
| [About synthesis and reflection](explanation/how-memex-works/synthesis-and-reflection.md) | The 7-phase reflection loop, mental models, vault summaries. |
| [About feedback and curation](explanation/how-memex-works/feedback.md) | Memory Worth, FSFM, the linter, deprioritization. |
| [About the note lifecycle](explanation/how-memex-works/note-lifecycle.md) | Active → superseded → archived; append vs re-ingest. |

### Cross-cutting concepts

| Page | Topic |
|:-----|:------|
| [About session briefings](explanation/session-briefings.md) | What the briefing carries; how Claude Code and Hermes consume it. |
| [About mental-model observations](explanation/mental-model-observations.md) | Read-only projections; why deprioritize routes to source units. |
| [Design principles (P1–P13)](explanation/design-principles.md) | The thirteen principles that anchor Memex's design. |
| [How Memex is evaluated](explanation/how-memex-is-evaluated.md) | The internal suite framework, LoCoMo external benchmark, empirical caveats. |

---

## Design notes

In-flight design decisions and reviews — not Diátaxis docs, but kept here as the audit trail for ongoing or recently-shipped subsystems.

| Note | Topic |
|:-----|:------|
| [Note lifecycle review (post-FSFM)](design/lifecycle-review.md) | Re-evaluation of the four `Note.status` lifecycle states against the FSFM-driven `is_deprioritized` path. |

---

## Package READMEs

Each package in the monorepo has its own README with package-specific details.

| Package | What |
|:--------|:-----|
| [`packages/core`](../packages/core/README.md) | Storage engines, memory system, services, FastAPI server. |
| [`packages/cli`](../packages/cli/README.md) | Typer CLI (`memex` command). |
| [`packages/mcp`](../packages/mcp/README.md) | FastMCP server for LLM integration. |
| [`packages/common`](../packages/common/README.md) | Shared Pydantic models, configuration, exceptions. |
| [`packages/eval`](../packages/eval/README.md) | Evaluation framework and benchmarks. |
| [`packages/claude-code-plugin`](../packages/claude-code-plugin/README.md) | Claude Code plugin. |
| [`packages/hermes-plugin`](../packages/hermes-plugin/README.md) | Hermes Agent memory backend plugin. |
| [`packages/firefox-extension`](../packages/firefox-extension/README.md) | Firefox extension for web content capture. |

> **Found a bug?** Run `memex report-bug` to open a pre-filled GitHub issue with your system info automatically attached.
