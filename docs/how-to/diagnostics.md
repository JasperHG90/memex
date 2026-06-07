# Inspect Memex with the diagnostics CLI

A vault has thousands of memory units and retrieval results have started to feel off. You want to see the shape of what's stored — how many units are active, which entities dominate, what the lint backlog looks like, whether the embedding space has clusters. `memex diagnostics` is the operator CLI for that inspection. Each subcommand emits JSON to stdout, so you can pipe it into `jq`, save it for a regression check, or paste it into a notebook.

## Prerequisites

- Memex server reachable at the URL in your config (`memex config show` to confirm).
- An active vault, or a vault name/UUID you pass with `--vault`.
- For the manifold subcommand: the `[diagnostics]` extra installed, which pulls in `umap-learn`. Without it the server returns 501. <code-ref path="packages/core/src/memex_core/server/diagnostics.py" lines="35-42" />
- `jq` on your `$PATH` if you want pretty-printed slices of the JSON output.

## Procedure

The four subcommands are independent. Start with `summary` for a one-page overview, then drill into whichever block looks interesting.

### 1. Check overall vault health

```bash
memex diagnostics summary --vault my-vault | jq
```

The summary is synchronous and skips the UMAP block, so it returns in one round trip. The payload includes <code-ref path="packages/core/src/memex_core/diagnostics/summary.py" lines="37-46" />:

- `unit_counts` — counts split by `active`, `deprioritized`, and `stale`.
- `avg_mw_score` — mean Memory Worth across active, non-deprioritized units (Bayesian smoothed, defaults to 0.5 on an empty vault).
- `manifold_status` — one of `ready`, `pending`, or `absent`. `ready` means a cached UMAP exists; `pending` means a compute is in flight from a previous `manifold` call; `absent` means no cache yet.
- `cluster_count` — null until the manifold cache is warm and a cluster pass has annotated it.
- `lint_pending_by_type` — pending lint findings grouped by `lint_type`.
- `top_5_retrieved_entities` — five entities with the highest co-occurrence volume in outcome records.

Use this single command in a watcher or a daily report — it is the cheapest call of the four.

### 2. Project the embedding space

```bash
memex diagnostics manifold --vault my-vault | jq '.points | length'
```

The first call against a cold vault returns HTTP 202 with a `task_id`; the compute runs in the background and writes the result to the file store. <code-ref path="packages/core/src/memex_core/services/diagnostics.py" lines="60-112" /> The next call returns 200 with the cached payload. The CLI prints both responses through `_http_status` so a one-line `jq '._http_status'` tells you which one you got.

To force a recompute after a large ingest:

```bash
memex diagnostics manifold --vault my-vault --force-refresh
```

The payload's `points` array is one entry per memory unit: `{unit_id, x, y}`. Feed it into Matplotlib, Vega, or a notebook to plot. Clusters of nearby points mean topically related units; outliers usually mean noisy or unique content. The cache key includes the unit count and the most recent unit's `updated_at`, so the cache invalidates automatically when the vault grows or any unit changes. <code-ref path="packages/core/src/memex_core/diagnostics/umap.py" lines="40-59" />

### 3. See which entities dominate retrieval

```bash
memex diagnostics retrieval --vault my-vault --limit 20 | jq '.entities[] | {name, volume, avg_mw_score}'
```

`retrieval` returns the top-N entities ranked by outcome volume — the sum of `success_co_count + failure_co_count` across every unit linked to the entity. <code-ref path="packages/core/src/memex_core/diagnostics/heatmap.py" lines="18-43" /> Each row also carries the Bayesian-smoothed average MW score for the entity's units. The default top-N is 50; the server caps it at 500.

Use this when retrieval feels skewed. An entity at the top of the list with a low `avg_mw_score` is a candidate for review — it shows up a lot but its units are doing badly. An entity you expect to dominate but don't see is either under-tagged or under-cited in outcome records.

### 4. Pivot the lint backlog

```bash
memex diagnostics findings --vault my-vault | jq '.pending_by_type'
```

The lint subcommand emits a JSON pivot over the `maintenance_proposals` table:

- `counts_by_type_status_source` — full cross-tab of `(lint_type, status, source)`.
- `pending_by_type` — the pending-only slice, grouped by `lint_type`.
- `top_5_pending` — the five most-recent pending findings, with `id`, `rule_name`, `suggested_action`, and `created_at`. <code-ref path="packages/core/src/memex_core/diagnostics/lint_dashboard.py" lines="25-122" />

