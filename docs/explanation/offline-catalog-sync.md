# Offline Catalog Sync Pattern

**Date**: 2026-03-21
**Status**: Proposed
**Related**: homelab-research ADR-003 (dual-write knowledge catalog)

## Problem

Memex runs as a Docker service on the homelab. When the homelab is offline (power outage, maintenance, travel), Claude Code sessions on the Mac or Windows machine cannot query Memex. This creates a gap in the Research-First Protocol — the Memex archive layer becomes unreachable.

## Proven Pattern: homelab-research catalog_sync

The `homelab-research` repo solved an identical problem with a dual-write catalog:

1. **Local SQLite** (`.gitignore`'d, per-machine) — always available, rebuilt by post-merge hook
2. **Remote Postgres** (Memex instance) — shared, fire-and-forward (failure logged, not blocking)

Implementation: `homelab-research/scripts/catalog_sync.py`
- Parses YAML frontmatter from all markdown files
- Computes freshness scores (evergreen/slow/fast decay model)
- Writes to SQLite + FTS5 for full-text search
- Optionally writes to Postgres (`knowledge_catalog` schema)
- Post-merge git hook keeps local catalog fresh

## Proposed Architecture for Memex

Replicate the same pattern for Memex data:

```
Memex Postgres (primary, online)
    ↓ export
Local SQLite cache (per-machine, offline)
    ↓ query
Claude Code / QMD / local tools
```

### Export Script: `memex-export-local`

A script (in the memex repo or xebia-toolbox) that:

1. Connects to Memex Postgres via Meshnet/localhost
2. Exports key tables to local SQLite:
   - `notes` (id, title, content, created_at, updated_at, status)
   - `memory_units` (id, note_id, content, embedding metadata)
   - `entities` (id, name, type, mention_count)
   - `entity_cooccurrences` (entity pairs + counts)
   - `kv_store` (key-value facts)
3. Creates FTS5 index on note content and memory units
4. Stores last export timestamp for incremental updates
5. `.gitignore`'d SQLite file (`memex-local.db`)

### Query Interface

- Claude Code queries via `sqlite3 memex-local.db` in Bash (zero infra)
- Or: lightweight MCP server that wraps the SQLite for structured queries
- Falls back gracefully: if Memex API is up → use it; if down → use local cache

### Sync Strategy

```
Option A: Scheduled cron (e.g., daily)
Option B: On-demand `memex-export-local --refresh`
Option C: Git hook in a sync repo (like homelab-research)
```

Recommended: **Option B** with a wrapper that checks staleness first. No git hook needed since Memex data isn't in a git repo.

### Consistency Model

- Memex Postgres is authoritative — local SQLite is a read-only cache
- Stale data is acceptable (archive doesn't change rapidly)
- Conflict resolution: none needed (one-way export)
- Export records Postgres `last_updated` watermark for incremental sync

## Implementation Phases

### Phase 1: Read-only export (MVP)
- Script exports notes + memory units to SQLite
- FTS5 index for search
- Manual trigger: `memex-export-local --refresh`

### Phase 2: Incremental sync
- Track watermark (last export timestamp)
- Only export records updated since last sync
- Reduces export time from minutes to seconds

### Phase 3: MCP integration
- Lightweight MCP server wrapping SQLite
- Same tool names as Memex MCP but backed by local cache
- Auto-fallback: try Memex API first, then local cache

## Key Differences from homelab-research Pattern

| Aspect | homelab-research | Memex |
|--------|-----------------|-------|
| Source of truth | Git (markdown files) | Postgres |
| Sync direction | Files → SQLite + Postgres | Postgres → SQLite |
| Trigger | Post-merge git hook | Manual / cron |
| Write access | Both targets writable | SQLite is read-only cache |
| Schema | Custom (sources table) | Mirrors Memex schema |

## Open Questions

- Should the export include embeddings (large, ~1GB for 500+ notes)?
- Should the local MCP server share config with the remote Memex MCP?
- What's the right granularity for memory_units export (full content vs. summaries)?
