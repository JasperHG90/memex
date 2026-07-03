# Eval suite authoring — non-negotiable rules

The eval framework at `packages/eval/src/memex_eval/suite/` is a reusable component. Every retrieval / extraction / agent-behaviour regression check belongs inside it, not alongside it. Below: how to add work without drifting into parallel infrastructure.

## Before writing code

<constraint name="eval-framework-first-pass" priority="critical">
BEFORE writing any new module, file, or test for an eval-shaped task, you MUST inventory the framework's existing extension points and pick one. Specifically:

1. **Read `packages/eval/src/memex_eval/suite/base.py:256` (`register_outcome`)** — a registry decorator for new outcome types. Discriminator pattern: `type: Literal['<name>']` on a Pydantic subclass of `ExpectedOutcomeBase`. Existing outcomes: `KeywordsPresent`, `GoldUnitIds`, `RankingOrder`, `UsefulAtK`, `LLMJudge`, ~20 more.
2. **Read `packages/eval/src/memex_eval/suite/setup_actions.py:130` (`register_setup_action`)** — a registry decorator for pre-scenario side effects. Existing handlers: `record_outcome`, `deprioritize`, `kv_write`, `consolidation_tick`, etc.
3. **Read `packages/eval/src/memex_eval/suite/agents.py`** — `AgentAnswer` (the shape every outcome consumes), `DirectApiBackend` (the `api.search` / `api.search_notes` dispatcher), `get_backend(name)` registry.
4. **Read `packages/eval/src/memex_eval/suite/runner.py`** — scenario context (`_note_id_by_key`, `_note_key_to_unit_ids`, `_inline_note_ids`, etc.), score() invocation, MLflow logging.
5. **Read at least one existing suite end-to-end** (`packages/eval/src/memex_eval/suites/acme_corp/__init__.py`).
6. **Read `packages/eval/src/memex_eval/suite/loader.py`** — how `load_suite('name')` discovers and imports your suite package.

Skipping this step and inventing a parallel pipeline is the failure mode. Verified instances of the drift include: standalone library modules that reimplement `api.search`, e2e pytest tests that bypass the runner, suite-shaped behaviour added to `tests/` instead of `suites/<name>/`.
</constraint>

## Where new code lives

<constraint name="suite-package-layout" priority="critical">
A new eval-shaped check MUST be expressed as a suite package under `packages/eval/src/memex_eval/suites/<suite_name>/`. Layout:

```
suites/<suite_name>/
├── __init__.py          # SUITE = Suite(...); imports of _outcomes/_setup_actions FIRST (decorator side effects), THEN suite.register(...) calls
├── _outcomes.py         # @register_outcome('<name>') Pydantic subclass(es) — ONLY if a needed type doesn't exist
├── _setup_actions.py    # @register_setup_action('<name>') handler(s) — ONLY if a needed action doesn't exist
├── README.md            # what the suite gates against; scope statement; out-of-scope items
├── sources/             # markdown corpus, one .md per note; optional per-note sources/assets/<note-key>/*
└── (optional) baselines/, fixtures/, etc. — suite-private artifacts
```

**Suite-private extensions** (`_outcomes.py`, `_setup_actions.py`) live inside the suite package, not under `packages/eval/src/memex_eval/suite/*`. The framework's registry surface accepts decorator registrations from anywhere; the suite's `__init__.py` imports its private modules for side effects, the registries populate, and any `suite.register(expected=YourOutcome(...))` resolves correctly.

PROHIBITED:
- Adding a new outcome class to `packages/eval/src/memex_eval/suite/base.py` "because it's general-purpose" — start in the suite that needs it; promote to core only when a SECOND suite needs the same outcome.
- Adding a new setup-action handler to `packages/eval/src/memex_eval/suite/setup_actions.py` for the same reason.
- Creating a top-level `packages/eval/<feature>_harness.py` library module.
- Writing pytest tests under `tests/test_e2e_<feature>.py` that drive `MemexAPI` directly to do what `memex-eval suite run` would do.
</constraint>

## Don't reinvent

<constraint name="reuse-framework-primitives" priority="critical">
The framework already implements these primitives. NEVER reimplement them in a suite package:

| You want… | Use… |
|---|---|
| Drive `api.search` / `api.search_notes` for a query | `DirectApiBackend.answer(scenario, ...)` via `get_backend('api')` |
| Map note_key → unit IDs | `context['_note_key_to_unit_ids']` (auto-populated by the runner during ingest) |
| Map note_key → note ID | `context['_note_id_by_key']` |
| Get the ranked unit ID list from an answer | `_aggregate_unit_ids(answer)` from `base.py:400` |
| Score multiple metrics on one outcome | Return a `dict[str, float]` from `score()`; declare `metric_keys()` |
| Aggregate pass/fail across a suite | Framework reads the `pass` key from each scenario's `score()` return |
| Skip extraction across runs | Snapshot cache via `--from-snapshot auto` (snapshot_cache.py) |
| Sweep a config knob | `packages/eval/src/memex_eval/suite/sweep.py` |
| MLflow logging, reporter, group filtering, replicates | Built into the runner |

If you find yourself building any of these, stop and re-read the framework files.
</constraint>

## Direct DB writes from a setup action

