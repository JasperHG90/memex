# Homelab Deployment Guide

Deploying Memex on a Windows 11 homelab with Docker Compose and local Ollama.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  Docker Compose                                     │
│                                                     │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────┐  │
│  │ db       │  │ api      │  │ mcp (SSE :8081)  │  │
│  │ PG18 +   │◄─┤ FastAPI  │◄─┤ FastMCP bridge   │  │
│  │ pgvector │  │ :8000    │  │                   │  │
│  └──────────┘  └────┬─────┘  └──────────────────-┘  │
│                     │         ▲                      │
│  ┌──────────────┐   │         │ SSE transport       │
│  │ dashboard    │   │         │                      │
│  │ nginx :5173  ├───┘         │                      │
│  └──────────────┘             │                      │
└───────────────────────────────┼──────────────────────┘
                                │
              ┌─────────────────┼─────────────────┐
              │ Claude Code     │  Other devices   │
              │ (local or       │  via Meshnet     │
              │  remote)        │                  │
              └─────────────────┘──────────────────┘
                                │
              ┌─────────────────┘
              ▼
        ┌──────────┐
        │ Ollama   │  host.docker.internal:11434
        │ gemma3   │  (runs on host GPU)
        │ :12b     │
        └──────────┘
```

## Prerequisites

- Docker Desktop for Windows with Compose v2
- Ollama running on the host with `gemma3:12b` pulled (`ollama pull gemma3:12b`)
- Git + GitHub CLI (`gh`)

## Quick Start

```bash
# 1. Clone and checkout
gh repo clone RicardoAGL/memex
cd memex
git checkout feat/xebia-testing

# 2. Create .env with your PostgreSQL password
cp env.template .env
# Edit .env — set MEMEX_DB_PASSWORD to a secure password

# 3. Build and start
docker compose build
docker compose up -d

# 4. Verify (wait ~30s for first-boot model downloads)
docker compose ps
docker exec memex-api-1 python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8000/api/v1/health').read().decode())"
# Expected: {"status":"ok"}
```

## Configuration Files

| File | Purpose | Committed? |
|------|---------|------------|
| `docker-compose.yaml` | Upstream base config (DO NOT MODIFY) | Yes |
| `docker-compose.override.yaml` | Homelab overrides (Ollama, DB password, MCP SSE, PG18 fix) | No* |
| `.env` | Secrets (`MEMEX_DB_PASSWORD`) | No |
| `env.template` | Template for `.env` creation | Yes |

\* Add to `.gitignore` if pushing homelab-specific changes.

## Services

| Service | Host Port | Internal Port | Purpose |
|---------|-----------|---------------|---------|
| db | 5432 | 5432 | PostgreSQL 18 + pgvector |
| api | 8000 | 8000 | Memex FastAPI server |
| dashboard | 5173 | 80 | React knowledge graph UI |
| mcp | 8081 | 8081 | MCP bridge (SSE transport) |

## LLM Configuration

Default: **Ollama `gemma3:12b`** (8.1 GB, fits in 16 GB VRAM).

Containers reach the host Ollama via `host.docker.internal:host-gateway`.

### Available Ollama Models (16 GB VRAM)

| Model | Size | Notes |
|-------|------|-------|
| `gemma3:12b` | 8.1 GB | Default — good balance of quality and speed |
| `qwen3:14b` | 9.3 GB | Strong reasoning, good for extraction |
| `qwen3:8b` | 5.2 GB | Fastest, good for lighter tasks |
| `ministral-3:14b` | 9.1 GB | Mistral family alternative |

All fit comfortably in 16 GB VRAM. Models >14B parameters (e.g., `gemma3:27b` at 17 GB) will not fit.

### Switching Models

Edit `docker-compose.override.yaml`:

```yaml
# Ollama (local, free) — use any model from the table above
MEMEX_SERVER__DEFAULT_MODEL__MODEL: "ollama_chat/gemma3:12b"
MEMEX_SERVER__DEFAULT_MODEL__BASE_URL: "http://host.docker.internal:11434"

