"""Unit tests for the `ranking_baseline_rbo` outcome.

Tests run against synthetic ``AgentAnswer`` + ``Scenario`` instances
— no DB, no model inference. The outcome's ``score()`` is the unit
under test; the framework's runner is not exercised here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Side-effect import: registers `ranking_baseline_rbo` in the outcome
# registry and `seed_paragraphs_from_sources` in the setup-action
# registry. Tests below construct the outcome class directly so the
# registration isn't strictly required, but importing the suite package
# is the contract that future-proofs reorganisations.
import memex_eval.suites.retrieval_stability  # noqa: F401
from memex_eval.suite import AgentAnswer, Scenario
from memex_eval.suite.base import KeywordsPresent
from memex_eval.suites.retrieval_stability._outcomes import RankingBaselineRbo


def _scenario(
    *,
    sid: str = 's1',
    query: str = 'q',
    top_k: int = 10,
    search_type: str = 'memory',
) -> Scenario:
    return Scenario(
        id=sid,
        description='d',
        query=query,
        # Placeholder expected for Scenario construction; tests override
        # at the call-site.
        expected=KeywordsPresent(type='keywords_present', keywords=['x']),
        top_k=top_k,
        search_type=search_type,  # type: ignore[arg-type]
    )


def _outcome(
    *,
    baseline: list[str],
    baseline_path: str = '/tmp/test-baseline.json',
    baseline_meta: dict[str, object] | None = None,
    expected_top_k: int = 10,
    expected_search_type: str = 'memory',
    p: float = 0.9,
    rbo_floor: float = 0.996,
) -> RankingBaselineRbo:
    # Default meta block mirrors what capture mode would write for the
    # given expected_* — so test_outcomes that don't care about meta
    # don't trip the partial-meta guard.
    if baseline_meta is None:
        baseline_meta = {
            'schema_version': 1,
            'top_k': expected_top_k,
            'search_type': expected_search_type,
        }
    return RankingBaselineRbo(
        type='ranking_baseline_rbo',
        baseline_path=baseline_path,
        baseline_ranking=baseline,
        baseline_meta=baseline_meta,
        expected_top_k=expected_top_k,
        expected_search_type=expected_search_type,  # type: ignore[arg-type]
        p=p,
        rbo_floor=rbo_floor,
    )


# ---------------------------------------------------------------------------
# Verify-mode happy paths
# ---------------------------------------------------------------------------


class TestVerifyMemorySearch:
    """search_type='memory': retrieved_unit_ids map to note_keys via
    runner-injected ``note_key_to_unit_ids``."""

    def test_identical_ranking_returns_one(self) -> None:
        outcome = _outcome(baseline=['n1', 'n2', 'n3'])
        ans = AgentAnswer(retrieved_unit_ids=['u1', 'u2', 'u3'])
        note_key_to_unit_ids = {'n1': ['u1'], 'n2': ['u2'], 'n3': ['u3']}
        result = outcome.score(ans, _scenario(), note_key_to_unit_ids=note_key_to_unit_ids)
        assert result == {'rbo': 1.0, 'pass': 1.0}

    def test_disjoint_returns_zero(self) -> None:
        outcome = _outcome(baseline=['n1', 'n2', 'n3'])
        ans = AgentAnswer(retrieved_unit_ids=['u4', 'u5', 'u6'])
        # The retrieved unit_ids resolve to note_keys n4/n5/n6 — disjoint
        # from the baseline.
        note_key_to_unit_ids = {'n4': ['u4'], 'n5': ['u5'], 'n6': ['u6']}
        result = outcome.score(ans, _scenario(), note_key_to_unit_ids=note_key_to_unit_ids)
        assert result['rbo'] == 0.0
        assert result['pass'] == 0.0

    def test_multiple_units_from_same_note_collapse(self) -> None:
        """Two units from note 'n1' both in top-3 collapse to one
        note_key entry. RBO's deduplicating-set semantics handle it."""
        outcome = _outcome(baseline=['n1', 'n2', 'n3'])
        ans = AgentAnswer(retrieved_unit_ids=['u1a', 'u1b', 'u2', 'u3'])
        note_key_to_unit_ids = {
            'n1': ['u1a', 'u1b'],
            'n2': ['u2'],
            'n3': ['u3'],
        }
        result = outcome.score(ans, _scenario(), note_key_to_unit_ids=note_key_to_unit_ids)
        # 'n1' appears twice in retrieved (positions 1 and 2); RBO
        # treats the prefix set as {n1, n2, n3} which matches baseline.
        assert result['rbo'] == 1.0
        assert result['pass'] == 1.0

    def test_unknown_unit_id_dropped(self) -> None:
        """A retrieved unit_id not in note_key_to_unit_ids is silently
        skipped — happens for inline-note seeding from a different
        scenario whose units the baseline never observed."""
        outcome = _outcome(baseline=['n1', 'n2'])
        ans = AgentAnswer(retrieved_unit_ids=['u_unknown', 'u1', 'u2'])
        note_key_to_unit_ids = {'n1': ['u1'], 'n2': ['u2']}
        result = outcome.score(ans, _scenario(), note_key_to_unit_ids=note_key_to_unit_ids)
        # After filtering, retrieved_note_keys = ['n1', 'n2'] which
        # matches baseline.
        assert result['rbo'] == 1.0


