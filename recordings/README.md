# Recording GIFs for Memex

This directory contains the tooling to produce the animated GIF demos shown in the project README.

## Prerequisites

| Tool | Purpose | Install |
|------|---------|---------|
| [asciinema](https://asciinema.org) | Record CLI terminal sessions | `uv tool install asciinema` |
| [agg](https://github.com/asciinema/agg) | Convert asciinema recordings to GIF | [GitHub releases](https://github.com/asciinema/agg/releases) |
| ffmpeg | Video encoding (used by dashboard recordings) | `apt install ffmpeg` / `brew install ffmpeg` |
| Node.js 18+ | Run Playwright dashboard recordings | [nodejs.org](https://nodejs.org) |
| Running Memex server | Serve API requests during recording | `memex server start -d` |
| Running dashboard | Needed for dashboard recordings | `just dashboard-dev` |

## Quick start

```bash
# 1. Install recording dependencies
just recording-setup

# 2. Start the server and dashboard (in separate terminals)
memex server start -d
just dashboard-dev

# 3. Seed the database with demo data
just recording-seed

# 4. Record everything
just record-all
```

Output GIFs are written to `assets/` at the repository root.

## Re-recording individual GIFs

### CLI recordings (asciinema + agg)

The `cli/record-cli.sh` script uses asciinema to record terminal sessions and agg to convert them to GIFs. Run all or specific recordings:

```bash
# Record all CLI GIFs
bash recordings/cli/record-cli.sh

# Record a specific one
bash recordings/cli/record-cli.sh memory_search
bash recordings/cli/record-cli.sh entity_list
```

Available recordings:

| Name | Output |
|------|--------|
| `memory_search` | `assets/memex_cli_memory.gif` |
| `memory_search_answer` | `assets/memex_cli_memory_answer.gif` |
| `note_search_reason` | `assets/memex_cli_docs.gif` |
| `entity_list` | `assets/memex_cli_entities.gif` |
| `stats_system` | `assets/memex_cli_stats.gif` |
| `memory_add_url` | `assets/memex_cli_ingest.gif` |

The `.tape` files in `cli/` are VHS format files kept for reference but are not used for recording (VHS has compatibility issues in containerized environments).

### Claude Code integration (simulated)

The `cli/record-claude-code.sh` script produces a standalone GIF showing Claude Code using Memex as long-term memory via MCP tools. This recording uses **simulated terminal output** (printf/echo with ANSI colors) rather than a live CLI session, since Claude Code interactions are non-deterministic.

```bash
bash recordings/cli/record-claude-code.sh
# or
just record-claude-code
```

| Name | Output |
|------|--------|
| `claude_code` | `assets/memex_claude_code.gif` |

### Dashboard recordings (Playwright)

Each script in `dashboard/scripts/` drives a headless browser to capture a dashboard interaction:

```bash
cd recordings/dashboard && npx tsx scripts/record-overview.ts
```

Available scripts:

| Script | Output |
|--------|--------|
| `dashboard/scripts/record-overview.ts` | `assets/memex_dashboard.gif` |
| `dashboard/scripts/record-entity-graph.ts` | `assets/memex_dashboard_entity_graph.gif` |
| `dashboard/scripts/record-memory-search.ts` | `assets/memex_dashboard_memory_search.gif` |
| `dashboard/scripts/record-knowledge-flow.ts` | `assets/memex_dashboard_knowledge_flow.gif` |
| `dashboard/scripts/record-lineage.ts` | `assets/memex_dashboard_lineage.gif` |

## Directory structure

```
recordings/
  cli/                   # CLI recording scripts
    record-cli.sh        # asciinema + agg recording driver
    *.tape               # VHS tape files (reference)
  dashboard/             # Playwright project for dashboard recordings
    scripts/             # Recording scripts (TypeScript)
    utils/               # Shared utilities (GifRecorder, wait-for-api)
    package.json
    tsconfig.json
  seed-data/             # Demo data seeder
    seed_demo_db.py
    demo_notes/          # Markdown notes for seeding
```

## Customizing recordings

### CLI recordings

Edit `cli/record-cli.sh` to change:

- **Theme**: `--theme` flag in `AGG_OPTS` (e.g., `dracula`, `monokai`)
- **Viewport**: `--cols` / `--rows` in `AGG_OPTS`
- **Font size**: `--font-size` in `AGG_OPTS`
- **Typing speed**: delay parameter in `type_command` function
- **Idle limit**: `--idle-time-limit` in `AGG_OPTS`

### Playwright scripts (Dashboard)

Edit the TypeScript files in `dashboard/scripts/` to change:

- **Viewport**: `GifRecorder` constructor options `{ width, height }`
- **Frame rate**: `GifRecorder` constructor option `{ fps }`
- **Actions**: Click, scroll, hover sequences
- **Timing**: `page.waitForTimeout(ms)` between actions

The `GifRecorder` (in `dashboard/utils/recorder.ts`) captures periodic screenshots and stitches them into a GIF using ffmpeg's two-pass palette method for optimal quality.
