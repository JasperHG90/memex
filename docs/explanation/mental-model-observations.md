# About mental-model observations

You wrote an agent that asks Memex what it knows about Alice. The first result back is a clean, well-phrased fact: *"Alice ships to staging on Fridays, against guidance, when a customer is waiting."* You decide it is wrong, or out of date, and ask Memex to deprioritize it. The server returns HTTP 400 with a body you did not expect:

```json
{
  "error": "observations are read-only",
  "source_memory_units": ["8b1f…", "c204…", "f3e9…"]
}
```

This page explains why. The short version: that fact is not a memory unit. It is a *mental-model observation* — a synthesis Memex built from several real memory units — and the only way to suppress it is to deprioritize the units it was built from. The 400 is Memex telling you which ones.

## Context

Most of what Memex returns from search is a `MemoryUnit`: an atomic, addressable claim extracted from a note. Each unit is a row in Postgres, with its own outcome counters, its own deprioritize flag, and its own audit trail. You can mark it helpful, mark it not helpful, deprioritize it, restore it.

Some of what Memex returns is not a memory unit at all. It is an *observation* attached to a `MentalModel` — Memex's synthesized understanding of one entity, built every time the reflection loop runs. Observations live as JSONB inside the `mental_models` table. They have no row of their own. They are constructed by reading the underlying memory units, asking an LLM to phrase the pattern, and persisting the result inside the parent mental model.

When retrieval returns an observation, it dresses it up as a `MemoryUnit` so your agent code does not need a second shape to handle. But it also flags it: `unit_metadata.virtual = True`. That flag is the contract. The unit you got back is a projection, not a row. If you treat it as a row and try to mutate it, the server will tell you so.

This matters because the synthesized layer is where most of Memex's higher-order value comes from. The raw extraction substrate is dense and accurate but flat. A single fact — *"Alice deployed at 4pm on a Friday"* — is true, low-level, and not very useful on its own. Five facts spread across three weeks become useful only when something stands back and reads them together. That reading is what the reflection loop does, and observations are what it writes. Suppressing the reading by hand, without touching the underlying facts, is the wrong shape: the next reflection cycle would just re-synthesize it.

## Model

The shape, end to end:

```
                       MentalModel  (one per entity, per vault)
                       │
                       ├── observations: JSONB
                       │   │
                       │   ├── Observation
                       │   │   ├── id            (uuid4, stable)
                       │   │   ├── title         ("ships on Fridays")
                       │   │   ├── content       (one-paragraph synthesis)
                       │   │   ├── trend         (NEW | STABLE | STRENGTHENING | …)
                       │   │   └── evidence: [
                       │   │       { memory_id: <MU-A>, … },
                       │   │       { memory_id: <MU-B>, … },
                       │   │       { memory_id: <MU-C>, … },   ← these are the real units
                       │   │     ]
                       │   │
                       │   └── Observation (… more …)
                       │
                       ├── embedding             (centroid of observation embeddings)
                       ├── version               (bumped each refresh)
                       └── trend                 (model-level trend across all observations)


  Retrieval time:

      MentalModel.observations  ──►  _convert_mm_to_units  ──►  virtual MemoryUnit
                                                                 ├── id = Observation.id (stable)
                                                                 ├── text = "[Alice] ships on Fridays: …"
                                                                 ├── fact_type = OBSERVATION
                                                                 └── unit_metadata = {
                                                                       'observation': True,
                                                                       'virtual': True,
                                                                       'trend': 'STRENGTHENING',
                                                                       'mental_model_id': '…',
                                                                       'evidence_ids': ['<MU-A>', '<MU-B>', '<MU-C>'],
                                                                     }
```

Three things to notice.

First, the observation has a stable `id` of its own — a uuid4 written when reflection persisted it <code-ref path="packages/core/src/memex_core/memory/sql_models.py" lines="109-121" />. That id is reused as the virtual unit's id, so a retrieval result is consistent across calls. It is not, however, a `MemoryUnit.id` — no row in the `memory_units` table carries that uuid.