class TestVerifyNoteSearch:
    """search_type='note': retrieved_unit_ids are NOTE ids, mapped via
    ``context['_note_id_by_key']``."""

    def test_identical_ranking_returns_one(self) -> None:
        outcome = _outcome(baseline=['n1', 'n2', 'n3'], expected_search_type='note')
        ans = AgentAnswer(retrieved_unit_ids=['note-id-1', 'note-id-2', 'note-id-3'])
        context = {
            '_note_id_by_key': {
                'n1': 'note-id-1',
                'n2': 'note-id-2',
                'n3': 'note-id-3',
            }
        }
        result = outcome.score(
            ans,
            _scenario(search_type='note'),
            note_key_to_unit_ids=None,
            context=context,
        )
        assert result == {'rbo': 1.0, 'pass': 1.0}

    def test_missing_note_id_dropped(self) -> None:
        outcome = _outcome(baseline=['n1', 'n2'], expected_search_type='note')
        ans = AgentAnswer(retrieved_unit_ids=['note-unknown', 'note-id-1', 'note-id-2'])
        context = {'_note_id_by_key': {'n1': 'note-id-1', 'n2': 'note-id-2'}}
        result = outcome.score(ans, _scenario(search_type='note'), context=context)
        assert result['rbo'] == 1.0


class TestRboFloor:
    def test_below_floor_fails(self) -> None:
        # Single rank swap at d=1↔2 gives RBO=0.9 at p=0.9.
        outcome = _outcome(baseline=['n1', 'n2', 'n3'], rbo_floor=0.996)
        ans = AgentAnswer(retrieved_unit_ids=['u2', 'u1', 'u3'])
        note_key_to_unit_ids = {'n1': ['u1'], 'n2': ['u2'], 'n3': ['u3']}
        result = outcome.score(ans, _scenario(), note_key_to_unit_ids=note_key_to_unit_ids)
        assert 0.85 < result['rbo'] < 0.95
        assert result['pass'] == 0.0

    def test_at_floor_passes(self) -> None:
        outcome = _outcome(baseline=['n1', 'n2'], rbo_floor=0.99)
        ans = AgentAnswer(retrieved_unit_ids=['u1', 'u2'])
        note_key_to_unit_ids = {'n1': ['u1'], 'n2': ['u2']}
        result = outcome.score(ans, _scenario(), note_key_to_unit_ids=note_key_to_unit_ids)
        assert result['rbo'] == 1.0
        assert result['pass'] == 1.0


