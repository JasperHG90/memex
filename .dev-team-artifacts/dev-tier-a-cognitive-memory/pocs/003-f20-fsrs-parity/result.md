# POC-F20 — FSRS reference parity result

> **2026-05-01 — ALGORITHM-LABEL CORRECTION (Path A).** The original POC
> heading and "FSRS-4.5" framing throughout this document are
> **mislabelled**. `py-fsrs==4.1.2` actually implements **FSRS-5** (19
> weights), not FSRS-4.5 (17 weights) — pip version ≠ algorithm version.
> See `paper-cross-check.md` (this directory) for the verification: 4
> formula divergences from the FSRS-4.5 paper, all because the lib
> shipped FSRS-5 since py-fsrs v3.0.0. **The bit-exact parity result is
> still valid** — it proves the port matches `py-fsrs==4.1.2` (= FSRS-5),
> which is the current production-grade open-source SRS algorithm
> (Anki, RemNote, ts-fsrs all ship FSRS-5 in 2025).
>
> Path A directive (team-lead-approved): drop the vendored port; depend
> on `py-fsrs>=4.0.0,<5.0.0` directly; `memory/revisit.py` is now a thin
> ~80-LOC wrapper. Shipped in #24 (PR `wave-2/F20-revisit-fsrs5`).
> Read `paper-cross-check.md` BEFORE relying on any "FSRS-4.5" wording
> below.

# POC-F20 — FSRS-4.5 reference parity result

| Field | Value |
|---|---|
| **Status** | **PASS** |
| **POC owner** | dev-ws-revisit |
| **Ticket** | #14 |
| **Backlog feature** | F20 (FSRS-based memory revisitation) |
| **Run date** | 2026-04-30 |
| **Worktree** | `.claude/worktrees/dev-ws-revisit` |
| **Branch** | `ws-revisit/poc-f20` |
| **Reference package** | `fsrs==4.1.2` (FSRS-4.5 algorithm; `DECAY=-0.5`, 19-weight tuple) |
| **Reference vectors hash** | `21c56ee3419b630b19d09d50b2f40b4d08ff5effe7f0a0351bb4b7ddcbc27b8c` |

## Summary

The vendored FSRS-4.5 port at `harness/schedule.py` reproduces `py-fsrs`
`Scheduler.review_card` outputs **with bit-exact stability and exact
integer-day intervals** across **150 parity step assertions** (37
sequences × 1-7 steps each). The port is what will land verbatim in
`packages/core/src/memex_core/memory/revisit.py` for #24 — POC has
locked the algorithm.

| Metric | Contract bound | Observed | Margin |
|---|---|---|---|
| `MAX_STABILITY_DRIFT` (abs diff) | ≤ 1e-4 | **0.0** | 4 orders below bound |
| `MAX_INTERVAL_DRIFT` (abs diff, days) | ≤ 1 day soft / 0 days exact | **0** | exact match |
| `NEXT_REVIEW_AT_MISMATCHES` (count) | 0 | **0** | exact ISO-second match |
| Parity step assertions | ≥ 80 | **150** | 1.9× target |
| Parametrized test cases | ≥ 50 | **75 passed / 0 failed** | 1.5× target |

## Test outcome

```
$ python -m pytest test_parity.py -q
75 passed in 0.24s
```