<constraint name="direct-db-setup-action" priority="high">
If your suite legitimately needs to bypass ingest+extraction and write rows directly to the DB (e.g., paragraph-precision seeding without LLM extraction), the framework supports it via `@register_setup_action`. The handler accepts the metastore via construction (NOT the HTTP `api` client) and inserts SQLModel rows in its own session.

Guardrails:
- The handler MUST set `required: ClassVar[bool] = True` so a write failure errors the scenario rather than being silently logged.
- The handler MUST set `reusable_under_reuse_vault` correctly (`True` only if re-invocation produces identical state — e.g., deterministic UUIDv5 ids with `INSERT ... ON CONFLICT DO NOTHING`).
- The handler MUST return `{'note_key_to_unit_ids': {...}}` so downstream outcomes can resolve retrieved unit IDs back to note keys via the auto-prefixed context entry.
- The handler MUST have an integration test against testcontainer Postgres (NOT a mocked metastore) so schema drift surfaces in CI rather than at suite-run time.
- The handler SHOULD use deterministic UUIDv5 ids derived from content (e.g., `uuid5(NS, f'{note_key}\x00{paragraph_idx}\x00{text}')`) — `\x00` separator, never `|` (collision surface on `|` in any field).

This is a powerful primitive — extraction-bypassing seeding short-circuits a lot of guarantees the framework otherwise provides. Use it only when normal ingest + the snapshot cache cannot deliver the determinism the gate needs.
</constraint>

## Baseline-pinning checks (capture-and-verify gates)

<constraint name="baseline-anchor-stability" priority="high">
A regression gate that pins captured artifacts (rankings, scores, retrieved IDs) MUST anchor on values that survive re-ingest and cross-machine runs:

- **Anchor on note_keys (filename stems), NOT on `gen_random_uuid()` unit IDs.** Note keys are filename-derived and stable; unit IDs are randomly generated per ingest and float across machines.
- **Baseline files MUST carry a `meta` block** with at minimum `schema_version`, `top_k`, and `search_type`. Additional config knobs whose change should force recapture (e.g. clip knobs) MAY be added when the outcome has a path to read them at verify time — without that path the field is informational only and silently misleads operators about what the gate enforces. The outcome's `score()` verifies the meta against the live scenario state and raises `RuntimeError` (recorded as `status='error'`) on mismatch — silent comparison against a stale baseline is a regression in itself. The outcome MUST also refuse to score against a ranking-present-but-meta-missing baseline (write a defensive guard), since `None`-checks on individual meta keys would otherwise short-circuit and let a malformed file slip through.
- **`_load_baseline(scenario_id)` MUST return `[]` (or a sentinel) on missing-file**, not raise. Otherwise `memex-eval suite list` breaks on fresh clones. The outcome's `score()` distinguishes empty-baseline (capture pending, `status='error'`) from RBO/recall/etc. failure.
- **Import order in `__init__.py`**: `from ._outcomes import …` and `from ._setup_actions import …` MUST appear BEFORE any `suite.register(...)` call. The decorators must fire (registering classes with the framework) before scenarios reference them by class.

A capture-then-verify workflow that pins random unit IDs cross-machine WILL fail in CI for the wrong reason (cross-arch ONNX float drift flips near-tied ranks). Note-key anchoring trades intra-note rank sensitivity for cross-machine survivability — document the trade in the suite README.
</constraint>

## Capture CLI subcommands

<constraint name="capture-via-framework-primitives" priority="medium">
If your suite needs a one-shot "capture the baseline from the current implementation" workflow, expose it as a typer subcommand on the existing `memex-eval suite` group in `packages/eval/src/memex_eval/cli.py`. The subcommand body MUST reduce over framework primitives:

- Iterate `Suite.scenarios` from `load_suite(name)`.
- Get the backend per scenario: `get_backend(scenario.answer_mode or 'api')`.
- Call `backend.answer(scenario, api, vault_id, ...)` — the same call the runner makes.
- Persist the result to a path the OUTCOME owns (suite-local `baselines/<scenario_id>.json` or similar).

PROHIBITED:
- Reimplementing `api.search` / `api.search_notes` / embedder loading / MemexAPI construction in the subcommand.
- Bypassing `get_backend()` to construct a backend directly.
- Writing the capture as a `--capture` flag on `suite run` that mixes scoring and persistence — separate concerns into separate subcommands.
</constraint>

## When to ask vs. when to build

<constraint name="ask-before-parallel-infrastructure" priority="high">
If you find yourself about to build any of the following, STOP and confirm with the user first:

- A new pytest-driven e2e test under `tests/` that does what a suite would do.
- A library module under `packages/eval/` that imports from the framework but doesn't register a suite/outcome/handler.
- Custom corpus-loading, custom query-enumeration, or custom result-serialization logic — the framework has `SuiteSources.from_directory`, scenario-declared queries, and per-suite artifact paths.
- A modification to `packages/eval/src/memex_eval/suite/*` files for suite-specific behaviour.

The adversarial-review loop will not catch architectural drift — it checks internal consistency, not architectural fit. Cost of confirming the design before building: one round-trip. Cost of building it wrong and being rejected: rebuilding from scratch.
</constraint>
