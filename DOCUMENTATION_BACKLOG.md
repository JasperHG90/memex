# Documentation Backlog

Tracked documentation issues that need further investigation or owner input.

## High Priority

### LoCoMo benchmark number inconsistency
- **Files**: `README.md`, `packages/eval/README.md`, `docs/reference/evaluation-report.md`
- `README.md` reports 47 QA pairs with scores: SH=0.944, MH=1.0, T=1.0, NA=0.986, Adv=0.773
- `packages/eval/README.md` reports 57 QA pairs (60 - 3 image-dependent) with different scores
- `docs/reference/evaluation-report.md` references 50 questions in retrieval path analysis
- **Action needed**: Owner must determine which benchmark run is authoritative and align all three documents.

### CORS default includes wrong dashboard port
- **File**: `packages/common/src/memex_common/config.py` (line 596)
- CORS origins default is `['http://localhost:5173', 'http://localhost:3000']`
- Dashboard default port is `3001` (config.py line 899), not `3000`
- `docs/reference/configuration.md` line 57 correctly reflects the code, but the code itself is likely wrong
- **Action needed**: Owner must decide if CORS default should be `3001` to match the dashboard default. This is a code bug, not just a docs issue.

## Medium Priority

### README.md:16 — `GEMINI_API_KEY` clarity
- The env var is technically correct (LiteLLM reads it), but it's confusing alongside the `MEMEX_` prefix config system.
- **Action needed**: Consider rephrasing to mention both approaches or linking directly to `config init`.

### eval/README.md:30 — Port 9000 in example
- `--server http://localhost:9000/api/v1/` uses port 9000, but default Memex port is 8000.
- Intentional as a "custom URL" example but could confuse copy-pasters.
- **Action needed**: Clarify or change to port 8000.

### docs/how-to/openclaw-integration.md:57 — `MEMEX_VAULT_NAME`
- This is an OpenClaw-specific env var (not a Memex `MEMEX_` prefix var). Could use clarification.
- **Action needed**: Add a note distinguishing it from Memex config env vars.

## Low Priority

### Dependency version inconsistencies across pyproject.toml files
- pyyaml ranges differ (6.0.0 / 6.0.1 / 6.0.3)
- httpx ranges differ (0.27 / 0.27.0 / 0.28.1)
- Not strictly docs issues but worth tracking for consistency.

### configure-memex.md placeholder inconsistency
- Uses `YOUR_API_KEY`, `YOUR_KEY`, and `YOUR_OPENAI_KEY` interchangeably in YAML examples.
- **Action needed**: Standardize placeholder names.