This is the dashboard view. It is distinct from `memex lint status` (single global count) and `memex lint findings` (paginated row listing). For an interactive walk through pending findings — accept, dismiss, apply a winner — use `memex lint review` instead. The diagnostics endpoint is designed for dashboards, alerts, and JSON-shaped automation; the review command is designed for a human at the keyboard.

To watch the backlog over time, capture the pivot daily:

```bash
memex diagnostics findings --vault my-vault \
  | jq '{at: now, pending: .pending_by_type}' \
  >> ~/memex-lint-history.ndjson
```

A growing per-type count means the scanner is producing findings faster than you are resolving them.

### Agent-driven check

If you only need the summary block and you are calling from an LLM agent, the MCP tool `memex_get_diagnostics_summary` returns the same payload as `memex diagnostics summary`. <code-ref path="packages/mcp/src/memex_mcp/server.py" lines="4191-4220" /> The manifold, retrieval, and lint dashboards are CLI-only.

### Scope the call to a different vault

All four subcommands accept `--vault` (or `-v`) with either a vault name or a UUID. Without the flag they target the active vault from your config. To inspect a vault other than the active one without changing config:

```bash
memex diagnostics summary --vault scratch
memex diagnostics summary --vault 6a8c…   # UUID also works
```

This makes the diagnostics CLI safe to run from a script that iterates over `memex vault list` without mutating anyone's session.

## Verification

You ran the right command and got the right shape back when:

- `summary` returns a JSON object with `unit_counts`, `avg_mw_score`, `manifold_status`, `lint_pending_by_type`, `cluster_count`, and `top_5_retrieved_entities` as keys.
- `manifold` returns either a payload with `points` (200) or an envelope with `task_id` and `_http_status: 202`.
- `retrieval` returns `{vault_id, as_of, top_n, entities: [...]}` with `entities` length up to `top_n`.
- `lint` returns `{vault_id, counts_by_type_status_source, pending_by_type, top_5_pending}`.

If any command exits non-zero, the CLI prints the API error to stderr — read that line first.

## Troubleshooting

**Manifold returns HTTP 501.** `umap-learn` is not installed on the server. Install the optional extra on the server host:

```bash
uv add 'memex-core[diagnostics]'
```

Then restart the server.

**Manifold has `points: []`.** No memory unit in this vault has a stored embedding. <code-ref path="packages/core/src/memex_core/diagnostics/umap.py" lines="121-131" /> Either the vault is empty, or every unit was extracted with the embedder disabled. Confirm with `memex diagnostics summary` — if `unit_counts.active` is non-zero and `points` is empty, the units exist but lack embeddings. Check that the embeddings model is configured on the server, ingest one new note, and re-call `manifold --force-refresh`.

**Manifold fails when the vault is small.** UMAP's default `n_neighbors` is 15. <code-ref path="packages/core/src/memex_core/diagnostics/umap.py" lines="27-33" /> A vault with fewer embedded units than that will raise inside `fit_transform`. Ingest more notes, or call the endpoint less often on a brand-new vault — `summary` works fine at any size.

**Manifold keeps returning 202.** The previous compute is still in flight. The diagnostics service keeps an in-process registry of pending tasks per vault and shares one compute across concurrent requests. <code-ref path="packages/core/src/memex_core/services/diagnostics.py" lines="85-112" /> If the server restarts mid-compute the registry is lost and the next call kicks off a fresh task. Wait, then re-call without `--force-refresh`.

**`retrieval` returns an empty `entities` list.** No unit in the vault has any entity links with a non-zero `success_co_count + failure_co_count`. Outcomes have not been recorded yet, or the vault has no entities at all. Run an outcome-producing workflow, or check `memex entity list --vault my-vault`.

**`lint` returns empty pivots.** No `maintenance_proposals` rows exist for this vault. Either the lint scanner has not run yet on this vault, or it ran and found nothing. Run `memex lint status --vault my-vault` to confirm, then check the lint how-to for how to trigger a scan.

## See also

- [How-to: Review and apply lint proposals](linting.md)
- [Reference: server config](../reference/server-config.md)
- [Explanation: how lint findings flow through the ledger](../explanation/maintenance-linter.md)
- [Tutorial: Walk through Memory Worth and deprioritization](../tutorial/memory-worth-and-deprioritization.md)
