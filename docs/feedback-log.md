# Memex Feedback Log — Living Document

**Purpose**: Capture ongoing feedback (bugs, friction, good things, ideas) as we use Memex in production. Both Windows and Mac sessions append here. Periodically compiled into structured feedback for Jasper.

**Convention**: Tag memex notes with `upstream-feedback` for automatic discovery.
**Compiled report**: `docs/upstream-feedback-report.md` (initial version from Session 3)

---

## How to Add Entries

Append under the appropriate section. Include date, machine, and a short description. Keep entries brief — we'll expand when compiling the report.

---

## Bugs / Difficulties

| Date | Machine | Issue | Severity | Status |
|------|---------|-------|----------|--------|
| 2026-03-04 | Windows | PG18 PGDATA volume mount conflict | Medium | Fixed (documented in report) |
| 2026-03-04 | Windows | ONNX model download race with multiple workers | Medium | Workaround (WORKERS=1) |
| 2026-03-05 | Windows | Pydantic HttpUrl trailing slash breaks Ollama routing | High | Fixed locally (6 files) |
| 2026-03-05 | Windows | Gunicorn 30s timeout kills workers during extraction | High | Fixed (GUNICORN_TIMEOUT env var) |
| 2026-03-09 | Windows | MCP SSE session lost on container restart — requires full Claude Code restart | Medium | No fix, documented |
| 2026-03-17 | Windows | `memex_memory_search` returns "Search failed:" with no details when using `after` date filter | Low | Needs investigation |
| 2026-03-17 | Windows | Memex background ingestion triggers Ollama gemma3:12b at 128K context (no num_ctx cap) — eats all 16GB VRAM | High | Our config issue, not memex bug — but LiteLLM integration docs could mention this |

## Good Things

| Date | Machine | What |
|------|---------|------|
| 2026-03-04 | Windows | Knowledge graph + entity extraction works well — co-occurrences are genuinely useful for discovery |
| 2026-03-07 | Windows | `memex_add_note` with `background: true` is excellent — non-blocking ingestion |
| 2026-03-10 | Both | YAML frontmatter extraction (v0.0.8a) — good improvement for structured note metadata |
| 2026-03-15 | Both | TEMPR retrieval strategies — multi-strategy fusion gives better results than pure semantic search |
| 2026-03-17 | Windows | Cross-session knowledge persistence genuinely works — Mac session finds Windows session notes via memex |

## Improvement Ideas

| Date | Machine | Idea | Priority |
|------|---------|------|----------|
| 2026-03-05 | Windows | Configurable Gunicorn timeout as first-class config | High |
| 2026-03-05 | Windows | Fix HttpUrl trailing slash at config validator level | High |
| 2026-03-09 | Windows | MCP SSE session recovery after container restart | Medium |
| 2026-03-05 | Windows | ONNX model preload on startup (FastAPI lifespan hook) | Medium |
| 2026-03-05 | Windows | Document multi-worker ONNX race condition | Low |
| 2026-03-17 | Windows | Better error messages on search failures (include reason, not just "Search failed:") | Medium |
| 2026-03-17 | Both | Backup/replica strategy documentation — how to run warm standby on second machine | Medium |

## Raw Notes (unstructured, to be categorized)

_Append quick observations here during sessions. Move to appropriate table when reviewing._