class TestMetaMismatchGuards:
    """Two layers of guard exist:

    1. The persisted JSON ``meta`` block: a baseline captured at
       ``top_k=5`` must not be silently compared at ``top_k=10`` —
       this catches stale baselines on disk.
    2. The outcome's ``expected_*`` fields vs the scenario fields:
       catches wiring errors in ``__init__.py`` where the scenario
       and outcome are constructed with divergent parameters.
    """

    def test_stale_baseline_top_k_raises(self) -> None:
        """JSON meta-block guard: baseline captured at top_k=5 cannot
        compare against a scenario asking for top_k=10."""
        outcome = _outcome(
            baseline=['n1'],
            expected_top_k=10,
            baseline_meta={
                'schema_version': 1,
                'top_k': 5,
                'search_type': 'memory',
            },
        )
        ans = AgentAnswer(retrieved_unit_ids=['u1'])
        with pytest.raises(RuntimeError, match='baseline JSON was captured at top_k=5'):
            outcome.score(ans, _scenario(top_k=10), note_key_to_unit_ids={'n1': ['u1']})

    def test_stale_baseline_search_type_raises(self) -> None:
        """JSON meta-block guard: baseline captured for note-search
        cannot compare against a memory-search scenario."""
        outcome = _outcome(
            baseline=['n1'],
            expected_search_type='memory',
            baseline_meta={
                'schema_version': 1,
                'top_k': 10,
                'search_type': 'note',
            },
        )
        ans = AgentAnswer(retrieved_unit_ids=['u1'])
        with pytest.raises(RuntimeError, match="baseline JSON was captured for 'note'"):
            outcome.score(ans, _scenario(), note_key_to_unit_ids={'n1': ['u1']})

    def test_stale_baseline_schema_version_raises(self) -> None:
        """JSON meta-block guard: baseline written at a prior
        schema_version cannot be compared at the current version."""
        outcome = _outcome(baseline=['n1'])
        outcome.baseline_meta = {
            'top_k': 10,
            'search_type': 'memory',
            'schema_version': 0,
        }
        ans = AgentAnswer(retrieved_unit_ids=['u1'])
        with pytest.raises(RuntimeError, match='schema_version'):
            outcome.score(ans, _scenario(), note_key_to_unit_ids={'n1': ['u1']})

    def test_wiring_error_top_k_raises(self) -> None:
        """Wiring-error guard: outcome expects top_k=10 but scenario
        registered at top_k=5 — code bug in __init__.py. JSON meta
        matches the scenario, so the wiring guard fires not the JSON
        guard."""
        outcome = _outcome(
            baseline=['n1'],
            expected_top_k=10,
            baseline_meta={
                'schema_version': 1,
                'top_k': 5,
                'search_type': 'memory',
            },
        )
        ans = AgentAnswer(retrieved_unit_ids=['u1'])
        with pytest.raises(RuntimeError, match='wiring error'):
            outcome.score(ans, _scenario(top_k=5), note_key_to_unit_ids={'n1': ['u1']})

    def test_wiring_error_search_type_raises(self) -> None:
        """Wiring-error guard: outcome expects 'memory' but scenario
        registered with 'note' — code bug in __init__.py. JSON meta
        matches the scenario. Pass note_key_to_unit_ids so the
        memory-branch resolver doesn't raise the missing-mapping
        guard before reaching the wiring guard."""
        outcome = _outcome(
            baseline=['n1'],
            expected_search_type='memory',
            baseline_meta={
                'schema_version': 1,
                'top_k': 10,
                'search_type': 'note',
            },
        )
        ans = AgentAnswer(retrieved_unit_ids=['note-id-1'])
        with pytest.raises(RuntimeError, match='wiring error'):
            outcome.score(
                ans,
                _scenario(search_type='note'),
                note_key_to_unit_ids={'n1': ['note-id-1']},
            )