Breakdown:
- `test_reference_vectors_hash_unchanged` — 1 case
- `test_fsrs_first_review_parity` — 37 parametrizations (one per sequence's first step)
- `test_fsrs_subsequent_review_parity` — 33 parametrizations (sequences with ≥ 2 steps; iterates over all subsequent steps within the parametrization)
- `test_edge_clock_skew_now_before_last_does_not_crash` — 1 case
- `test_edge_max_interval_cap` — 1 case
- `test_edge_first_review_again_initial_stability_floor` — 1 case
- `test_edge_difficulty_bounds_clamp` — 1 case
- = **75 cases**, ~150 underlying step-level assertions

## Tolerance calibration

The contract tolerance was `≤ 1e-4` stability / `≤ 1 day` interval (from
RFC-014's calibrated bound; RFC-008's aspirational `1e-6` was relaxed by
QA on 2026-04-30 because py-fsrs reference outputs are not 1e-6-stable
across Python/numpy versions).

Observed drift is `0.0` on stability and `0` on intervals — this is
*tighter* than the contract, because:

1. The port mirrors the exact formula structure of py-fsrs's
   `Scheduler._next_recall_stability` / `_next_forget_stability` /
   `_next_difficulty` / `_initial_stability` / `_initial_difficulty`
   /`_next_interval`, only with renamed locals.
2. We call `schedule()` with the same FSRS-4.5 weights, the same
   `desired_retention=0.9`, the same `DECAY=-0.5`, the same `FACTOR =
   0.9**(1/-0.5) - 1`, and the same `maximum_interval=36500` that the
   regenerator hard-codes when constructing py-fsrs `Scheduler`.
3. py-fsrs's `_next_interval` rounds to int days via `round()`; the port
   matches the rounding, so intervals agree at integer-day resolution.
4. The IEEE-754 floating-point ops are deterministic when ordered the
   same — nothing in the port deviates from py-fsrs's expression order,
   so stabilities agree to bit-exact precision.

## Findings worth surfacing for #24

**1. Disable py-fsrs's `learning_steps` / `relearning_steps` short-circuit
to get FSRS-4.5 stability scheduling on first review.**

Initial regeneration with default `learning_steps=(1min, 10min)` produced
`Again`/`Hard`/`Good` first-review next-review-times of "1 minute later"
(Anki Learning UX flow), not the FSRS stability-formula day-scale
intervals. Only `Easy` first-reviews skipped Learning state. To get the
FSRS-4.5 algorithmic outputs spec'd in RFC-014 §"FSRS implementation",
both `learning_steps` and `relearning_steps` must be empty `()`. This is
documented in `regenerate_reference_vectors.py` and propagated to
`reference_vectors.json` `generation.note`.

For the `revisit.py` production port (#24), this means **F20 deliberately
omits the learning/relearning short-circuit** — every review goes through
the FSRS stability/difficulty update path. Memex units skip the Anki
Learning/Relearning UX states and write FSRS state directly via init
formulas on first review. This matches RFC-014 line 142-144.

**2. py-fsrs FSRS-4.5 trained weights ≠ RFC-014's quoted weights.**

RFC-014 line 113-118 quoted FSRS-4.5 weights (e.g. `0.4197, 1.1869,
3.0412, ...`) which differ from `py-fsrs==4.1.2`'s defaults (`0.40255,
1.18385, 3.173, ...`). The port uses py-fsrs's defaults verbatim because
they're the package's canonical FSRS-4.5 trained reference. The RFC-014
quotation appears to be a different training run. **The production port
in #24 should use py-fsrs's defaults, not the RFC's quoted values.** I
will flag this in the #24 PR description so the discrepancy is auditable.

**3. `enable_fuzzing=False` is mandatory for determinism.**

`Scheduler` defaults to `enable_fuzzing=True`, which adds a small random
jitter to intervals ≥ 2.5 days. The reference vectors set `False` for
deterministic parity. The production port (#24) hard-codes fuzzing off
because Memex's scheduler is deterministic per spec.

**4. Time-going-backwards (`now < last_review`).**

py-fsrs's `Scheduler.review_card` rejects backward time. The port
clamps `elapsed_days` at 0 (retrievability=1) so it does not crash on
clock skew. This is harness-only and is verified by a standalone test
(not by py-fsrs comparison) — flagged as a deliberate divergence with a
comment in `schedule.py`.

## Provenance

| Field | Value |
|---|---|
| `py_fsrs_version` | `4.1.2` |
| `numpy_version` | (recorded in `reference_vectors.json`) |
| `python_version` | `3.12.12` |
| `deterministic_now_iso` | `2026-01-01T12:00:00+00:00` |
| `gap_days_between_reviews` | `1.0` |
| `desired_retention` | `0.9` |
| `maximum_interval` | `36500` |
| `enable_fuzzing` | `false` |
| `learning_steps` | `()` (intentionally empty) |
| `relearning_steps` | `()` (intentionally empty) |

Re-run the regenerator and check the new hash to verify nothing has
drifted:

```bash
.venv-dev-ws-revisit/bin/python regenerate_reference_vectors.py
# expected hash: 21c56ee3419b630b19d09d50b2f40b4d08ff5effe7f0a0351bb4b7ddcbc27b8c
```

If the hash changes, py-fsrs has been bumped or numpy float-precision
has shifted; re-run the parity tests to confirm tolerance still holds
and update the recorded hash in this report.

## Decision

**POC PASS.** Algorithm port is locked. Promote `harness/schedule.py` to
`packages/core/src/memex_core/memory/revisit.py` for #24 (with renaming
of imports). Reference vectors stay in this POC dir as the regression
substrate; #24's `tests/unit/test_fsrs_parity.py` ports a ~20-case
subset to default CI and tags the full 75 with `@pytest.mark.slow`.

## After-merge cleanup

Per TS guidance, after #24 merges, drop `fsrs` from root
`pyproject.toml [dependency-groups.dev]` if `tests/unit/test_fsrs_parity.py`
embeds the reference values directly (no runtime call into `fsrs`). If
the production parity test still imports `fsrs`, leave the dev-dep in
place. The decision belongs in the #24 PR description.
