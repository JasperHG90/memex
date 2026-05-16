"""Suite-private outcome: ranking-baseline RBO comparison.

Registers ``ranking_baseline_rbo`` via :func:`memex_eval.suite.base.register_outcome`.
Suite-local: lives inside the suite package, not under the framework's
``suite/`` core. Future suites that need the same outcome should
import-promote it; otherwise keep it here.

Scope of the gate:
  * Memory-search scenarios: the full memory-rerank pipeline inside
    ``retrieval.engine._rerank_units``, including the log-additive
    bounded-boost composition over per-unit metadata boosts.
  * Note-search scenarios: the note-rerank pipeline at
    ``retrieval.document_search.NoteSearchEngine._rerank_results``
    (CE → sigmoid → RRF → cosine over note-aggregated unit scores).
    Independent of the memory-rerank composition but a real
    regression surface.

Anchoring decision:
  * The baseline pins **note_keys** (filename stems), NOT unit IDs.
    Note keys are filename-stable; unit IDs are ``gen_random_uuid()``
    per ingest. Cross-machine cache-miss therefore breaks unit-id
    pinning silently. The trade-off: a rerank flip between two units
    from the same note is invisible to a note-key baseline. A strict
    unit-id mode is a planned follow-up that engages only when the
    runner can report a snapshot-cache hit (deferred — runner
    extension).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from memex_eval.ranking_stability import rank_biased_overlap
from memex_eval.suite.base import (
    AgentAnswer,
    ExpectedOutcomeBase,
    Scenario,
    _aggregate_unit_ids,
    register_outcome,
)

logger = logging.getLogger('memex_eval.suites.retrieval_stability._outcomes')

# Env-gate that flips the outcome into capture mode. When set to a
# truthy value, ``score()`` writes the captured ranking to
# ``baseline_path`` (with meta block) and returns ``pass=1.0`` —
# bypasses RBO comparison so the run "passes" while persisting the
# baseline. Default-off; verify is the safe behaviour.
_CAPTURE_ENV_VAR = 'MEMEX_EVAL_CAPTURE_BASELINES'


def _capture_mode_enabled() -> bool:
    val = os.environ.get(_CAPTURE_ENV_VAR, '').strip().lower()
    return val in {'1', 'true', 'yes', 'on'}


# Surface capture mode at module-load time so an operator who set the
# env var sees one explicit log line instead of inferring mode from
# the absence of failures. Kept at module scope (not per-score) to
# match the env-read frequency under normal use (env is set once
# before invocation); per-scenario re-reads remain in score() so
# tests using ``monkeypatch.setenv(...)`` still observe their flips.
if _capture_mode_enabled():
    logger.info(
        '%s is set; retrieval_stability outcomes are in CAPTURE mode — '
        'baselines will be (re)written on every score() call. '
        'Unset the env var to return to verify mode.',
        _CAPTURE_ENV_VAR,
    )


@register_outcome('ranking_baseline_rbo')
class RankingBaselineRbo(ExpectedOutcomeBase):
    """Rank-Biased Overlap of retrieved IDs against a captured baseline.

    Compares ``answer.retrieved_unit_ids`` (MemoryUnit UUIDs for
    memory-search, Note UUIDs for note-search) directly against a
    captured ID list. The suite ships a snapshot (``snapshot/`` dir
    in the suite package) that the runner imports verbatim instead of
    re-extracting; every run therefore sees the same UUIDs, and RBO
    measures actual ranking stability — not laundered note-key
    agreement.

    Two modes, gated by the ``MEMEX_EVAL_CAPTURE_BASELINES`` env var:

    * **Verify (default)**: read ``baseline_ranking``; compute RBO of
      retrieved IDs against it at ``p`` persistence; pass when
      RBO ≥ ``rbo_floor``.
    * **Capture (``MEMEX_EVAL_CAPTURE_BASELINES=1``)**: write the
      current retrieved-ID ranking to ``baseline_path`` with a meta
      block, return ``pass=1.0``. A subsequent verify run reads it
      back and asserts RBO.

    Empty ``baseline_ranking`` in verify mode → ``RuntimeError`` →
    ``status='error'`` with a clear recapture hint. Distinguishable
    from a 0.0 RBO (real regression).

    Meta-mismatch (``expected_top_k`` ≠ scenario.top_k or
    ``expected_search_type`` ≠ scenario.search_type) → ``RuntimeError``
    in verify mode → ``status='error'``. In capture mode, the meta is
    REWRITTEN from the current scenario+outcome state (so a future
    schema bump self-heals on recapture).
    """

    type: Literal['ranking_baseline_rbo']
    baseline_path: str
    baseline_ranking: list[str]
    baseline_meta: dict[str, Any] = Field(default_factory=dict)
    # schema_version 3: meta now carries ``config_pins`` for any
    # retrieval-pipeline knob whose change should force recapture.
    # A bump invalidates every prior-version baseline (verify raises
    # with a clear recapture hint), which is the right behaviour when
    # the meta contract changes.
    schema_version: int = 3
    expected_top_k: int = 10
    expected_search_type: Literal['memory', 'note']
    p: float = 0.9
    rbo_floor: float = 0.92
    # Knob values pinned into the baseline meta. The suite author
    # updates this dict when they intentionally change a knob; a stale
    # ``config_pins`` vs. the captured baseline raises at verify time
    # with a recapture hint.
    #
    # SCOPE OF THE PIN (operator-discipline contract):
    #   * Mismatch between this in-memory dict and the persisted meta
    #     block is detected ⇒ gate refuses to score until recapture.
    #   * A knob change in production server config WITHOUT a paired
    #     edit to this dict is NOT detected — the eval client does not
    #     introspect server-side state. The pin therefore catches
    #     author intent drift, not server-state drift. CI cadence +
    #     code-review of server-config edits are the out-of-band
    #     guards for the latter. Suite README documents the workflow.
    #   * Values are JSON-safe primitives. Non-JSON values (tuples,
    #     callables, ``math.inf``, sets) are accepted by the type
    #     declaration but break ``json.dumps`` on capture or compare
    #     unequal after JSON round-trip. Use stringified values for
    #     anything non-primitive (``'inf'`` for ``math.inf``, etc.).
    config_pins: dict[str, Any] = Field(default_factory=dict)

    def score(
        self,
        answer: AgentAnswer,
        scenario: Scenario,
        *,
        note_key_to_unit_ids: dict[str, list[str]] | None = None,
        judge: Any | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, float]:
        # Wiring guards run FIRST — they catch a code bug in
        # ``__init__.py`` (scenario and outcome constructed with
        # divergent parameters) and apply equally in verify and
        # capture modes.
        if scenario.top_k != self.expected_top_k:
            raise RuntimeError(
                f'scenario/outcome wiring error for {scenario.id}: '
                f'scenario.top_k={scenario.top_k} but outcome was '
                f'constructed with expected_top_k={self.expected_top_k}.'
            )
        if scenario.search_type != self.expected_search_type:
            raise RuntimeError(
                f'scenario/outcome wiring error for {scenario.id}: '
                f'scenario.search_type={scenario.search_type!r} but '
                f'outcome was constructed with '
                f'expected_search_type={self.expected_search_type!r}.'
            )

        # Both search modes populate retrieved_unit_ids with stable IDs:
        #   memory search → MemoryUnit UUIDs
        #   note search   → Note UUIDs (after agents.py:419 fix)
        # The suite-shipped snapshot pins these UUIDs across runs so the
        # comparison measures actual ranking stability, not laundered
        # note-key agreement.
        retrieved_ids = list(_aggregate_unit_ids(answer))[: scenario.top_k]

        # Capture mode: skip verification and rewrite the baseline from
        # the current scenario state. Meta-mismatch self-heals.
        if _capture_mode_enabled():
            self._write_baseline(scenario, retrieved_ids)
            return {'rbo': 1.0, 'pass': 1.0}

        # Surface a corrupt-baseline sentinel from _load_baseline as a
        # per-scenario error rather than letting an empty ranking
        # silently score 0.0. _load_baseline cannot raise — doing so
        # would crash `memex-eval suite list` for every suite — so the
        # detection lives here.
        if self.baseline_meta.get('_corrupt'):
            raise RuntimeError(
                f'baseline file is corrupt for {scenario.id}: '
                f'{self.baseline_meta.get("_error", "(unknown error)")}. '
                f'Recapture with `MEMEX_EVAL_CAPTURE_BASELINES=1 '
                f'memex-eval suite run retrieval_stability`.'
            )

        # Verify mode. The persisted meta block in the baseline JSON is
        # the source of truth for capture-time parameters; compare it
        # against the live scenario state to detect stale baselines.
        recapture = (
            'Recapture with `MEMEX_EVAL_CAPTURE_BASELINES=1 '
            'memex-eval suite run retrieval_stability`.'
        )
        # Empty baseline → "capture pending". Check this FIRST so a
        # stale meta block on an otherwise-empty file does not fire a
        # misleading "captured at top_k=5" error when the real problem
        # is that there is nothing on disk to compare against.
        if not self.baseline_ranking:
            raise RuntimeError(
                f'no baseline captured for {scenario.id}. '
                f'Run `MEMEX_EVAL_CAPTURE_BASELINES=1 memex-eval suite '
                f'run retrieval_stability` to seed.'
            )
        # Defensive guard: a baseline file MAY exist with a complete
        # ranking but no meta block (manually crafted or written by an
        # older schema). Refuse to score against an unguarded baseline
        # — otherwise the three checks below all skip via the
        # ``is not None`` short-circuit and a stale baseline slips
        # through silently.
        # ``config_pins`` is required at schema_version >= 3. A
        # missing key would otherwise short-circuit the ``is not None``
        # guard below and score against stale knobs silently. Listing
        # it here forces the suite author to either (a) include the
        # block (even as an explicit ``{}``) or (b) bump
        # ``schema_version`` so the prior verify path refuses.
        required_meta_keys = {'top_k', 'search_type', 'schema_version', 'config_pins'}
        missing_meta_keys = required_meta_keys - set(self.baseline_meta)
        if missing_meta_keys:
            raise RuntimeError(
                f'baseline for {scenario.id} has ranking but is missing '
                f'required meta keys {sorted(missing_meta_keys)}. '
                f'{recapture}'
            )
        # Reject ``null`` values on required keys: the value-comparison
        # guards below use ``is not None`` short-circuit, so a baseline
        # JSON with ``"top_k": null`` would skip the value check and
        # score against a meaningless baseline. Treat null-valued meta
        # the same as missing meta — refuse to score.
        null_meta_keys = {k for k in required_meta_keys if self.baseline_meta.get(k) is None}
        if null_meta_keys:
            raise RuntimeError(
                f'baseline for {scenario.id} has null values for required '
                f'meta keys {sorted(null_meta_keys)}. {recapture}'
            )
        captured_top_k = self.baseline_meta.get('top_k')
        if captured_top_k is not None and captured_top_k != scenario.top_k:
            raise RuntimeError(
                f'baseline meta mismatch for {scenario.id}: '
                f'scenario.top_k={scenario.top_k} but baseline JSON was '
                f'captured at top_k={captured_top_k}. {recapture}'
            )
        captured_search_type = self.baseline_meta.get('search_type')
        if captured_search_type is not None and captured_search_type != scenario.search_type:
            raise RuntimeError(
                f'baseline meta mismatch for {scenario.id}: '
                f'scenario.search_type={scenario.search_type!r} but '
                f'baseline JSON was captured for '
                f'{captured_search_type!r}. {recapture}'
            )
        captured_schema = self.baseline_meta.get('schema_version')
        if captured_schema is not None and captured_schema != self.schema_version:
            raise RuntimeError(
                f'baseline schema_version mismatch for {scenario.id}: '
                f'outcome expects {self.schema_version} but baseline JSON '
                f'is at version {captured_schema}. {recapture}'
            )
        # Config-knob pinning. The suite author updates ``config_pins``
        # in __init__.py when a retrieval knob changes; mismatch here
        # refuses to score until the operator runs the recapture
        # workflow. ``{}`` on either side is treated as "no pins", not
        # a wildcard, so schema_version bump is the lever to require
        # pins on every baseline going forward.
        captured_pins = self.baseline_meta.get('config_pins')
        if captured_pins is not None and captured_pins != self.config_pins:
            raise RuntimeError(
                f'baseline config_pins mismatch for {scenario.id}: '
                f'outcome expects {self.config_pins!r} but baseline JSON '
                f'was captured at {captured_pins!r}. {recapture}'
            )

        rbo = rank_biased_overlap(
            self.baseline_ranking,
            retrieved_ids,
            p=self.p,
        )
        return {'rbo': rbo, 'pass': 1.0 if rbo >= self.rbo_floor else 0.0}

    def metric_keys(self, top_k: int | None = None) -> list[str]:
        return ['rbo', 'pass']

    def _write_baseline(
        self,
        scenario: Scenario,
        ranking: list[str],
    ) -> None:
        path = Path(self.baseline_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            'meta': {
                'schema_version': self.schema_version,
                'top_k': scenario.top_k,
                'search_type': scenario.search_type,
                'config_pins': dict(self.config_pins),
            },
            'ranking': ranking,
        }
        # Atomic write: tempfile in same directory + rename. Without
        # this a parallelized runner could interleave partial writes
        # and leave a truncated JSON on disk, which the next verify
        # run would fail to parse. Clean up the tempfile under any
        # failure path so a disk-full / permission error doesn't
        # litter the baselines directory.
        tmp = path.with_suffix(path.suffix + '.tmp')
        try:
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n')
            tmp.replace(path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        logger.info(
            'captured baseline for %s → %s (%d ids)',
            scenario.id,
            path,
            len(ranking),
        )