Second, the observation carries `evidence` — pointers back to the memory units that justified the synthesis. Those memory units *are* real rows. They are the ones you can act on.

Third, the surfacing is one-way. Observations are built from memory units, never the reverse. Reflection reads units to write observations; nothing in the system writes a unit because an observation says so.

The write side, from above:

```
   New note ingested
        │
        ▼
   Extraction emits memory units  ────────────────► memory_units table
        │                                                 │
        │ (entity mentions)                               │
        ▼                                                 │
   ReflectionQueue priority bumps                         │
        │                                                 │
        ▼                                                 │
   Leader-elected worker picks an entity                  │
        │                                                 │
        ▼                                                 │
   7-phase reflection loop                                │
        │                                                 │
        │  Phase 0  prune dead evidence, refresh          │
        │  Phase 1  seed candidate observations           │
        │  Phase 2  hunt — pgvector pull more units  ◄────┘
        │  Phase 3  validate — LLM checks each cand.
        │  Phase 4  compare/merge, compute trend
        │  Phase 5  finalize: persist + bump version
        │  Phase 6  enrich — tag contributing units
        ▼
   MentalModel.observations rewritten (JSONB)
   MentalModel.version incremented
```

Phase 4 is where each observation's `evidence` list is finalized. It is the same list retrieval reads back when it builds the virtual unit, and the same list the deprioritize handler reads when it answers your 400. There is no separate observation store. The JSONB column is the truth.

## Mechanism

Walk through one cycle.

**Retrieval.** Your agent calls `memex_memory_search('Alice deploy habits')`. Memex runs the usual fusion strategies and finds three candidate memory units about Alice's Friday deploys. Alongside them, the engine pulls the `MentalModel` row for Alice and runs `_convert_mm_to_units` on it: for each observation in the JSONB column, it recomputes the trend from the evidence timestamps and emits a virtual `MemoryUnit` <code-ref path="packages/core/src/memex_core/memory/retrieval/engine.py" lines="1903-1962" />. The virtual unit gets `unit_metadata.virtual = True` and an `evidence_ids` list pointing back at the real units.

Trend recomputation is cheap and worth doing every time. The `trend` value persisted on the parent `MentalModel` was correct *when reflection last ran*, but evidence ages and timestamps shift relative to today. The retrieval-time call to `compute_trend` reads the same evidence list against the current clock, so a strengthening pattern that has gone quiet for two months surfaces as `WEAKENING` even if the persisted model still says `STRENGTHENING` <code-ref path="packages/core/src/memex_core/memory/reflect/trends.py" lines="11-89" />. Thresholds are fixed: density ratio above 1.5 is `STRENGTHENING`, below 0.5 is `WEAKENING`, in between is `STABLE`, no recent evidence at all is `STALE`.

The final result set mixes both. The observation often ranks highly — its synthesized text is dense and on-topic. Your agent code should not need to treat it differently; that is the point.

**Deprioritize attempt.** Your agent decides the observation is misleading and calls `memex_memory_deprioritize(unit_id=<observation-id>)`. The HTTP route delegates to `UnitsService.set_unit_deprioritized` <code-ref path="packages/core/src/memex_core/server/memories.py" lines="172-211" />. The service tries `session.get(MemoryUnit, unit_id)` — and gets `None`, because there is no row with that uuid in `memory_units`.

Before raising `MemoryUnitNotFoundError`, the service runs a pre-resolution check: it queries `mental_models.observations` for any JSONB row whose `observations` list contains an object with that id <code-ref path="packages/core/src/memex_core/services/units.py" lines="219-238" />. The query uses Postgres' `@>` containment operator against a GIN index on the JSONB column, so it stays fast even when the vault has thousands of mental models <code-ref path="packages/core/src/memex_core/services/units.py" lines="291-357" />.

If a match comes back, the service collects the `memory_id` values from the matched observation's `evidence` list and raises `ObservationReadOnlyError(source_mus)`. The route handler catches that exception specifically — *before* the generic `MemexError` catch, otherwise the structured detail would be flattened to a string — and returns HTTP 400 with a body shaped by `ObservationReadOnlyError.to_http_detail()` <code-ref path="packages/common/src/memex_common/exceptions.py" lines="103-130" />.