# Anthropic (requires API key in .env)
# MEMEX_SERVER__DEFAULT_MODEL__MODEL: "anthropic/claude-sonnet-4-20250514"
# MEMEX_SERVER__DEFAULT_MODEL__API_KEY: ${ANTHROPIC_API_KEY}
```

Per-component overrides are also possible (e.g., different model for reflection):

```yaml
MEMEX_SERVER__MEMORY__REFLECTION__MODEL__MODEL: "ollama_chat/qwen3:14b"
```

**Important:** When using Ollama, always prefix model names with `ollama_chat/` (LiteLLM provider prefix). After changing models, restart the API: `docker compose restart api`.

## Known Issues & Fixes

### 1. PG18 Volume Layout (`PGDATA` conflict)

**Symptom:** PostgreSQL 18 container fails to start with `initdb: error: directory "/var/lib/postgresql/data" exists but is not empty`.

**Cause:** PG18+ (trixie) images require `PGDATA` to be a subdirectory under the mount, not the mount root itself.

**Fix:** Set `PGDATA` to a subdirectory in the override:
```yaml
PGDATA: /var/lib/postgresql/data/pgdata
```

### 2. API Command Changed (silent exit)

**Symptom:** API container starts and exits immediately with code 0. No error in logs.

**Cause:** The Memex CLI changed from `memex server` to `memex server start` (subcommand). The Dockerfile's default `CMD ["server"]` is outdated.

**Fix:** Override the command:
```yaml
command: ["server", "start"]
```

### 3. ONNX Model Download Race (worker crash on first boot)

**Symptom:** On first start, API crashes with `onnxruntime.capi.onnxruntime_pybind11_state.InvalidProtobuf: Load model from ... failed:Protobuf parsing failed` or `Deserialize tensor ... failed ... out of bounds`.

**Cause:** Gunicorn spawns multiple workers. Worker 1 starts downloading ONNX models from HuggingFace. Worker 2 tries to load the partially-downloaded `.onnx` file, hits a corrupt protobuf, and crashes. Gunicorn then kills all workers.

**Fix (two-part):**
1. Set `MEMEX_WORKERS=1` to avoid the race entirely:
   ```yaml
   MEMEX_WORKERS: "1"
   ```
2. Add a named volume for the model cache so downloads persist across restarts:
   ```yaml
   volumes:
     - model_cache:/root/.cache/memex
   ```
   And declare it in the `volumes:` section:
   ```yaml
   volumes:
     model_cache:
   ```

After a clean first boot, models are cached in the volume. If the cache gets corrupted, remove the volume and restart:
```bash
docker compose down api
docker volume rm memex_model_cache
docker compose up -d api
```

### 4. Port 8080 Conflict

**Symptom:** `docker compose up` fails with `Bind for 0.0.0.0:8080 failed: port is already allocated`.

**Cause:** Port 8080 is occupied by another service on the host (common with development tools).

**Fix:** MCP service is configured on port 8081 instead. If 8081 is also taken, change in the override:
```yaml
ports:
  - "9081:8081"  # any available port
```

### 5. Ollama 405 Method Not Allowed (LLM calls fail)

**Symptom:** Memory extraction or reflection fails with `litellm.InternalServerError: OllamaException - Ollama returned 405 (Method Not Allowed)`. API logs show requests to `http://host.docker.internal:11434//api/chat` (note the double slash).

**Cause:** Pydantic v2 `HttpUrl` type automatically appends a trailing slash to URLs (e.g., `http://host:11434` becomes `http://host:11434/`). When LiteLLM constructs the API path, it prepends `/api/chat`, creating `http://host:11434//api/chat`. Ollama rejects the double-slash path with 405.

**Fix:** Added `.rstrip('/')` to all 6 locations where `base_url` is passed to `dspy.LM`:
- `packages/core/src/memex_core/api.py` (line ~317)
- `packages/core/src/memex_core/memory/engine.py` (lines ~60, ~321)
- `packages/core/src/memex_core/memory/extraction/engine.py` (line ~69)
- `packages/core/src/memex_core/memory/reflect/reflection.py` (lines ~90, ~101)

