# Developer Feedback: Real-World Memex Deployment on Windows Homelab

Thank you for building Memex -- it is an excellent system and we have been running it successfully as persistent long-term memory for Claude Code sessions. This report summarizes our deployment experience, the issues we encountered, the workarounds we applied, and some suggestions that could benefit other users running Memex with local LLMs.

## Usage Context

- **Environment**: Windows 11 homelab, Docker Compose stack (v0.0.7a fork, branch `feat/xebia-testing`)
- **Services**: db (pgvector/PG18), api, mcp (SSE transport on port 8081), dashboard, plus a LiteLLM proxy sidecar for LLM routing
- **Use case**: Long-term memory for Claude Code sessions via MCP SSE transport, accessed locally and remotely over NordVPN Meshnet
- **LLM backend**: Ollama `gemma3:12b` running on the host GPU (primary, free), with Gemini Flash as a paid fallback -- routed through a LiteLLM proxy container
- **Scale**: Single-user, ~50 notes ingested, daily interactive use via MCP tools
- **Ingestion performance**: ~8 minutes per note on local Ollama (the extraction pipeline makes multiple LLM calls per note, which is expected but worth documenting for users choosing local models)
- **Worker config**: Single worker (`MEMEX_WORKERS=1`) to prevent ONNX race conditions

## Custom Additions

These are changes we made in our fork. We are happy to contribute any of these upstream as PRs if they would be useful.

### 1. Configurable Gunicorn Timeout (`GUNICORN_TIMEOUT` env var)

**File**: `packages/cli/src/memex_cli/server.py`

Added support for a `GUNICORN_TIMEOUT` environment variable (default: 120s) that is passed as `--timeout` to Gunicorn. The upstream default of 30 seconds is insufficient when ONNX models need to reload or when LLM calls go through slower local models. Without this, Gunicorn kills workers with SIGKILL mid-request.

### 2. Pydantic `HttpUrl` Trailing Slash Fix

**Files**: 6 locations across `memex_core` where `base_url` is passed to `dspy.LM`

Pydantic v2's `HttpUrl` type appends a trailing slash to URLs (e.g., `http://host:4000` becomes `http://host:4000/`). When downstream libraries (LiteLLM, dspy) construct API paths, this creates double-slash URLs like `http://host:4000//v1/chat/completions`, which Ollama and some other backends reject with HTTP 405.

Applied `.rstrip('/')` to `str(model_config.base_url)` in all 6 `dspy.LM` initialization sites:
- `packages/core/src/memex_core/api.py`
- `packages/core/src/memex_core/memory/engine.py`
- `packages/core/src/memex_core/memory/extraction/engine.py`
- `packages/core/src/memex_core/memory/reflect/reflection.py` (2 locations)

### 3. LiteLLM Proxy Integration

Added a `litellm-proxy` service to `docker-compose.override.yaml` with a `litellm_config.yaml` config file. This provides multi-model LLM fallback (local Ollama to paid Gemini) without modifying Memex source code -- Memex just sees an OpenAI-compatible endpoint at `http://litellm-proxy:4000`.

### 4. Docker Volume for ONNX Model Cache

Added a `model_cache` named volume mounted at `/root/.cache/memex` on the API container. This prevents ONNX model re-downloads on container restarts, which saves ~2 minutes on cold boot and eliminates the download race condition when multiple workers are configured.

### 5. SSE Transport for Remote MCP Access

Configured the MCP service with `--transport sse --port 8081` for remote access over Meshnet. The default stdio transport only works for local MCP clients.

### 6. Homelab Deployment Documentation

Created `docs/homelab-deployment.md` and `docs/homelab-troubleshooting.md` covering the full setup process, architecture diagram, known issues, and operational runbook. Happy to share these if useful for the official docs.

## Suggested Improvements

### 1. First-Class Configurable Gunicorn Timeout

Currently the 30-second Gunicorn timeout is hardcoded. For users running local LLMs (Ollama, llama.cpp) where inference can take 30-120 seconds, this causes silent worker kills.

**Suggestion**: Accept a `GUNICORN_TIMEOUT` env var or `--timeout` CLI flag on `memex server start`, defaulting to something more generous (120s or matching `LLM_TIMEOUT_SECONDS`).

### 2. Fix Pydantic `HttpUrl` Trailing Slash Upstream

This affects anyone using `base_url` with backends that reject double-slash paths. The fix is a one-line `.rstrip('/')` at each `dspy.LM` call site, or alternatively, a model validator on the config class that strips the trailing slash when the URL is set.

A config-level fix might be cleaner:

```python
# In the model config class
@field_validator('base_url', mode='after')
@classmethod
def strip_trailing_slash(cls, v: HttpUrl | None) -> HttpUrl | None:
    if v is not None:
        return HttpUrl(str(v).rstrip('/'))
    return v
```

### 3. ONNX Model Preload on Startup

