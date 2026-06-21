# Walk through Memory Worth and deprioritization

Three notes go into a fresh vault. They all answer the same question — "how do I monitor a Python web service?" — but they were written years apart, and only one of them is still good advice. Memex has no way to know that yet. In this tutorial you teach it.

You will ingest the three notes, watch them rank identically at first, record one as helpful and one as wrong, and watch the ranking shift. Then you will deprioritize the wrong one so it disappears from default search, and restore it again to see that the act is reversible. At the end you will understand the two signals Memex uses to express "this memory is worth more" and "this memory is hidden by default", and you will know which command moves each one.

## Prerequisites

- A running Memex server with the HTTP API reachable at `http://localhost:8000` (set a different host with `MEMEX_URL` if you have one).
- The `memex` CLI installed and configured against that server. Run `memex stats stats` to confirm — it prints document, entity, and reflection-queue counts.
- An active vault. `memex vault list` will show one. If none exists, run `memex vault create demo`, then point this shell at it with `export MEMEX_VAULT__ACTIVE=demo`.
- `curl` and `jq` on your shell — used to record outcomes against the HTTP endpoint, since Memex has no CLI for recording outcomes today.
- About fifteen minutes.

## Steps

### 1. Ingest three notes about the same topic

Create a working directory and three short Markdown files. Each one offers different advice for the same job: monitoring a Python web service in production.

```bash
mkdir -p /tmp/mw-tutorial && cd /tmp/mw-tutorial
```

Write the first note. This one is current and accurate.

```bash
cat > monitor-modern.md <<'EOF'
# Modern Python service monitoring

For a Python web service in production today, instrument with OpenTelemetry
SDK auto-instrumentation, ship traces and metrics to an OTLP collector, and
use Prometheus or a managed backend like Honeycomb for storage and queries.
The OpenTelemetry Python SDK reached stable 1.0 and supports FastAPI,
Flask, and Django out of the box.
EOF
```

Write the second note. This one is out-of-date — `statsd` is rarely used for new services in 2026.

```bash
cat > monitor-statsd.md <<'EOF'
# Service monitoring with statsd

Run a statsd daemon next to your Python service, push counters and timers
to it from application code, and aggregate in Graphite for dashboards. The
statsd Python client is a thin wrapper over UDP and adds no runtime cost.
EOF
```

Write the third note. This one is partially right but vague — useful background only.

```bash
cat > monitor-general.md <<'EOF'
# Web service monitoring fundamentals

Every production Python web service needs three telemetry families:
metrics for aggregate behaviour, traces for per-request latency, and logs
for forensics. Pick a backend that stores all three and lets you correlate
between them.
EOF
```

Ingest all three into your active vault.

```bash
memex note add --file monitor-modern.md --key monitor-modern
memex note add --file monitor-statsd.md --key monitor-statsd
memex note add --file monitor-general.md --key monitor-general
```

Each command prints a confirmation with a note UUID and the number of memory units extracted. Wait until all three return before the next step — extraction runs synchronously here, but if you used `--background` you would need to wait for the worker.

You should now see three notes in `memex note list`.

### 2. Run a search and read the cold-start counters

Search for the topic you ingested. Use `--json` so you can read every field on each returned memory unit.

```bash
memex memory search "how should I monitor a Python web service" --json --limit 10
```

The output is a JSON array of memory-unit objects. Each object has the unit's text, its source note, and — relevant to this tutorial — two integer counters and a deprioritization flag:

```json
{
  "id": "0193ab...-...",
  "text": "Instrument with OpenTelemetry SDK auto-instrumentation...",
  "success_co_count": 0,
  "failure_co_count": 0,
  "is_deprioritized": false,
  ...
}
```

Every unit has `success_co_count = 0` and `failure_co_count = 0`. You have not told Memex how useful any of them are yet. <code-ref path="packages/common/src/memex_common/schemas.py" lines="615-628" />

Memory Worth is computed from these two counters. The formula is the Beta-Bernoulli posterior mean with a uniform prior:

```
mw_score = (success_co_count + 1) / (success_co_count + failure_co_count + 2)
```

For `(0, 0)` the score is `1 / 2 = 0.5`. The retrieval composition then applies a multiplicative boost `mw_boost = 1.0 + alpha * (mw_score - 0.5)`, which at `0.5` is exactly `1.0` — neutral, no rank change. <code-ref path="packages/core/src/memex_core/services/outcomes.py" lines="86-114" />

That is the *cold-start* behaviour. Three units about the same topic land in the result list with effectively the same MW contribution. Their order at this point is decided by the other ranking signals — embedding similarity, keyword match, recency. Memory Worth is silent.

Save the unit IDs from the JSON output for the next step. Pick one ID that belongs to the modern OpenTelemetry note and one ID that belongs to the statsd note. The `metadata.note_name` field on each result tells you which note a unit came from.

Pipe through `jq` to extract the IDs directly:

```bash
SEARCH=$(memex memory search "how should I monitor a Python web service" --json --limit 10)

GOOD_UNIT=$(echo "$SEARCH" | jq -r \
  '.[] | select(.metadata.note_name == "monitor-modern") | .id' | head -1)

BAD_UNIT=$(echo "$SEARCH" | jq -r \
  '.[] | select(.metadata.note_name == "monitor-statsd") | .id' | head -1)

echo "Good: $GOOD_UNIT"
echo "Bad:  $BAD_UNIT"
```

Both variables should print a UUID. If either is empty, the search did not return a unit from that note — verify the note was ingested and re-run the search.

### 3. Record one fact as helpful and one as not helpful

You confirmed in your last search session that the OpenTelemetry advice solved your problem, and the statsd advice would have been wrong for a new project today. Tell Memex.

There is no CLI command for recording outcomes — the capability lives on the HTTP endpoint and on the MCP tool only. Call the endpoint directly with `curl`. <code-ref path="packages/core/src/memex_core/server/outcomes.py" lines="110-156" />

```bash
curl -sX POST http://localhost:8000/api/v1/outcomes/record \
  -H 'Content-Type: application/json' \
  -d "{
    \"units\": [
      {\"unit_id\": \"$GOOD_UNIT\", \"verb\": \"helpful\",
       \"reason\": \"OpenTelemetry was the right answer for my service\"},
      {\"unit_id\": \"$BAD_UNIT\", \"verb\": \"not_helpful\",
       \"reason\": \"statsd is dated for new projects in 2026\"}
    ]
  }" | jq .
```

The response reports the counts of rows that were updated:

```json
{
  "units_updated": 2,
  "entities_updated": 2,
  "models_updated": 0,
  "audit_log_id": "...",
  "verb_counts": {"helpful": 1, "not_helpful": 1},
  "coverage_ratio": null
}
```

A few things to notice in the request and the response.

- The `units` field is a list, one entry per unit you classified. Each entry needs a `unit_id`, a `verb`, and a `reason`. A bare `success=true` body is the legacy shape and will print a `FutureWarning`; the per-unit shape is the only one that survives. <code-ref path="packages/core/src/memex_core/services/outcomes.py" lines="156-162" />
- `reason` is required for `helpful` and `not_helpful`. A missing or empty reason raises a 400 from the endpoint before any database write happens — the service rejects the unit (<code-ref path="packages/core/src/memex_core/services/outcomes.py" lines="65-67" />) and the route maps that to a 400 (<code-ref path="packages/core/src/memex_core/server/outcomes.py" lines="153-156" />). The reason lives in the audit log, not on the unit — it gives a future operator something to read when they audit why a counter moved.
- `entities_updated: 2` is the linked-entity propagation. Each unit you classified mentions entities like `OpenTelemetry` or `statsd`, and Memex bumped per-entity success and failure counters on those entities too. <code-ref path="DESIGN_DOCUMENT.md" lines="495-501" />
- The verbs cover three cases: `helpful` bumps `success_co_count`, `not_helpful` bumps `failure_co_count`, and `not_used` bumps a third counter (`unused_co_count`) which does NOT enter the Memory Worth formula. Use `not_used` for units that surfaced in your search but were not actually relevant to your answer. <code-ref path="packages/core/src/memex_core/services/outcomes.py" lines="38-54" />