**Recovery.** Your agent reads the `source_memory_units` array and re-issues `memex_memory_deprioritize` against one of those IDs — usually whichever one most clearly carries the claim it wants suppressed. That call hits the normal path: the unit row exists, the flag flips, the audit log records the change.

The observation does not vanish. It refreshes asynchronously. The deprioritize service, on a successful flip of a real unit, scans `mental_models.observations` in the same database session and enqueues one `refresh_observation` task per matched observation. The leader-elected reflection worker drains that queue and revisits Phase 0 (Liveness & Update) for each affected model. Phase 0 prunes dead evidence — superseded units, deprioritized units that no longer count — and folds in any new evidence found since the last cycle. The result is one of three outcomes: the observation survives (the deprioritized unit was one of several supporters), it weakens (the `trend` shifts as the density ratio falls), or it disappears entirely (no surviving evidence). Your one deprioritize call cannot force any of those outcomes — that is reflection's job.

**Stale evidence in the audit trail.** An observation's `evidence` list can include memory units that have since been superseded by a newer, contradicting note. Reflection does not auto-prune stale evidence; it keeps the citation as historical support. The `evidence_ids` you see in `unit_metadata` on the virtual unit may therefore contain ids you would not want re-promoted. Treat the list as audit trail — *these are the units that contributed to this observation as of the last reflection cycle* — not as an active claim about which units still hold.

A practical consequence: if you re-issue the deprioritize against a stale id, the call still succeeds. The unit row exists, the flag still flips, and an audit entry is still written. But you have done nothing useful — the stale unit was already invisible to default retrieval, and the observation's `trend` was already accounting for it as historical context. When you pick a `source_memory_units` id to act on, pick one whose `status` is `active`. The cheapest way to check: fetch the underlying unit before deprioritizing, look at its `status`, and pass through to the next id if it is `stale`.

**A full round-trip, written out.** Here is what the wire looks like end to end. The search call returns a result set that mixes real and virtual units:

```json
{
  "results": [
    {
      "id": "8b1f7c52-…",
      "text": "Alice deployed staging at 4:12pm on 2026-03-13 to unblock the Acme demo.",
      "fact_type": "observation",
      "status": "active",
      "unit_metadata": {}
    },
    {
      "id": "f0c91a04-…",
      "text": "[Alice] ships on Fridays: Alice repeatedly deploys to staging on Friday afternoons against the documented deploy window, typically when a customer is waiting on a fix.",
      "fact_type": "observation",
      "status": "active",
      "unit_metadata": {
        "observation": true,
        "virtual": true,
        "trend": "STRENGTHENING",
        "mental_model_id": "a7be…",
        "evidence_ids": ["8b1f7c52-…", "c204…", "f3e9…"]
      }
    }
  ]
}
```

Your agent picks the second result and tries to deprioritize it. The 400 comes back:

```json
{
  "detail": {
    "error": "observations are read-only",
    "source_memory_units": ["8b1f7c52-…", "c204…", "f3e9…"]
  }
}
```

Your agent reads the array, picks `8b1f7c52-…` (the unit most directly carrying the Friday-deploy claim), and re-issues the call against that id. The response is a normal `MemoryUnitDTO` with `is_deprioritized: true`. The audit log records *who, when, why*. A `refresh_observation` task lands on the reflection queue. Several minutes later, the next reflection cycle for Alice runs Phase 0, sees that `8b1f7c52-…` is now deprioritized, drops it from the observation's evidence, and re-finalizes the model. If the surviving evidence still supports the synthesis, the observation stays — perhaps with a `WEAKENING` trend. If not, it disappears. Either way, your agent did not have to know that path; it only had to handle the 400.

## Trade-offs

