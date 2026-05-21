# Send Memex traces to Arize Phoenix

Memex exports OpenTelemetry traces over OTLP/HTTP. Point them at a running [Arize Phoenix](https://docs.arize.com/phoenix) instance and you get per-request timelines, LiteLLM call spans for every DSPy extraction or reflection, and session-grouped views of `memex_memory_search` calls.

This guide walks you through enabling tracing on an existing Memex server and confirming spans land in Phoenix.

## Prerequisites

- A running Memex server you can restart and edit config for. See [the configuration guide](../configure-memex.md) if you have not set one up yet.
- A reachable Phoenix OTLP endpoint. The Phoenix UI defaults to port `6006` and its trace receiver to `http://<host>:6006/v1/traces`. Self-hosted Phoenix (`pip install arize-phoenix && phoenix serve`) and Phoenix Cloud both work.
- The `tracing` extra installed on `memex-core`. The dependencies live behind an opt-in extra so default installs stay small. <code-ref path="packages/core/pyproject.toml" lines="73-79" />

  ```bash
  uv tool install "memex-cli[server,tracing] @ git+https://github.com/JasperHG90/memex.git@latest#subdirectory=packages/cli"
  ```

  If you already installed Memex without the extra, the server will refuse to start with tracing enabled and print `Install them with: uv add memex-core[tracing]`. <code-ref path="packages/core/src/memex_core/tracing.py" lines="33-43" />

## Procedure

### 1. Set the tracing config

Tracing lives under `server.tracing` in the config schema. Four fields matter, and all have defaults: <code-ref path="packages/common/src/memex_common/config.py" lines="1522-1540" />

| Field | Default | Purpose |
|---|---|---|
| `enabled` | `false` | Off by default. Set to `true` to wire up the exporter. |
| `endpoint` | `http://localhost:6006/v1/traces` | OTLP/HTTP receiver. The default matches local Phoenix. |
| `headers` | `{}` | Auth headers for hosted Phoenix or a gateway. |
| `service_name` | `memex` | Reported as `service.name` on every span. |

Pick one of two ways to set them.

**Option A — environment variables.** Memex maps nested config to env vars with the prefix `MEMEX_` and the delimiter `__`. <code-ref path="packages/common/src/memex_common/config.py" lines="2397-2400" />

```bash
export MEMEX_SERVER__TRACING__ENABLED=true
export MEMEX_SERVER__TRACING__ENDPOINT=http://localhost:6006/v1/traces
export MEMEX_SERVER__TRACING__SERVICE_NAME=memex-prod
```

**Option B — YAML.** Add a block to `~/.config/memex/config.yaml` (global) or `.memex.yaml` (per-project):

```yaml
server:
  tracing:
    enabled: true
    endpoint: http://localhost:6006/v1/traces
    service_name: memex-prod
```

For hosted Phoenix, add auth headers. The exporter sends every header on every span batch:

```yaml
server:
  tracing:
    enabled: true
    endpoint: https://app.phoenix.arize.com/v1/traces
    headers:
      api_key: "${PHOENIX_API_KEY}"
```

YAML supports `${VAR}` interpolation for secrets — keep the API key out of the file.

### 2. Restart the server

The tracing exporter is wired during startup; config changes do not hot-reload. <code-ref path="packages/core/src/memex_core/server/__init__.py" lines="92-96" /> Restart with whatever runs your server — `memex server start`, `systemctl restart memex`, your container orchestrator, or a process manager.

On boot you should see this log line:

```
OpenTelemetry tracing enabled, exporting to http://localhost:6006/v1/traces
```

If the endpoint is unreachable at startup, you instead get a warning — spans will buffer and retry, so the server still starts cleanly: <code-ref path="packages/core/src/memex_core/tracing.py" lines="68-79" />

```
Tracing endpoint http://localhost:6006/v1/traces is unreachable — spans will be buffered and retried.
```

## Verification

### Check the readiness endpoint

When tracing is enabled, `/api/v1/ready` reports its status alongside the database and file store: <code-ref path="packages/core/src/memex_core/server/health.py" lines="47-54" />

```bash
curl -s http://localhost:8000/api/v1/ready | jq
```

A healthy server returns:

```json
{"status": "ok", "database": "ok", "filestore": "ok", "tracing": "ok"}
```

### Generate traffic with a known session ID

The middleware reads an `X-Session-ID` header from each request and binds it to the active span via the OpenInference `using_session` context manager — so Phoenix can group spans into sessions. <code-ref path="packages/core/src/memex_core/server/__init__.py" lines="334-354" /> Send a search with a session ID you will remember:

```bash
curl -s http://localhost:8000/api/v1/memory/search \
  -H 'Content-Type: application/json' \
  -H 'X-Session-ID: phoenix-smoke-test' \
  -d '{"query": "anything in the vault"}' > /dev/null
```

### Find the trace in Phoenix

Open the Phoenix UI (default `http://localhost:6006`). The `memex` project should appear in the project list within a few seconds — the exporter batches spans before sending, so allow up to a minute on a quiet server.

In the **Sessions** tab, filter by `session.id = "phoenix-smoke-test"`. You should see one session containing the HTTP request span, the retrieval pipeline spans, and any LiteLLM child spans for query expansion or reranking — LiteLLM is auto-instrumented, so every DSPy call shows up without further setup. <code-ref path="packages/core/src/memex_core/tracing.py" lines="58-59" />

## Troubleshooting

### No spans appear in Phoenix

Check the four things, in order:

1. **The server log line.** No `OpenTelemetry tracing enabled` message means `enabled` is still `false` — re-check the env var or YAML key. Common typo: `MEMEX_SERVER__TRACE__ENABLED` (the field is `tracing`, not `trace`).
2. **The unreachable warning.** If the log shows `Tracing endpoint ... is unreachable`, the address resolves but Phoenix is not accepting connections there. Confirm Phoenix is up (`curl http://localhost:6006/`) and that the path is `/v1/traces`, not `/v1/spans` or bare `/`.
3. **The extra is installed.** If startup raises `Tracing is enabled but required packages are not installed`, re-install with the `[tracing]` extra (see prerequisites).
4. **Container networking.** From inside a container, `localhost` is the container itself. Point at the host with `http://host.docker.internal:6006/v1/traces` on Docker Desktop or the host's LAN IP on Linux Docker.

### Spans arrive but `service.name` is wrong

The exporter reads `service_name` once at startup and stamps every span with it. Edit the field, restart the server, and new spans pick up the new name — existing spans in Phoenix keep the old one.

### Phoenix returns 401 or 403

Hosted Phoenix needs an API key. Add it under `server.tracing.headers` with the header name your Phoenix tenant expects (usually `api_key` or `Authorization: Bearer ...`). Restart. If 401s persist, send one trace manually with `curl` against the same endpoint and headers to isolate Memex from the gateway.

## See also

- [Tutorial: Getting started with Memex](../../tutorials/getting-started.md)
- [How-to: Scrape Memex metrics with Prometheus](prometheus.md)
- [Reference: Observability metrics](../../reference/observability.md)
- [Explanation: Architecture overview](../../explanation/how-memex-works/architecture-overview.md)
