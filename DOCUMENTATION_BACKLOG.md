# Documentation Backlog

All issues from the deep documentation review have been addressed.

---

## Fixed

### Critical
1. **MCP tool count** — Reconciled to 26 across `README.md`, `packages/mcp/README.md`, `docs/reference/mcp-tools.md`, and `docs/index.md`. Added missing tools (find_note, list_notes, KV store) to tables.
2. **README eval metrics** — Synced with 2026-03-13 evaluation report (36 non-adversarial questions, 0.986 score, 4,609 median retrieval tokens).
3. **REST API reference gaps** — Added 7 missing endpoints: `GET /notes/{id}/metadata`, `POST /notes/metadata/batch`, `POST /nodes/batch`, `PATCH /notes/{id}/date`, `PATCH /notes/{id}/title`, `POST /notes/{id}/migrate`, `POST /entities/batch`.
4. **Dashboard warning** — Removed outdated "under construction" warning from README.
5. **CLAUDE.md packages** — Added `packages/eval` and `packages/claude-code-plugin` to architecture list.
6. **Claude Code setup guide** — Updated to recommend plugin-first, per-project setup as alternative.
7. **`memory add` vs `note add`** — Documented as legacy alias in CLI reference.
8. **MemexAPI reference** — Created `docs/reference/memexapi-reference.md` with 60+ public methods. Added to README and docs index.
9. **Install command** — Standardized across README and getting-started tutorial.
10. **Python 3.13 + uv version** — Documented in README requirements and tutorial prerequisites.
11. **Docker compose** — README now says `docker compose up -d postgres` instead of vague pointer.
12. **Placeholder env vars** — `.devcontainer/.env.example` now uses `your-*-here` pattern.
13. **qmd collection names** — CLAUDE.md corrected: `memex_test` = all source, `memex_src` = test files, `memex_md` = markdown.
14. **Config naming** — CLI reference now lists all three accepted local config filenames (`memex_core.yaml`, `.memex.yaml`, `memex_core.config.yaml`) matching `docs/reference/configuration.md`. Resolution order fixed to match code priority.
15. **docs/index.md** — Fixed stale "22 MCP tools" count, added MemexAPI reference link.