Currently, ONNX embedding models are loaded on the first request, not at startup. Combined with the 30-second Gunicorn timeout, this means the first search after a cold boot often kills the worker.

**Suggestion**: Add a startup preload step (e.g., in the FastAPI `lifespan` or as a Gunicorn `post_fork` hook) that loads the embedding model before accepting requests. This would also make the `/api/v1/ready` endpoint more accurate -- it could return "not ready" until models are loaded.

### 4. Support for External LLM Providers via Configurable `base_url`

The current config supports `base_url` for the LLM model, which is great. However, the documentation could be clearer about using it with proxy setups (LiteLLM, OpenRouter, etc.) where you point `base_url` at a proxy and use `model: "openai/model-name"` with a dummy API key. This is a powerful pattern for multi-model fallback that works without any code changes.

### 5. MCP SSE Session Recovery

When the MCP container restarts, all SSE sessions are invalidated. Clients must fully restart to reconnect. For long-running Claude Code sessions, this is disruptive.

**Suggestion**: Consider implementing session recovery (e.g., session tokens that survive server restarts via Redis or database-backed session state) or at minimum, return a clear error code that clients can use to trigger automatic reconnection.

### 6. Document Multi-Worker ONNX Race Condition

With `MEMEX_WORKERS > 1`, multiple Gunicorn workers race to download the ONNX model on first boot. Worker 2 tries to load a partially-downloaded `.onnx` file, hits a corrupt protobuf, and crashes.

**Suggestion**: Either document `MEMEX_WORKERS=1` as the recommended setting for first boot, or add a file lock around the model download/load path. The `model_cache` volume mitigates this after first boot, but the initial download remains racy.

## Bugs Found

### 1. Gunicorn Default 30s Timeout Kills Workers During Heavy Operations

**Severity**: High (causes crash loops)

**Reproduction**: Configure Memex with a local Ollama backend. Ingest a note. The extraction pipeline makes multiple LLM calls, each taking 10-30 seconds. If a single request chain exceeds 30 seconds total, Gunicorn sends SIGKILL to the worker. The API container enters a crash-restart loop.

**Root cause**: Gunicorn's default `--timeout 30` is not configurable in the upstream `server.py`.

**Impact**: Any user with a slow LLM backend (local models, rate-limited API) will hit this.

### 2. Pydantic `HttpUrl` Appends Trailing Slash, Breaking LLM Routing

**Severity**: High (LLM calls fail with HTTP 405)

**Reproduction**: Set `MEMEX_SERVER__DEFAULT_MODEL__BASE_URL` to any URL without a trailing slash (e.g., `http://localhost:4000`). Pydantic's `HttpUrl` normalizes it to `http://localhost:4000/`. When `dspy.LM` constructs the API path, it produces `http://localhost:4000//v1/chat/completions`. Ollama (and some other backends) reject the double-slash path with 405 Method Not Allowed.

**Root cause**: `str(model_config.base_url)` returns the URL with trailing slash. The value is passed directly to `dspy.LM(api_base=...)` without stripping.

**Affected files**: 6 locations across `api.py`, `engine.py`, `extraction/engine.py`, `reflect/reflection.py`.

### 3. PG18 `PGDATA` Volume Mount Conflict

**Severity**: Medium (blocks fresh deployment on PG18)

**Reproduction**: Use `pgvector/pgvector:pg18-trixie` as the database image with the upstream `docker-compose.yaml` volume mount `pgdata:/var/lib/postgresql`. PostgreSQL 18 fails to start with `initdb: error: directory "/var/lib/postgresql/data" exists but is not empty`.

**Root cause**: PG18 (Debian trixie-based images) requires `PGDATA` to point to a subdirectory under the volume mount, not the mount root itself. The upstream compose file does not set `PGDATA` explicitly.

**Fix**: Set `PGDATA: /var/lib/postgresql/data/pgdata` in the db service environment. Alternatively, update the upstream compose to use `pgdata:/var/lib/postgresql/data` as the volume mount path.

### 4. ONNX Model Download Race with Multiple Workers

**Severity**: Medium (crashes on first boot with default settings)

**Reproduction**: Start Memex with `MEMEX_WORKERS > 1` on a fresh deployment (no cached models). Make a search request. The first worker begins downloading the ONNX embedding model. A second worker attempts to load the same file while the download is in progress, encounters a truncated/corrupt protobuf, and crashes with `onnxruntime.capi.onnxruntime_pybind11_state.InvalidProtobuf`.

**Root cause**: No file locking or download coordination between Gunicorn workers for the ONNX model download path.

**Workaround**: Set `MEMEX_WORKERS=1` and use a persistent Docker volume for the model cache.

---

We are grateful for Memex and the thoughtful architecture behind the Hindsight Framework. The extraction pipeline, TEMPR retrieval strategies, and knowledge graph design are well thought out. We hope this feedback is useful, and we are happy to contribute PRs for any of the fixes or improvements described above.
