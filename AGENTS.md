# AGENTS.md

This file provides guidance to LLM models when working with code in this repository.

## **CRITICAL**: Code Search Protocol (Non-negotiable)

All code is indexed using `qmd`.

**Collections**
- Source code indexed in **memex_src**
- Test code indexed in **memex_test**

1. **Step 1: Keyword Search.** You **MUST** first run `qmd search --line-numbers -n 10 "<exact_term>" --collection <COLLECTION>`.
2. **Step 2: Vector Search.** If Step 1 fails or yields too many results, you **MUST** run `qmd vsearch --line-numbers -n 10 "<concept_description>" --collection <COLLECTION>`.
3. **Step 3: Deep Query.** ONLY if Steps 1 & 2 fail, you may run `qmd query --line-numbers -n 10 "<complex_question>" --collection <COLLECTION>`.

**PROHIBITED:**
- Do NOT skip to `query`.
- Do NOT skip step 2. Ever.
- Do NOT use `read_file`, `glob`, `grep`, `ripgrep`, `find`, `search_file_content` or any other shell-based search command for discovery until Step 3 is complete.

**TOOL OVERRIDES**:
- **REPLACEMENT**: You **MUST** use `run_shell_command` with `qmd search` for all code discovery.

## **CRITICAL**: Code Retrieval Protocol (Non-negotiable)

1. **Step 1 (Mandatory):** To retrieve code, you **MUST** use `qmd get <file>:<line> -l <lines>` to read the specific segment identified by your search.
    - **Guideline:** Start with `-l 50` lines to get the immediate context.
2. **Step 2 (Fallback):** `qmd get <file>` (Full file retrieval).
    - **Constraint:** This is **FORBIDDEN** unless Step 1 has been executed and the output was insufficient.
    - **Exception:** Small config files (toml/yaml/json) under 100 lines may be read fully.

**Critical constraint**:
- You are **PROHIBITED** from using Step 2 (full file retrieval) as your first step for source code.

## Project Overview

Memex is a long-term memory system for LLMs - "Obsidian for LLMs". It stores notes as Markdown files (FileStore) with PostgreSQL+pgvector for metadata and vector search (MetaStore).

## Commands

```bash
# Setup
just setup              # Install deps + pre-commit hooks

# Development
just test               # Run pytest on /tests
just prek               # Run linting/formatting (ruff + mypy)

# Run a single test
uv run pytest tests/test_file.py::test_name -v

# Run tests by marker
uv run pytest -m integration    # Integration tests (require Docker)
uv run pytest -m llm            # Tests requiring LLM API calls

# Dependency management (always use uv, not pip)
uv add --dev <package>                    # Add dev dependency to root
uv add <package> --package memex_core     # Add to specific package
```

## Architecture

### Package Structure (Python monorepo with uv)

- **`packages/core`** - Core library: storage engines, memory system (extraction/retrieval/reflection), MemexAPI, FastAPI server
- **`packages/cli`** - Typer-based CLI (`memex` command)
- **`packages/mcp`** - FastMCP server for LLM integration (Claude, etc.)
- **`packages/common`** - Shared Pydantic models, config, exceptions
- **`packages/dashboard`** - Reflex-based web UI

### Three-Layer Memory Model (Hindsight Framework)

1. **Extraction** (`memex_core.memory.extraction`) - Extract facts from documents using LLM, resolve entities, generate embeddings
2. **Retrieval** (`memex_core.memory.retrieval`) - TEMPR architecture with 5 search strategies (Temporal, Entity, Mental Model, Keyword, Semantic) + Reciprocal Rank Fusion
3. **Reflection** (`memex_core.memory.reflect`) - Synthesize observations into mental models using LLM reasoning

### Key Entry Points

| Module | Purpose |
|--------|---------|
| `memex_core.api.MemexAPI` | Main API class |
| `memex_core.server` | FastAPI REST server |
| `memex_core.memory.engine.MemoryEngine` | Memory orchestrator |
| `memex_cli.__init__.app` | Typer CLI app |
| `memex_mcp.server.mcp` | MCP server instance |

## Code Style