**Why not make observations first-class memory units?** Because the substrate is different. Memory units come from extraction — one note in, several units out, each grounded in a specific span of text. Their counters and lifecycle are managed against that source. Observations come from reflection — many units in, one synthesized line out, regenerated on each cycle. Giving observations their own row would mean either (a) garbage-collecting them when reflection runs again, which loses any outcome counters the agent stamped on them, or (b) keeping them around and having two parallel substrates of "atomic claims" that drift apart. The current design treats observations as views over units, the way a database view is a view over tables: queryable, indexable, surfaced as if they were rows, but never the canonical store.

**Why 400 with `source_memory_units` instead of a silent no-op?** A silent success would leave the agent believing it had suppressed the observation, when in fact nothing changed. A 404 would leave the agent believing the unit never existed, when in fact it does — just as a different kind of object, at a different address. The 400 is the only response that conveys both *your request did not land* and *here is the request that will*. Three things hang off this contract:

- The agent surface in `agent_surface.py` documents the 400 + `source_memory_units` shape so a model can recover without retraining on the surprise.
- The MCP tool description for `memex_memory_deprioritize` repeats the contract inline so the model sees it at tool-call time.
- A Prometheus counter (`DEPRIORITIZE_REJECTED_OBSERVATION_UUID_TOTAL`) bumps every time the path fires, so the rate of agent confusion is observable.

**Why pre-resolve against the JSONB column instead of a join table?** Because observations move. Reflection rewrites the JSONB list every cycle: ids stay stable for surviving observations, but new ones appear and old ones drop. Maintaining a separate `observation_id → memory_unit_id` join table would require keeping it in lockstep with the JSONB, with no winning consistency story. The GIN-indexed `@>` query is cheap enough, and it has one less moving part to keep honest.

**Why keep stale evidence in the list at all?** Because the audit trail is the only honest record of how the observation got written. Pruning stale evidence at write time would make the observation look better-supported than it actually is — five recent units stand differently when you know there are also three older units the system has since superseded. Reflection consumes the same audit trail when it revisits an observation: knowing which evidence has gone stale is what tells the next cycle whether the pattern is genuinely strengthening or is being kept alive by old data. Pruning the list would erase that signal.

## Implications

If you write agent code that calls `memex_memory_deprioritize`, the contract is:

1. **Check `unit_metadata.virtual` before mutating.** If it is truthy, the unit you have is an observation. Read `unit_metadata.evidence_ids` and pick the underlying MU to deprioritize.
2. **Be ready to recover from 400.** If you skip step 1 — or if you got the unit id from a path that did not preserve metadata — the deprioritize call will return 400 with `source_memory_units`. Re-issue against one of those ids.
3. **Do not retry the same observation id after a 400.** It will never succeed. The observation id is not a memory unit id, and never will be.

If you read the `trend` field on an observation, the value is recomputed at retrieval time from the timestamps in the `evidence` list — not the trend persisted on the parent `MentalModel`. The model-level trend is a coarser summary across all observations; the per-observation trend is what shows up in `unit_metadata.trend`. They can disagree. The per-observation value is the one to trust when you are reasoning about a single claim.

If you build evaluation suites that assert on retrieval shape, expect virtual units in the result. A suite that filters them out is making a deliberate choice — it is evaluating the raw extraction substrate, not the synthesized layer. A suite that keeps them in is evaluating what the agent will actually see. Both are valid; pick on purpose.

If you record outcomes against retrieval results, the same rule applies. `memex_record_outcome` against an observation id will land — the units pre-resolve check is specific to deprioritize — but the outcome counter increments on the parent `MentalModel`, not on the underlying memory units. That can be the right thing (you are crediting the synthesis itself) or the wrong thing (you meant to credit the unit that carried the claim). When in doubt, route outcomes to the underlying `evidence_ids` and let the next reflection cycle propagate the credit upward through the usual MW signal path.

## See also

- [Tutorial: Getting started](../tutorials/getting-started.md)
- [How-to: Deprioritize units](../how-to/deprioritize-units.md)
- [Reference: MCP tools](../reference/mcp-tools.md)
- [Explanation: Reflection and mental models](how-memex-works/synthesis-and-reflection.md)
