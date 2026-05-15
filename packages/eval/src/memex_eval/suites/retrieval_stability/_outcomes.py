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


@register_outcome('ranking_baseline_rbo')
class RankingBaselineRbo(ExpectedOutcomeBase):
    """Rank-Biased Overlap of retrieved note_keys against a captured baseline.

    Two modes, gated by the ``MEMEX_EVAL_CAPTURE_BASELINES`` env var:

    * **Verify (default)**: read ``baseline_ranking``; compute RBO of
      ``retrieved_note_keys`` against it at ``p`` persistence; pass
      when RBO ≥ ``rbo_floor``.
    * **Capture (``MEMEX_EVAL_CAPTURE_BASELINES=1``)**: resolve the
      retrieved IDs back to note_keys, write ``baseline_path`` with a
      meta block + the ranking list, and return ``pass=1.0`` so the
      runner records a successful scenario. The verify run on a
      subsequent invocation reads that baseline and asserts RBO.

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
    schema_version: int = 1
    expected_top_k: int = 10
    expected_search_type: Literal['memory', 'note']
    p: float = 0.9
    rbo_floor: float = 0.996

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
        # capture modes. Running them before ``_retrieved_note_keys``
        # avoids resolving against the wrong search-type branch and
        # then discarding the result.
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

        # Resolve current retrieved note_keys for the (now-confirmed)
        # search_type. Truncate to the scenario's declared top_k. Both
        # capture and verify modes consume the result.
        retrieved_note_keys = _retrieved_note_keys(
            answer=answer,
            search_type=self.expected_search_type,
            note_key_to_unit_ids=note_key_to_unit_ids,
            context=context,
        )[: scenario.top_k]

        # Capture mode: skip verification and rewrite the baseline from
        # the current scenario state. Meta-mismatch self-heals.
        if _capture_mode_enabled():
            self._write_baseline(scenario, retrieved_note_keys)
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
        # Defensive guard: a baseline file MAY exist with a complete
        # ranking but no meta block (manually crafted or written by an
        # older schema). Refuse to score against an unguarded baseline
        # — otherwise the three checks below all skip via the
        # ``is not None`` short-circuit and a stale baseline slips
        # through silently.
        required_meta_keys = {'top_k', 'search_type', 'schema_version'}
        missing_meta_keys = (
            required_meta_keys - set(self.baseline_meta) if self.baseline_ranking else set()
        )
        if missing_meta_keys:
            raise RuntimeError(
                f'baseline for {scenario.id} has ranking but is missing '
                f'required meta keys {sorted(missing_meta_keys)}. '
                f'{recapture}'
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
        if not self.baseline_ranking:
            raise RuntimeError(
                f'no baseline captured for {scenario.id}. '
                f'Run `MEMEX_EVAL_CAPTURE_BASELINES=1 memex-eval suite '
                f'run retrieval_stability` to seed.'
            )

        rbo = rank_biased_overlap(
            self.baseline_ranking,
            retrieved_note_keys,
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
            },
            'ranking': ranking,
        }
        # Atomic write: tempfile in same directory + rename. Without this
        # a parallelized runner could interleave partial writes and
        # leave a truncated JSON on disk, which the next verify run
        # would fail to parse.
        tmp = path.with_suffix(path.suffix + '.tmp')
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n')
        tmp.replace(path)
        logger.info(
            'captured baseline for %s → %s (%d note_keys)',
            scenario.id,
            path,
            len(ranking),
        )


def _retrieved_note_keys(
    *,
    answer: AgentAnswer,
    search_type: Literal['memory', 'note'],
    note_key_to_unit_ids: dict[str, list[str]] | None,
    context: dict[str, Any] | None,
) -> list[str]:
    """Resolve the retrieved IDs back to note_keys, deduped by first occurrence.

    Memory search: ``answer.retrieved_unit_ids`` are MemoryUnit ids;
    invert ``note_key_to_unit_ids: {note_key: [unit_ids]}``.

    Note search: ``answer.retrieved_unit_ids`` are Note ids (the
    framework's ``DirectApiBackend`` populates that field with note
    IDs whenever the scenario's ``search_type='note'``); invert
    ``context['_note_id_by_key']: {note_key: note_id}``.

    Dedupe semantics: when several retrieved units resolve to the same
    note_key (common in memory search — two paragraphs of one note
    co-ranking), the note_key appears once at its **first** rank
    position. RBO's set-based agreement-at-depth-d would otherwise
    treat the repeated entries as low-agreement positions, which is
    not what the baseline pins.

    Unknown ids (not present in either inversion map) are dropped
    silently — happens when ingest produced units the suite's baseline
    didn't observe (e.g. inline-note seeding from a different scenario).
    """
    retrieved_ids = _aggregate_unit_ids(answer)

    if search_type == 'memory':
        # The runner injects ``note_key_to_unit_ids`` for every
        # memory-search scenario it ingested. ``None`` here means the
        # runner contract broke — fail loudly so a regression in the
        # runner shows as an explicit error rather than RBO=0.0
        # masquerading as a retrieval regression.
        if not note_key_to_unit_ids:
            raise RuntimeError(
                'memory-search scenario received no '
                '``note_key_to_unit_ids`` mapping; the runner is '
                'expected to inject this for every ingested suite. '
                'An empty/None mapping would otherwise score RBO=0 '
                'as a false regression.'
            )
        unit_to_note: dict[str, str] = {}
        for note_key, unit_ids in note_key_to_unit_ids.items():
            for uid in unit_ids:
                unit_to_note[uid] = note_key
        out: list[str] = []
        seen: set[str] = set()
        for uid in retrieved_ids:
            nk = unit_to_note.get(uid)
            if nk is not None and nk not in seen:
                out.append(nk)
                seen.add(nk)
        return out

    # search_type == 'note': the runner injects ``_note_id_by_key``
    # into scenario_context for every ingested suite. Missing context,
    # missing key, or empty mapping all signal a runner-contract
    # break — fail loudly so a runner regression doesn't quietly
    # score RBO=0 as a false retrieval regression.
    note_id_by_key: dict[str, str] = (context or {}).get('_note_id_by_key') or {}
    if not note_id_by_key:
        raise RuntimeError(
            'note-search scenario received no '
            "``context['_note_id_by_key']`` mapping; the runner is "
            'expected to inject this for every ingested suite. '
            'An empty/None mapping would otherwise score RBO=0 as '
            'a false regression.'
        )
    note_id_to_key: dict[str, str] = {nid: nk for nk, nid in note_id_by_key.items()}
    out = []
    seen = set()
    for nid in retrieved_ids:
        nk = note_id_to_key.get(nid)
        if nk is not None and nk not in seen:
            out.append(nk)
            seen.add(nk)
    return out
