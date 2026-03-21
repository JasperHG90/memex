# Homelab Troubleshooting Guide

## Gunicorn Worker Timeout (2026-03-10)

**Symptom**: MCP tools return "MCP error -32602: Invalid request parameters". API container shows workers being killed with SIGKILL. The `memex-api` container enters a crash loop.

**Root cause**: Gunicorn's default worker timeout is 30 seconds. When the ONNX embedding model needs to reload (after long uptime or container restart), the first search request triggers model loading + similarity search, which exceeds 30s. Gunicorn kills the worker mid-request.

**Fix applied**:
1. Added `GUNICORN_TIMEOUT` env var support to `packages/cli/src/memex_cli/server.py` (passes `--timeout` to Gunicorn, defaults to 120s)
2. Set `GUNICORN_TIMEOUT: "300"` in `docker-compose.override.yaml` to match `MEMEX_SERVER__LLM_TIMEOUT_SECONDS`

**Prevention**: The `model_cache` Docker volume persists ONNX model downloads, and `MEMEX_WORKERS=1` prevents race conditions on cold boot. But the timeout increase is still needed for operations that combine model loading with LLM calls.

**Recovery**: If the API enters a crash loop:
```bash
docker compose -f docker-compose.yaml -f docker-compose.override.yaml restart
```
After restart, MCP SSE sessions become stale. Claude Code sessions must restart to reconnect.

## MCP SSE Stale Sessions

**Symptom**: After any API/MCP container restart, existing Claude Code sessions get "Invalid request parameters" errors on MCP tool calls.

**Root cause**: SSE transport maintains server-side session state. When the MCP container restarts, all session IDs are invalidated. Clients must establish a new SSE connection.

**Fix**: Restart Claude Code (the client). There is no server-side fix — this is inherent to SSE transport.

## ONNX Model Cache Corruption

**Symptom**: API fails to start, errors mentioning ONNX model loading failures.

**Fix**:
```bash
docker volume rm memex_model_cache
docker compose -f docker-compose.yaml -f docker-compose.override.yaml up -d
```
The model will be re-downloaded on first start (~2 min).

## Ollama 405 Error (Base URL Trailing Slash)

**Symptom**: LLM calls via LiteLLM proxy return HTTP 405 (Method Not Allowed).

**Root cause**: Pydantic's `HttpUrl` type appends a trailing slash to base URLs. LiteLLM then constructs URLs like `http://host:4000//v1/chat/completions` (double slash), which Ollama rejects.

**Fix**: Applied `.rstrip('/')` on `base_url` in 6 source files. Already committed in `961fe3a`.

## LiteLLM Routing

**Config**: `litellm_config.yaml` with `simple-shuffle` routing strategy (NOT `latency-based`).

Current tiers:
1. `ollama_chat/gemma3:12b` via `host.docker.internal:11434` (local, free)
2. `gemini/gemini-2.5-flash` (paid fallback)

Note: `latency-based` routing was tried but it "cooled down" Ollama (preferring Gemini due to lower latency). `simple-shuffle` round-robins between available models.

## PG18 PGDATA Issue

**Symptom**: PostgreSQL 18 container fails to start.

**Root cause**: PG18 requires PGDATA to be under `/var/lib/postgresql/<version>/`, not at the volume root.

**Fix**: Set `PGDATA: /var/lib/postgresql/data/pgdata` in `docker-compose.override.yaml`.

## Environment Variable Changes

Docker Compose `restart` does NOT pick up env var changes from `docker-compose.override.yaml`. You must use:
```bash
docker compose -f docker-compose.yaml -f docker-compose.override.yaml up -d
```
This recreates containers with the new config.