class TestEmptyBaselineGuard:
    def test_empty_baseline_raises_with_recapture_hint(self) -> None:
        outcome = _outcome(baseline=[])
        ans = AgentAnswer(retrieved_unit_ids=['u1'])
        with pytest.raises(RuntimeError, match='no baseline captured'):
            outcome.score(ans, _scenario(), note_key_to_unit_ids={'n1': ['u1']})

    def test_empty_baseline_raises_under_meta_match(self) -> None:
        # The empty-baseline guard fires AFTER the meta-mismatch guards
        # — the error message should still point at recapture.
        outcome = _outcome(baseline=[], expected_top_k=10)
        ans = AgentAnswer(retrieved_unit_ids=['u1'])
        with pytest.raises(RuntimeError, match='MEMEX_EVAL_CAPTURE_BASELINES'):
            outcome.score(
                ans,
                _scenario(top_k=10),
                note_key_to_unit_ids={'n1': ['u1']},
            )

    def test_partial_meta_block_raises(self) -> None:
        """A baseline with a ranking but missing meta keys is refused
        — every guard would otherwise short-circuit on ``is None`` and
        silently let a stale baseline through."""
        outcome = _outcome(baseline=['n1'])
        outcome.baseline_meta = {}  # ranking present, meta missing
        ans = AgentAnswer(retrieved_unit_ids=['u1'])
        with pytest.raises(RuntimeError, match='missing required meta keys'):
            outcome.score(ans, _scenario(), note_key_to_unit_ids={'n1': ['u1']})

    def test_meta_block_missing_one_key_raises(self) -> None:
        """Partial meta block (e.g. older schema with no schema_version)
        is treated the same as fully-missing meta."""
        outcome = _outcome(baseline=['n1'])
        outcome.baseline_meta = {'top_k': 10, 'search_type': 'memory'}
        ans = AgentAnswer(retrieved_unit_ids=['u1'])
        with pytest.raises(RuntimeError, match="'schema_version'"):
            outcome.score(ans, _scenario(), note_key_to_unit_ids={'n1': ['u1']})


# ---------------------------------------------------------------------------
# Capture mode
# ---------------------------------------------------------------------------


