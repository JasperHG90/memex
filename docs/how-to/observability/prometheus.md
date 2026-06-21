# Scrape Memex metrics with Prometheus

Memex exposes a Prometheus text-exposition endpoint at `GET /api/v1/metrics`. Point your existing Prometheus deployment at it, and you get retrieval latency, reflection queue depth, LLM cost, circuit-breaker state, lint findings, and roughly a hundred other series — all prefixed `memex_*` — without installing an exporter sidecar.

This guide shows you how to wire the scrape, confirm it works, and fix the three things that usually go wrong.

## Prerequisites

- A Memex server reachable on the network from your Prometheus instance. If you have not started one yet, see [Tutorial: Getting started](../../tutorials/getting-started.md).
- A running Prometheus 2.x or 3.x with permission to edit its `prometheus.yml`.
- `curl` on the host you use to verify (any version).

## Procedure

### 1. Confirm the endpoint is reachable

From a host that can reach the Memex server, run:

```bash
curl -sS http://memex.internal:8000/api/v1/metrics | head -20
```

You should see plain text starting with `# HELP …` and `# TYPE …` lines, followed by series like `memex_ingestion_total`. If you get a 401 here, jump to [Troubleshooting](#troubleshooting) before continuing.

### 2. Add a scrape job

Edit `prometheus.yml` and add a job under `scrape_configs`:

```yaml
scrape_configs:
  - job_name: memex
    metrics_path: /api/v1/metrics
    scrape_interval: 30s
    scrape_timeout: 10s
    static_configs:
      - targets:
          - memex.internal:8000
        labels:
          service: memex
          env: prod
```

Replace `memex.internal:8000` with your server's host and port. The 30-second interval matches the granularity of the histograms; tighter intervals waste storage without giving you more signal.

### 3. Reload Prometheus

Send a SIGHUP, or POST to the reload endpoint if you have `--web.enable-lifecycle` on:

```bash
curl -X POST http://prometheus.internal:9090/-/reload
```

### 4. (Optional) Confirm the metrics path is auth-exempt

Memex ships with `/api/v1/metrics` in the default `auth.exempt_paths` list <code-ref path="packages/common/src/memex_common/config.py" lines="1406-1409" />, so unauthenticated scraping works out of the box. If you have customised `auth.exempt_paths` in your config and removed the metrics route, put it back:

```yaml
auth:
  enabled: true
  keys:
    - key: "${MEMEX_API_KEY}"
      policy: writer
  exempt_paths:
    - /api/v1/health
    - /api/v1/ready
    - /api/v1/metrics
```

If you cannot exempt the path for policy reasons, attach an API key to the scrape job with `authorization` (Prometheus 2.26+) instead.

## Verification

Open the Prometheus UI at `http://prometheus.internal:9090` and run these checks.

**1. The target is up.** Navigate to *Status → Targets*. The `memex` job should show one endpoint in state `UP` with a recent `Last Scrape`. If it shows `DOWN`, the error column names the cause (DNS, connection refused, 401).

**2. Custom metrics are present.** In the *Graph* tab, type `memex_` and the autocomplete should list dozens of series. Try:

```promql
memex_ingestion_total
```

You should see at least one series per `vault_id` × `status` combination that has handled traffic. New servers may show nothing until they ingest the first note.

**3. The instrumentator metrics are present.** The endpoint also exposes the default `prometheus_fastapi_instrumentator` series:

```promql
http_request_duration_seconds_bucket{handler="/api/v1/notes"}
```

These give you request-rate, latency, and per-route status-code counts for free.

## Troubleshooting

**401 Unauthorized on `/api/v1/metrics`.** Auth is enabled and someone removed the metrics path from `auth.exempt_paths`. Either add it back as shown above, or add `authorization` to the scrape job. The Memex server itself never requires a key for this path when it appears in `exempt_paths` <code-ref path="packages/common/src/memex_common/config.py" lines="1406-1409" />.

**No `memex_*` series, only `python_…` and `process_…`.** The endpoint is responding but `memex_core.metrics` was never imported in this process. This happens with custom embedders or thin server wrappers that bypass `memex_core.server`. Confirm by hitting the endpoint directly — if `memex_ingestion_total` is missing from the raw text, the module did not load. Restart the server with the standard entrypoint (`memex server start` or `uvicorn memex_core.server:app`); the metrics are registered at import time <code-ref path="packages/core/src/memex_core/metrics.py" lines="1-15" />.

**High label cardinality warnings in Prometheus logs.** Several Memex metrics carry a `vault_id` label. One vault per tenant is fine; one vault per user is not. If you are running thousands of vaults, drop the label at scrape time with a `metric_relabel_configs` block:

```yaml
    metric_relabel_configs:
      - source_labels: [__name__]
        regex: 'memex_.*'
        action: labeldrop
        regex: vault_id
```

You lose per-vault breakdowns; you keep Prometheus responsive. The trade-off is yours to make.

## See also

- [Tutorial: Getting started](../../tutorials/getting-started.md)
- [How-to: Export traces to Arize Phoenix](arize-phoenix.md)
- [Reference: Observability metrics](../../reference/observability.md)
- [Explanation: Design principles](../../explanation/design-principles.md)