If you would rather drive this from inside an agent, the MCP tool `memex_record_outcome` takes the same `units` argument with the same shape.

### 4. Re-run the search and watch the ranking shift

Run the same query again with `--json`.

```bash
memex memory search "how should I monitor a Python web service" --json --limit 10
```

Find the same two units in the new output. They now look different:

```json
{
  "id": "<GOOD_UNIT>",
  "success_co_count": 1,
  "failure_co_count": 0,
  "is_deprioritized": false,
  ...
}
{
  "id": "<BAD_UNIT>",
  "success_co_count": 0,
  "failure_co_count": 1,
  "is_deprioritized": false,
  ...
}
```

Pull the counters directly with `jq` to read them side-by-side:

```bash
memex memory search "how should I monitor a Python web service" --json --limit 10 \
  | jq -r '.[] | select(.id == "'"$GOOD_UNIT"'" or .id == "'"$BAD_UNIT"'") |
           "\(.metadata.note_name): success=\(.success_co_count) failure=\(.failure_co_count)"'
```

The two lines printed should look like:

```
monitor-modern: success=1 failure=0
monitor-statsd: success=0 failure=1
```

The Memory Worth posteriors have moved away from the cold-start neutral:

- The helpful unit: `mw_score = (1 + 1) / (1 + 0 + 2) = 2/3 ≈ 0.667`.
- The not-helpful unit: `mw_score = (0 + 1) / (0 + 1 + 2) = 1/3 ≈ 0.333`.

The retrieval composition turns each score into a multiplier:

- Helpful: `mw_boost = 1.0 + 0.3 * (0.667 - 0.5) ≈ 1.05`.
- Not-helpful: `mw_boost = 1.0 + 0.3 * (0.333 - 0.5) ≈ 0.95`.

`mw_boost` is one of five bounded factors that combine inside the reranker. <code-ref path="packages/core/src/memex_core/memory/retrieval/decay.py" lines="33-45" /> The other factors are recency, temporal proximity, evidence confidence, and FSFM decay; each is bounded to roughly `[0.5, 1.5]` so no one signal can dominate the rest, and they compose as a log-additive sum that gets clipped before being applied to the cross-encoder score. The helpful unit's score is being scaled by about five percent up; the unhelpful one by about five percent down.

In the search output you may notice the helpful unit has moved up the result list and the unhelpful one has moved down. The shift after a single outcome is small on purpose — one signal does not pin a unit at the top — but it is real, and it compounds. Record ten outcomes against the same unit and the boost saturates near its bound. Memory Worth lives between `[1 - alpha * 0.5, 1 + alpha * 0.5]`, which at the default `alpha = 0.3` is `[0.85, 1.15]`.

The unhelpful unit is still in the result list. Memory Worth alone does not hide anything — it nudges. Hiding is what step 6 is for.

### 5. Deprioritize the not-helpful unit

You want the statsd advice off your default surface entirely. Memory Worth has nudged its rank down, but a determined searcher would still find it. Use `memex memory deprioritize` to flip a separate binary flag — `is_deprioritized` — that hides the unit from default-scope retrieval. <code-ref path="packages/cli/src/memex_cli/memory.py" lines="163-194" />

```bash
memex memory deprioritize "$BAD_UNIT" --reason "statsd outdated for 2026 services"
```

The CLI prints a confirmation:

```
Memory unit <BAD_UNIT> deprioritized.  reason=statsd outdated for 2026 services
```

`--reason` is optional and defaults to `"manual"`, but supply one — the audit log keeps the reason for the cooldown logic and for future operators reading the row.

Deprioritization and Memory Worth live on different axes, on purpose:

- Memory Worth is a continuous score that nudges ranking up or down. It accumulates from many outcomes and is *append-only* — you cannot un-record an outcome, but new outcomes shift the posterior.
- Deprioritization is a single boolean column on the unit. Setting it true hides the unit from default search; setting it false brings it back. It is reversible.

The retrieval engine applies the deprioritization filter inside `apply_generic_filters`, before the reranker even sees the unit, so deprioritized units never enter the result set under default scope. <code-ref path="packages/core/src/memex_core/memory/retrieval/strategies.py" lines="127-130" />

### 6. Confirm the deprioritized unit no longer surfaces

Re-run the search.

```bash
memex memory search "how should I monitor a Python web service" --json --limit 10
```

Two of the three units appear — the OpenTelemetry one and the general-fundamentals one. The statsd unit is gone.

To prove it is hidden rather than deleted, ask explicitly for the hidden ones too. The `--include-stale` flag on the CLI controls a related filter (units marked `status='stale'`), but to surface deprioritized units you need the `include_deprioritized=true` shape — which today is on the MemexAPI and HTTP surface, not on the `search` CLI subcommand. The simplest proof-of-life is to query the unit by ID directly:

```bash
memex memory view "$BAD_UNIT"
```

The output shows the unit's text, status, and metadata — proving the row is still in storage. Deprioritization changed visibility, not existence:

```
Memory Unit <BAD_UNIT>
Type: world
Status: active

[Content]
Run a statsd daemon next to your Python service, push counters
and timers to it from application code, and aggregate in Graphite
for dashboards...
```

The `Status: active` line is important. Deprioritized units stay `active` — they are not the same as units whose source note was superseded (which would show `status='stale'`). The two states overlap, and a single unit can be both, but they come from different operations and serve different roles in the curate workflow. <code-ref path="DESIGN_DOCUMENT.md" lines="394-397" />

### 7. Restore the deprioritized unit

Suppose you reconsidered, decided the statsd advice is fine for legacy systems after all, and want it back on your default surface. `memex memory restore` reverses step 5. <code-ref path="packages/cli/src/memex_cli/memory.py" lines="196-216" />

```bash
memex memory restore "$BAD_UNIT"
```

The CLI prints:

```
Memory unit <BAD_UNIT> restored.
```

Re-run the search and you will see the statsd unit back in the result list. Its `is_deprioritized` flag is `false` again in the JSON output. The Memory Worth counters did not change — `success_co_count` is still `0`, `failure_co_count` is still `1`. Restoring undid the visibility flag; it did not undo your outcome judgment. Those are different signals on different axes, and they each retain their own history.

The restore writes an audit row of its own. The FSFM auto-band lint pass reads that audit row and refuses to auto-deprioritize the same unit again within a recent-actions window — so a single restore cannot be silently overridden by the background curator. <code-ref path="packages/core/src/memex_core/services/lint.py" lines="325-330" />

## What you built

You taught Memex which of three competing memories was correct, which was wrong, and which to push out of sight — and you watched each command land on a different axis. Memory Worth is the long-running ranking signal that accumulates from every outcome you record; deprioritization is the binary on-or-off surface flag that hides one unit from default search and is reversible by a single restore call. The two are designed to be used together: record outcomes liberally so the ranking learns, and reach for deprioritize only when a unit needs to be off the default surface entirely.

The same three notes are still in your vault. The OpenTelemetry advice now ranks slightly higher than its neighbours; the statsd advice is visible by default again, with one failure on its record. You can repeat step 3 to keep training the ranking, or rerun step 5 to hide a unit at any time.

## Next steps

- [How-to: configure Memory Worth EMA half-life](../how-to/configure-mw-ema.md) — change the 60-day default decay window for outcome evidence.
- [Reference: memex memory commands](../reference/cli-memory.md) — every flag on `deprioritize`, `restore`, `search`, and `view`.
- [Explanation: the FSFM composite score](../explanation/fsfm-composite.md) — how the background curator decides which units to deprioritize on its own.
- [Tutorial: get started with Memex](getting-started.md) — start here if you have not ingested your first note yet.