class TestCaptureMode:
    @pytest.fixture(autouse=True)
    def _capture_env(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        monkeypatch.setenv('MEMEX_EVAL_CAPTURE_BASELINES', '1')
        return tmp_path

    def test_capture_writes_baseline_and_passes(self, _capture_env: Path) -> None:
        baseline_path = _capture_env / 's1-memory.json'
        # Even with empty baseline_ranking, capture mode succeeds —
        # it's persisting, not comparing.
        outcome = _outcome(
            baseline=[], baseline_path=str(baseline_path), expected_search_type='memory'
        )
        ans = AgentAnswer(retrieved_unit_ids=['u1', 'u2'])
        note_key_to_unit_ids = {'n1': ['u1'], 'n2': ['u2']}
        result = outcome.score(ans, _scenario(), note_key_to_unit_ids=note_key_to_unit_ids)
        assert result == {'rbo': 1.0, 'pass': 1.0}
        assert baseline_path.is_file()
        payload = json.loads(baseline_path.read_text())
        assert payload['ranking'] == ['n1', 'n2']
        assert payload['meta']['schema_version'] == 1
        assert payload['meta']['top_k'] == 10
        assert payload['meta']['search_type'] == 'memory'

    def test_capture_overwrites_existing_baseline(self, _capture_env: Path) -> None:
        baseline_path = _capture_env / 's1-memory.json'
        baseline_path.write_text('{"meta": {"schema_version": 0}, "ranking": ["stale"]}\n')
        outcome = _outcome(baseline=['stale'], baseline_path=str(baseline_path))
        ans = AgentAnswer(retrieved_unit_ids=['u1'])
        result = outcome.score(ans, _scenario(), note_key_to_unit_ids={'n1': ['u1']})
        assert result['pass'] == 1.0
        payload = json.loads(baseline_path.read_text())
        assert payload['ranking'] == ['n1']
        assert payload['meta']['schema_version'] == 1

    def test_capture_meta_mismatch_does_NOT_raise(self, _capture_env: Path) -> None:
        """Capture mode REWRITES the meta from the current state — a
        stale meta block (e.g. baseline captured at a previous top_k)
        should self-heal on recapture, not error. Wiring (outcome vs
        scenario) IS still validated since that's always a code bug."""
        baseline_path = _capture_env / 's1-memory.json'
        outcome = _outcome(
            baseline=['stale'],
            baseline_path=str(baseline_path),
            expected_top_k=10,
            expected_search_type='memory',
            # Stale meta on disk: previous capture was at top_k=5.
            baseline_meta={'schema_version': 1, 'top_k': 5, 'search_type': 'memory'},
        )
        ans = AgentAnswer(retrieved_unit_ids=['u1'])
        # Scenario is consistent with the outcome (wiring OK); only
        # the on-disk meta is stale. Capture rewrites it.
        result = outcome.score(ans, _scenario(top_k=10), note_key_to_unit_ids={'n1': ['u1']})
        assert result['pass'] == 1.0
        payload = json.loads(baseline_path.read_text())
        # Meta reflects the live scenario.top_k, not the stale value
        # on the prior baseline.
        assert payload['meta']['top_k'] == 10

    def test_capture_wiring_error_still_raises(self, _capture_env: Path) -> None:
        """A wiring error in __init__.py (outcome.expected_* diverges
        from scenario.*) IS a code bug and raises in capture mode too
        — capture should not paper over a bug it cannot fix."""
        baseline_path = _capture_env / 's1-memory.json'
        outcome = _outcome(
            baseline=['stale'],
            baseline_path=str(baseline_path),
            expected_top_k=10,
        )
        ans = AgentAnswer(retrieved_unit_ids=['u1'])
        with pytest.raises(RuntimeError, match='wiring error'):
            outcome.score(ans, _scenario(top_k=5), note_key_to_unit_ids={'n1': ['u1']})


class TestStrictMappingGuards:
    """The runner injects ``note_key_to_unit_ids`` (memory) and
    ``context['_note_id_by_key']`` (note) for every ingested suite.
    Missing mapping → fail loudly so a runner regression doesn't
    quietly score RBO=0."""

    def test_memory_search_without_note_key_to_unit_ids_raises(self) -> None:
        outcome = _outcome(baseline=['n1'], expected_search_type='memory')
        ans = AgentAnswer(retrieved_unit_ids=['u1'])
        with pytest.raises(RuntimeError, match='note_key_to_unit_ids'):
            outcome.score(ans, _scenario(), note_key_to_unit_ids=None)

    def test_memory_search_with_empty_mapping_raises(self) -> None:
        outcome = _outcome(baseline=['n1'], expected_search_type='memory')
        ans = AgentAnswer(retrieved_unit_ids=['u1'])
        with pytest.raises(RuntimeError, match='note_key_to_unit_ids'):
            outcome.score(ans, _scenario(), note_key_to_unit_ids={})

    def test_note_search_without_context_raises(self) -> None:
        outcome = _outcome(baseline=['n1'], expected_search_type='note')
        ans = AgentAnswer(retrieved_unit_ids=['nid-1'])
        with pytest.raises(RuntimeError, match='_note_id_by_key'):
            outcome.score(ans, _scenario(search_type='note'), context=None)

    def test_note_search_without_note_id_by_key_raises(self) -> None:
        outcome = _outcome(baseline=['n1'], expected_search_type='note')
        ans = AgentAnswer(retrieved_unit_ids=['nid-1'])
        with pytest.raises(RuntimeError, match='_note_id_by_key'):
            outcome.score(ans, _scenario(search_type='note'), context={})

    def test_note_search_with_empty_note_id_by_key_raises(self) -> None:
        """Empty ``_note_id_by_key={}`` must raise — silently scoring
        RBO=0 would mask a runner-contract break as a false retrieval
        regression."""
        outcome = _outcome(baseline=['n1'], expected_search_type='note')
        ans = AgentAnswer(retrieved_unit_ids=['nid-1'])
        with pytest.raises(RuntimeError, match='_note_id_by_key'):
            outcome.score(
                ans,
                _scenario(search_type='note'),
                context={'_note_id_by_key': {}},
            )


class TestBaselineFileIO:
    """The on-disk lifecycle: atomic capture write + corrupt-file
    refusal at load time."""

    def test_capture_write_is_atomic(self, tmp_path: Path) -> None:
        """``_write_baseline`` writes via tempfile + rename, so a
        crashed write never leaves a partial file at the canonical path."""
        baseline_path = tmp_path / 's1-memory.json'
        outcome = _outcome(baseline=['stale'], baseline_path=str(baseline_path))
        outcome._write_baseline(_scenario(), ['n1', 'n2'])
        # Tempfile is cleaned up; canonical path exists with valid JSON.
        assert not (tmp_path / 's1-memory.json.tmp').exists()
        assert baseline_path.is_file()
        payload = json.loads(baseline_path.read_text())
        assert payload['ranking'] == ['n1', 'n2']

    def test_load_corrupt_baseline_returns_sentinel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A truncated baseline JSON returns a corrupt sentinel rather
        than raising. ``_load_baseline`` raising at module-import time
        would crash ``memex-eval suite list`` for every suite, not just
        this one. Redirect ``_BASELINES_DIR`` to a tmp path so a test
        crash never leaves a corrupt file in the package."""
        import memex_eval.suites.retrieval_stability as suite_pkg

        monkeypatch.setattr(suite_pkg, '_BASELINES_DIR', tmp_path)
        (tmp_path / 's1-corrupt.json').write_text('{"meta": {"top_k": 10},')

        ranking, meta = suite_pkg._load_baseline('s1-corrupt')
        assert ranking == []
        assert meta.get('_corrupt') is True
        assert 's1-corrupt' in str(meta.get('_error', ''))

    def test_score_surfaces_corrupt_sentinel(self) -> None:
        """The outcome detects a corrupt sentinel in baseline_meta and
        raises a per-scenario RuntimeError so the runner records
        status='error' for THIS scenario without breaking the suite."""
        outcome = _outcome(baseline=[], baseline_meta={'_corrupt': True, '_error': 'truncated'})
        ans = AgentAnswer(retrieved_unit_ids=['u1'])
        with pytest.raises(RuntimeError, match='corrupt'):
            outcome.score(ans, _scenario(), note_key_to_unit_ids={'n1': ['u1']})


def test_metric_keys_declared() -> None:
    outcome = _outcome(baseline=['n1'])
    assert outcome.metric_keys() == ['rbo', 'pass']


def test_p_kwarg_propagates_through_to_rbo() -> None:
    # At p=0.5 a top-1 swap hurts more than at p=0.99.
    ans = AgentAnswer(retrieved_unit_ids=['u2', 'u1', 'u3', 'u4', 'u5'])
    note_key_to_unit_ids = {f'n{i}': [f'u{i}'] for i in range(1, 6)}
    low = _outcome(baseline=['n1', 'n2', 'n3', 'n4', 'n5'], p=0.5).score(
        ans, _scenario(), note_key_to_unit_ids=note_key_to_unit_ids
    )['rbo']
    high = _outcome(baseline=['n1', 'n2', 'n3', 'n4', 'n5'], p=0.99).score(
        ans, _scenario(), note_key_to_unit_ids=note_key_to_unit_ids
    )['rbo']
    assert low < high


def test_outcome_registered() -> None:
    """Importing the suite package registers the outcome by name."""
    from memex_eval.suite.base import list_outcomes

    assert 'ranking_baseline_rbo' in list_outcomes()


def test_setup_action_registered() -> None:
    """Importing the suite package registers the setup action by name."""
    from memex_eval.suite.setup_actions import list_setup_actions

    assert 'seed_paragraphs_from_sources' in list_setup_actions()
