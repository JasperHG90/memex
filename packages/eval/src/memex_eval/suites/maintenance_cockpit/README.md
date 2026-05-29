# maintenance_cockpit eval suite

Regression gates for the lint auto-learning loop (Layers 1-4).

## Scope

This suite exercises the lint lifecycle end-to-end:

1. **Cooldown suppression** (Layer 1) -- a resolved finding does not re-appear
   within the 30-day cooldown window.
2. **Evidence blob integrity** (Layer 1) -- the `evidence.resolution.followup`
   blob carries action, params, applied_state, prior_state, and the reviewer
   note after a cockpit-style resolve.
3. **Telemetry verdict rollup** (Layer 2) -- resolving findings with known
   verdicts (accept/dismiss) and refreshing telemetry produces the expected
   per-rule counts.
4. **Threshold calibration adjusts** (Layer 3) -- when telemetry shows a low
   accept_rate (<0.3, mostly dismisses), the calibration job raises the
   surprise_threshold above the default 0.7.
5. **Threshold calibration stable** (Layer 3) -- when telemetry shows an
   accept_rate in the dead zone (~0.5), calibration does not write a new row.
6. **Auto-apply confidence gate** (Layer 4) -- a pending finding with
   surprise_score below the confidence_threshold (0.95) is not auto-applied.
7. **Optimizer compile** (Layer 4) -- the DSPy optimizer produces a
   lint_llm_signature row with version >= 1 and a non-null validation_score.

## Out of scope

- **fresh_db_creates_all_tables** -- verified by integration tests in
  `packages/core/tests/integration/`, not by this suite. A fresh-DB scenario
  would need a clean Postgres instance per run, which the eval framework does
  not provide.
- **Retrieval quality** -- covered by `acme_corp` and `project_nexus` suites.
- **Agent integration** -- covered by `agent_integration` suite.

## Prerequisites

- Docker (for testcontainer Postgres)
- NLI polarity classifier enabled (`server.memory.lint_llm.polarity.enabled=true`)
- LLM API key for the lint LLM pass (ANTHROPIC_API_KEY or equivalent)

## Running

```bash
memex-eval suite run maintenance_cockpit
```