- **Quotes**: Single (`'`)
- **Line length**: 100
- **Formatter**: Ruff (not Black)
- **Type hints**: Strict (mypy enforced)
- **Python**: >= 3.12
- **Async**: All I/O uses asyncio

## Testing

- **Root tests** (`/tests/`) - E2E tests
- **Unit tests** (`packages/core/tests/unit/`)
- **Markers**: `@pytest.mark.integration`, `@pytest.mark.llm`
- **Test DB**: PostgreSQL via `testcontainers`

### Test Best Practices

- **Idempotency**: Use `uuid4()` in content to prevent `idempotency_check` failures
- **LLM Bypass**: Use `skip_opinion_formation=True` in payloads to avoid external API calls in tests that don't need LLM
- **Environment Isolation**: Use `patch.dict(os.environ, ...)` for config tests
- **E2E Tests**: Ensure `ensure_db_env_vars` fixture is active

## Architectural Decisions

- **Distributed Reflection Queue**: Uses PostgreSQL `SELECT ... FOR UPDATE SKIP LOCKED` for atomic task claiming across multiple workers
- **Append-Only Design**: Notes are immutable; new versions create new entries
- **fsspec Abstraction**: Storage is backend-agnostic (local, S3, GCS)

## Active role

Your current **active role** (if exists) is stored in @.gemini/persona/active_role.json

## Active skills

The following skills should be loaded by default:

- Python expert: @/home/vscode/workspace/.persona/skills/expert_python_skill/SKILL.md

## Planning mode

Invoked by '/planning'.

**CRITICAL**: In planning mode you must **strictly** follow (a) 'no changes protocol' and (b) 'planning protocol' **UNTIL** told otherwise by the user.

<!-- MEMEX CLAUDE CODE INTEGRATION -->
## Memex Memory Integration

You have access to **Memex**, a long-term memory system, via MCP tools. Use these to
build persistent knowledge across sessions.

### Proactive memory capture

Call `memex_add_note` (with `background: true`) when you encounter:

- **Architectural decisions** or design rationale
- **Bug root causes** and their fixes
- **User preferences** and workflow patterns
- **Important project context** that would be useful in future sessions
- **Key technical discoveries** or learnings

**Do NOT capture**: trivial exchanges, routine code edits, debugging noise, or
information already in the codebase.

### Memory retrieval

**Session start context is automatic.** The `on_session_start` hook prefills recent
memories into your context. Do NOT call `memex_search` or `memex_note_search` to
"load context" at the start of a session — it is already present.

**PROHIBITED:** Do NOT use `memex_list_notes` for discovery. Note titles are often
"Untitled" and the listing provides no useful information.

### Memory search vs. Note search

These are two distinct search interfaces. Choose based on what you need:

**`memex_search` — Memory Search (atomic facts, cross-vault knowledge graph)**
- Searches over atomic facts, observations, and mental models using the TEMPR
  architecture (Temporal, Entity, Mental Model, Probabilistic Ranking).
- Use when: you want a synthesized answer spanning the entire knowledge base,
  exploring entity relationships, or asking general questions.
- Think: "What do we know about X globally?"

**`memex_note_search` — Note/Document Search (raw source notes, scoped)**
- Searches raw source notes (PDFs, Markdown, web scrapes) split into semantic
  nodes, using hybrid RRF (Semantic + Keyword + Entity Graph + Temporal).
- Supports `reason=True` (identify relevant sections) and `summarize=True`
  (synthesize an answer from matched sections).
- Use when: you need a specific quote or paragraph, want original formatting/context,
  or are searching within specific notes or vaults.
- Think: "Find the note where X is mentioned."

### Note reading protocol

When you need to read note content from a Note ID:

1. **Step 1:** Call `memex_get_page_index` with the Note ID to get the table of contents.
2. **Step 2:** Call `memex_get_node` with the relevant node ID(s) to read specific sections.
3. **Fallback only:** Use `memex_read_note` only for very short notes or when the page
   index is unavailable. Never use it as the first step.

### Slash commands

- `/remember [text]` — explicitly save something to memory
- `/recall [query]` — search your memories
