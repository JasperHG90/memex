"""Unit tests for the `ranking_baseline_rbo` outcome.

Tests run against synthetic ``AgentAnswer`` + ``Scenario`` instances —
no DB, no model inference. The outcome's ``score()`` is the unit under
test; the framework's runner is not exercised here.

The outcome compares ``answer.retrieved_unit_ids`` (raw UUID strings)
directly against ``baseline_ranking`` (also UUID strings) via RBO. The
suite's shipped snapshot pins those UUIDs across runs, so the unit
test can use any stable strings (``'u1'``, ``'u2'``, …) without
needing the runner's note_key inversion.
"""

from __future__ import annotations

import json

import pytest

# Side-effect import: registers `ranking_baseline_rbo` in the outcome
# registry and `seed_paragraphs_from_sources` in the setup-action
# registry. Tests below construct the outcome class directly so the
# registration isn't strictly required, but importing the suite package
# is the contract that future-proofs reorganisations.
import memex_eval.suites.retrieval_stability  # noqa: F401
from memex_eval.suite import AgentAnswer, Scenario
from memex_eval.suite.base import KeywordsPresent
from memex_eval.suites.retrieval_stability._outcomes import (
    _CAPTURE_ENV_VAR,
    RankingBaselineRbo,
)

_SCHEMA = 3


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
    rbo_floor: float = 0.92,
) -> RankingBaselineRbo:
    if baseline_meta is None:
        baseline_meta = {
            'schema_version': _SCHEMA,
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


class TestVerifyHappyPath:
    """Baseline + retrieved IDs match → RBO=1.0 → pass."""

    def test_identical_ranking_returns_one(self) -> None:
        outcome = _outcome(baseline=['u1', 'u2', 'u3'])
        ans = AgentAnswer(retrieved_unit_ids=['u1', 'u2', 'u3'])
        result = outcome.score(ans, _scenario())
        assert result['rbo'] == pytest.approx(1.0)
        assert result['pass'] == 1.0

    def test_completely_disjoint_returns_low(self) -> None:
        outcome = _outcome(baseline=['u1', 'u2', 'u3'])
        ans = AgentAnswer(retrieved_unit_ids=['u4', 'u5', 'u6'])
        result = outcome.score(ans, _scenario())
        assert result['rbo'] == pytest.approx(0.0)
        assert result['pass'] == 0.0

    def test_partial_overlap_passes_above_floor(self) -> None:
        # 9 of 10 in same position; one swap at the tail — RBO well above 0.92
        outcome = _outcome(baseline=['u1', 'u2', 'u3', 'u4', 'u5', 'u6', 'u7', 'u8', 'u9', 'u10'])
        ans = AgentAnswer(
            retrieved_unit_ids=['u1', 'u2', 'u3', 'u4', 'u5', 'u6', 'u7', 'u8', 'u10', 'u9']
        )
        result = outcome.score(ans, _scenario())
        assert result['rbo'] > 0.92
        assert result['pass'] == 1.0

    def test_top_position_swap_fails_floor(self) -> None:
        # Swap rank 1 and rank 2 — heavy RBO penalty at p=0.9
        outcome = _outcome(baseline=['u1', 'u2', 'u3'])
        ans = AgentAnswer(retrieved_unit_ids=['u2', 'u1', 'u3'])
        result = outcome.score(ans, _scenario())
        # RBO of a top-1-vs-top-2 swap at p=0.9 ≈ 0.81 — below 0.92 floor
        assert result['pass'] == 0.0

    def test_truncates_to_top_k(self) -> None:
        """Retrieved IDs beyond top_k MUST NOT contribute to RBO."""
        outcome = _outcome(
            baseline=['u1', 'u2', 'u3'],
            expected_top_k=3,
        )
        ans = AgentAnswer(retrieved_unit_ids=['u1', 'u2', 'u3', 'garbage1', 'garbage2'])
        result = outcome.score(ans, _scenario(top_k=3))
        assert result['rbo'] == pytest.approx(1.0)
        assert result['pass'] == 1.0


# ---------------------------------------------------------------------------
# Search-type wiring guards
# ---------------------------------------------------------------------------


class TestSearchTypeWiring:
    """``expected_search_type`` mismatch with scenario raises clearly."""

    def test_memory_outcome_on_note_scenario_raises(self) -> None:
        outcome = _outcome(baseline=['u1'], expected_search_type='memory')
        with pytest.raises(RuntimeError, match='search_type'):
            outcome.score(
                AgentAnswer(retrieved_unit_ids=['u1']),
                _scenario(search_type='note'),
            )

    def test_note_outcome_on_memory_scenario_raises(self) -> None:
        outcome = _outcome(baseline=['n1'], expected_search_type='note')
        with pytest.raises(RuntimeError, match='search_type'):
            outcome.score(
                AgentAnswer(retrieved_unit_ids=['n1']),
                _scenario(search_type='memory'),
            )

    def test_top_k_mismatch_raises(self) -> None:
        outcome = _outcome(baseline=['u1'], expected_top_k=5)
        with pytest.raises(RuntimeError, match='top_k'):
            outcome.score(AgentAnswer(retrieved_unit_ids=['u1']), _scenario(top_k=10))


# ---------------------------------------------------------------------------
# Empty / corrupt / stale baseline guards (verify mode)
# ---------------------------------------------------------------------------


class TestBaselineGuards:
    def test_empty_baseline_raises_capture_pending(self) -> None:
        outcome = _outcome(baseline=[])
        ans = AgentAnswer(retrieved_unit_ids=['u1', 'u2'])
        with pytest.raises(RuntimeError, match='no baseline captured'):
            outcome.score(ans, _scenario())

    def test_corrupt_baseline_raises_recapture_hint(self) -> None:
        outcome = _outcome(
            baseline=[],  # forced empty + corrupt sentinel
            baseline_meta={'_corrupt': True, '_error': 'bad json'},
        )
        ans = AgentAnswer(retrieved_unit_ids=['u1'])
        with pytest.raises(RuntimeError, match='corrupt'):
            outcome.score(ans, _scenario())

    def test_missing_meta_keys_raises(self) -> None:
        outcome = _outcome(
            baseline=['u1'],
            baseline_meta={},  # no top_k / search_type / schema_version
        )
        ans = AgentAnswer(retrieved_unit_ids=['u1'])
        with pytest.raises(RuntimeError, match='missing required meta keys'):
            outcome.score(ans, _scenario())

    def test_baseline_top_k_mismatch_raises(self) -> None:
        outcome = _outcome(
            baseline=['u1'],
            baseline_meta={
                'schema_version': _SCHEMA,
                'top_k': 5,  # baseline captured at 5
                'search_type': 'memory',
            },
            expected_top_k=10,
        )
        ans = AgentAnswer(retrieved_unit_ids=['u1'])
        with pytest.raises(RuntimeError, match='baseline meta mismatch'):
            outcome.score(ans, _scenario(top_k=10))

    def test_baseline_search_type_mismatch_raises(self) -> None:
        outcome = _outcome(
            baseline=['u1'],
            baseline_meta={
                'schema_version': _SCHEMA,
                'top_k': 10,
                'search_type': 'note',  # baseline captured for note
            },
            expected_search_type='memory',
        )
        ans = AgentAnswer(retrieved_unit_ids=['u1'])
        with pytest.raises(RuntimeError, match='captured for'):
            outcome.score(ans, _scenario(search_type='memory'))

    def test_schema_version_mismatch_raises(self) -> None:
        outcome = _outcome(
            baseline=['u1'],
            baseline_meta={
                'schema_version': 1,  # old schema
                'top_k': 10,
                'search_type': 'memory',
            },
        )
        ans = AgentAnswer(retrieved_unit_ids=['u1'])
        with pytest.raises(RuntimeError, match='schema_version mismatch'):
            outcome.score(ans, _scenario())

    def test_config_pins_mismatch_raises(self) -> None:
        """A baseline captured at one set of knob values must NOT
        verify against an outcome configured with different knob
        values — the suite author either restored the prior pins or
        recaptures. Either way, scoring a stale knob against a new
        ranking is a silent invalidation we must prevent.
        """
        outcome = RankingBaselineRbo(
            type='ranking_baseline_rbo',
            baseline_path='/tmp/test-baseline.json',
            baseline_ranking=['u1'],
            baseline_meta={
                'schema_version': _SCHEMA,
                'top_k': 10,
                'search_type': 'memory',
                'config_pins': {'composite_boost_log_clip': '5.0'},
            },
            expected_top_k=10,
            expected_search_type='memory',
            config_pins={'composite_boost_log_clip': 'inf'},
        )
        ans = AgentAnswer(retrieved_unit_ids=['u1'])
        with pytest.raises(RuntimeError, match='config_pins mismatch'):
            outcome.score(ans, _scenario())

    def test_config_pins_match_passes(self) -> None:
        """Matching pins must not raise. Sanity-check the comparison
        is structural-equality not identity.
        """
        pins = {'composite_boost_log_clip': 'inf'}
        outcome = RankingBaselineRbo(
            type='ranking_baseline_rbo',
            baseline_path='/tmp/test-baseline.json',
            baseline_ranking=['u1'],
            baseline_meta={
                'schema_version': _SCHEMA,
                'top_k': 10,
                'search_type': 'memory',
                'config_pins': dict(pins),
            },
            expected_top_k=10,
            expected_search_type='memory',
            config_pins=dict(pins),
        )
        ans = AgentAnswer(retrieved_unit_ids=['u1'])
        result = outcome.score(ans, _scenario())
        assert result['pass'] == 1.0


# ---------------------------------------------------------------------------
# Capture mode
# ---------------------------------------------------------------------------


class TestCaptureMode:
    def test_writes_baseline_and_returns_pass(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv(_CAPTURE_ENV_VAR, '1')
        baseline_file = tmp_path / 's1.json'
        outcome = _outcome(baseline=[], baseline_path=str(baseline_file))
        ans = AgentAnswer(retrieved_unit_ids=['u1', 'u2', 'u3'])
        result = outcome.score(ans, _scenario())
        assert result['pass'] == 1.0
        assert result['rbo'] == 1.0
        # File must exist + parse cleanly.
        payload = json.loads(baseline_file.read_text())
        assert payload['ranking'] == ['u1', 'u2', 'u3']
        assert payload['meta']['schema_version'] == _SCHEMA
        assert payload['meta']['top_k'] == 10
        assert payload['meta']['search_type'] == 'memory'
        # ``config_pins`` must round-trip into the persisted meta so a
        # later verify-against-stale-knobs run can detect mismatch.
        assert payload['meta']['config_pins'] == {}

    def test_truncates_to_top_k_on_capture(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv(_CAPTURE_ENV_VAR, '1')
        baseline_file = tmp_path / 's1.json'
        outcome = _outcome(
            baseline=[],
            baseline_path=str(baseline_file),
            expected_top_k=3,
        )
        ans = AgentAnswer(retrieved_unit_ids=['u1', 'u2', 'u3', 'u4', 'u5'])
        outcome.score(ans, _scenario(top_k=3))
        payload = json.loads(baseline_file.read_text())
        assert payload['ranking'] == ['u1', 'u2', 'u3']

    def test_atomic_write_no_tmp_file_left(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv(_CAPTURE_ENV_VAR, '1')
        baseline_file = tmp_path / 's1.json'
        outcome = _outcome(baseline=[], baseline_path=str(baseline_file))
        outcome.score(AgentAnswer(retrieved_unit_ids=['u1']), _scenario())
        # Tmp suffix is .json.tmp — must not survive a successful write.
        assert not (tmp_path / 's1.json.tmp').exists()

    def test_capture_self_heals_meta(self, monkeypatch, tmp_path) -> None:
        """A baseline captured at top_k=5 is overwritten at top_k=10 on
        re-capture; meta block reflects the new scenario state."""
        monkeypatch.setenv(_CAPTURE_ENV_VAR, '1')
        baseline_file = tmp_path / 's1.json'
        baseline_file.write_text(
            json.dumps(
                {
                    'meta': {'schema_version': 1, 'top_k': 5, 'search_type': 'memory'},
                    'ranking': ['stale'],
                },
            )
        )
        outcome = _outcome(baseline=[], baseline_path=str(baseline_file))
        outcome.score(AgentAnswer(retrieved_unit_ids=['u1', 'u2']), _scenario(top_k=10))
        payload = json.loads(baseline_file.read_text())
        assert payload['meta']['top_k'] == 10
        assert payload['meta']['schema_version'] == _SCHEMA
        assert payload['ranking'] == ['u1', 'u2']