Pattern applied:
```python
# Before (broken)
api_base=str(model_config.base_url) if model_config.base_url else None,

# After (fixed)
api_base=str(model_config.base_url).rstrip('/') if model_config.base_url else None,
```

**Note:** This fix requires rebuilding the Docker images: `docker compose build api mcp && docker compose up -d api mcp`.

### 6. Docker Compose Port Merging (can't restrict to localhost)

**Symptom:** Attempting to override `ports: - "127.0.0.1:5432:5432"` in the override results in duplicate port bindings (both the base and override ports are published).

**Cause:** Docker Compose merges list-type directives (ports, volumes) from base and override files. You cannot replace or remove a port mapping defined in the base file.

**Fix:** Accept both bindings and control access via Windows Firewall rules instead. The `scripts/meshnet-setup.ps1` script blocks external access to API (8000) and DB (5432) ports from the Meshnet range.

### 7. Docker BuildKit Network Failures (transient)

**Symptom:** `docker compose build` fails with `Could not resolve 'deb.debian.org'` or similar DNS errors during `apt-get update`, but `docker run` with the same base image works fine.

**Cause:** Docker BuildKit uses a different network stack than `docker run`. On Windows with Docker Desktop, BuildKit occasionally has transient DNS resolution failures.

**Fix:** Simply retry the build. This is intermittent and usually succeeds on the second attempt:
```bash
docker compose build --no-cache
```

## Operations

```bash
# Service status
docker compose ps

# Recent API logs
docker compose logs api --since 60s

# Restart API only
docker compose restart api

# Full restart (preserves data)
docker compose down && docker compose up -d

# Full reset (DESTROYS ALL DATA)
docker compose down -v && docker compose up -d

# Check stats
docker exec memex-api-1 python -c \
  "import urllib.request, json; \
   r=urllib.request.urlopen('http://localhost:8000/api/v1/stats/counts'); \
   print(json.dumps(json.loads(r.read()), indent=2))"
```

## Connecting Claude Code

See [Connecting Claude Code to Homelab Memex](homelab-claude-code-setup.md).

**Quick start** — copy `mcp-template.json` to your project as `.mcp.json`:

```bash
cp C:\github\memex\mcp-template.json /path/to/your/project/.mcp.json
```

## NordVPN Meshnet Setup

Meshnet provides network-level access control — only your linked devices can reach the homelab.

### Current Meshnet Config

| Adapter | IP | Purpose |
|---------|-----|---------|
| NordLynx | 100.119.180.187 | Meshnet peer IP (use this in remote configs) |

### Firewall Rules

Run `scripts/meshnet-setup.ps1` **as Administrator** to create firewall rules:

```powershell
# In an elevated PowerShell:
powershell -ExecutionPolicy Bypass -File C:\github\memex\scripts\meshnet-setup.ps1
```

This creates:
- **ALLOW** port 8081 (MCP SSE) from Meshnet range `100.64.0.0/10`
- **ALLOW** port 5173 (Dashboard) from Meshnet range
- **BLOCK** port 8000 (API) from Meshnet range
- **BLOCK** port 5432 (DB) from Meshnet range

### Verification

Run `scripts/test-meshnet.ps1` to verify connectivity:

```powershell
powershell -ExecutionPolicy Bypass -File C:\github\memex\scripts\test-meshnet.ps1
```

### NordVPN App Settings

In the NordVPN GUI under "Devices in Meshnet":
- Ensure **Meshnet** is enabled
- Ensure **"Remote access to your device"** is toggled ON for each peer device
- You do NOT need "Traffic routing" enabled

> **Note:** NordVPN on Windows has no CLI for Meshnet management — use the GUI app.

## Security Model

| Layer | What it protects | How |
|-------|-----------------|-----|
| **Meshnet** | Network access | Only your linked NordVPN devices can reach the IP |
| **Windows Firewall** | Port access | Only MCP + Dashboard ports open to Meshnet range |
| **Docker internal network** | API + DB | MCP/Dashboard containers access API internally; no direct external path |

- API and DB are accessible from `localhost` for local development
- PostgreSQL password is read from `.env` — never hardcoded
- Dashboard proxies `/api/` to the API service internally
