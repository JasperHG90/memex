# Memex Documentation

Memex is a long-term memory system for LLMs. It stores notes as Markdown files, extracts structured facts and entities using LLM-powered pipelines, and provides multi-strategy retrieval via PostgreSQL with pgvector. A background reflection engine synthesizes observations into high-level mental models over time.

This documentation is organized using the [Diataxis](https://diataxis.fr/) framework.

---

## Tutorials

Learning-oriented guides that walk you through complete workflows from start to finish.

| Guide | Description |
|:------|:------------|
| [Getting Started](tutorials/getting-started.md) | Install Memex, configure PostgreSQL, ingest your first document, and run a search. |
| [Using the Dashboard](tutorials/using-the-dashboard.md) | Explore the web UI: search memories, browse entities, visualize the knowledge graph, and monitor reflection. |
| [AI Agent Memory](tutorials/ai-agent-memory.md) | Give an AI agent persistent long-term memory using Memex via MCP or the OpenClaw plugin. |

---

## How-To Guides

Goal-oriented recipes for specific tasks. Assumes you have a working Memex installation.

| Guide | Description |
|:------|:------------|
| [Set Up Claude Code](how-to/setup-claude-code.md) | Configure Claude Code to use Memex as long-term memory with one command. |
| [Configure Memex](how-to/configure-memex.md) | Set up YAML config files, environment variables, and model providers. |
| [Organize with Vaults](how-to/organize-with-vaults.md) | Create vaults, set the active writer vault, and attach read-only vaults for multi-project workflows. |
| [Using MCP](how-to/using-mcp.md) | Connect Memex to Claude Desktop, Claude Code, and other MCP-compatible AI assistants. |
| [Batch Ingestion](how-to/batch-ingestion.md) | Ingest folders of documents, URLs, and file batches efficiently. |
| [Doc Search vs Memory Search](how-to/doc-search-vs-memory-search.md) | Choose between note search (raw documents) and memory search (extracted facts). |
| [Database Migrations](how-to/database-migrations.md) | Manage PostgreSQL schema migrations with `memex db` (Alembic). |
| [Delete and Archival](how-to/delete-archival.md) | Delete notes, entities, and memory units; manage data lifecycle. |
| [OpenClaw Integration](how-to/openclaw-integration.md) | Install and configure the Memex memory plugin for OpenClaw agents. |

---

## Reference

Technical descriptions of every interface, command, endpoint, and configuration key.

| Reference | Description |
|:----------|:------------|
| [CLI Commands](reference/cli-commands.md) | Complete reference for all `memex` CLI commands, flags, and arguments. |
| [REST API](reference/rest-api.md) | All HTTP endpoints, request/response schemas, status codes, and authentication. |
| [MCP Tools](reference/mcp-tools.md) | All 22 MCP tools with parameter tables and usage workflow. |
| [Configuration](reference/configuration.md) | Every configuration key, type, default, and environment variable mapping. |
| [Evaluation Report](reference/evaluation-report.md) | LoCoMo benchmark results with retrieval efficiency analysis and distribution plots. |

---

## Explanation

Understanding-oriented articles that explain how Memex works and why it is designed the way it is.

| Article | Description |
|:--------|:------------|
| [Hindsight Framework](explanation/hindsight-framework.md) | The three-phase memory architecture: Extraction, Retrieval, and Reflection. |
| [Extraction Pipeline](explanation/extraction-pipeline.md) | How documents are chunked, facts are extracted, entities are resolved, and embeddings are generated. |
| [Retrieval Strategies](explanation/retrieval-strategies.md) | The TEMPR system: five search strategies fused via Reciprocal Rank Fusion. |
| [Reflection and Mental Models](explanation/reflection-and-mental-models.md) | How the background engine synthesizes observations into evolving mental models. |
| [Dashboard Architecture](explanation/dashboard-architecture.md) | React + Vite frontend design, data flow, and component structure. |
| [OpenClaw Plugin](explanation/openclaw-plugin.md) | Plugin architecture, lifecycle hooks, circuit breaker, and prompt injection protection. |

---

## Package READMEs

Each package in the monorepo has its own README with package-specific details.

| Package | Description |
|:--------|:------------|
---

> **Found a bug?** Run `memex report-bug` to open a pre-filled GitHub issue with your system info automatically attached.

---

| [packages/core](../packages/core/README.md) | Storage engines, memory system, services, and FastAPI server. |
| [packages/cli](../packages/cli/README.md) | Typer CLI (`memex` command). |
| [packages/mcp](../packages/mcp/README.md) | FastMCP server for LLM integration. |
| [packages/common](../packages/common/README.md) | Shared Pydantic models, configuration, and exceptions. |
| [packages/dashboard](../packages/dashboard/README.md) | React + Vite web UI. |
| [packages/openclaw](../packages/openclaw/README.md) | Memex memory plugin for OpenClaw agents. |
