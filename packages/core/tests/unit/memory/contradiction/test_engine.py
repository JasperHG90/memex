"""Unit tests for ContradictionEngine."""

import pytest
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from memex_common.config import ContradictionConfig
from memex_core.memory.contradiction.engine import ContradictionEngine
from memex_core.memory.sql_models import MemoryUnit, ContentStatus


def _make_unit(
    text: str = '',
    event_date: datetime | None = None,
    confidence: float = 1.0,
    note_id=None,
) -> MemoryUnit:
    """Create a test MemoryUnit."""
    return MemoryUnit(
        id=uuid4(),
        text=text or f'Test fact {uuid4()}',
        fact_type='world',
        status=ContentStatus.ACTIVE,
        event_date=event_date or datetime.now(timezone.utc),
        vault_id=uuid4(),
        note_id=note_id or uuid4(),
        embedding=[0.1] * 384,
        confidence=confidence,
    )


@pytest.fixture
def config():
    return ContradictionConfig(
        enabled=True,
        alpha=0.1,
        similarity_threshold=0.5,
        max_candidates_per_unit=15,
        superseded_threshold=0.3,
    )


@pytest.fixture
def mock_lm():
    return MagicMock()


@pytest.fixture
def engine(mock_lm, config):
    return ContradictionEngine(lm=mock_lm, config=config)


class TestTriage:
    """Test that triage correctly filters units."""

    @pytest.mark.asyncio
    async def test_triage_returns_empty_for_new_facts(self, engine):
        """Most units are new info -- triage should return empty."""
        units = [_make_unit(text='The sky is blue'), _make_unit(text='Water is wet')]

        mock_result = MagicMock()
        mock_result.flagged_ids = []

        with patch('memex_core.memory.contradiction.engine.run_dspy_operation') as mock_op:
            mock_op.return_value = (mock_result, MagicMock())
            result = await engine._triage(units)

        assert result == []

    @pytest.mark.asyncio
    async def test_triage_flags_corrective_units(self, engine):
        """Units with corrective language should be flagged."""
        correction = _make_unit(text='Actually, the backlog has 5 items, not 15')
        new_fact = _make_unit(text='The meeting is at 3pm')
        units = [correction, new_fact]

        mock_result = MagicMock()
        mock_result.flagged_ids = [str(correction.id)]

        with patch('memex_core.memory.contradiction.engine.run_dspy_operation') as mock_op:
            mock_op.return_value = (mock_result, MagicMock())
            result = await engine._triage(units)

        assert str(correction.id) in result
        assert str(new_fact.id) not in result

    @pytest.mark.asyncio
    async def test_triage_handles_string_response(self, engine):
        """Triage should handle flagged_ids returned as JSON string."""
        unit = _make_unit()

        mock_result = MagicMock()
        mock_result.flagged_ids = json.dumps([str(unit.id)])

        with patch('memex_core.memory.contradiction.engine.run_dspy_operation') as mock_op:
            mock_op.return_value = (mock_result, MagicMock())
            result = await engine._triage([unit])

        assert str(unit.id) in result

    @pytest.mark.asyncio
    async def test_triage_handles_invalid_string(self, engine):
        """Triage should return empty list on unparseable string."""
        unit = _make_unit()

        mock_result = MagicMock()
        mock_result.flagged_ids = 'not valid json {'

        with patch('memex_core.memory.contradiction.engine.run_dspy_operation') as mock_op:
            mock_op.return_value = (mock_result, MagicMock())
            result = await engine._triage([unit])

        assert result == []

    @pytest.mark.asyncio
    async def test_triage_handles_none_flagged(self, engine):
        """Triage should handle None flagged_ids gracefully."""
        unit = _make_unit()

        mock_result = MagicMock()
        mock_result.flagged_ids = None

        with patch('memex_core.memory.contradiction.engine.run_dspy_operation') as mock_op:
            mock_op.return_value = (mock_result, MagicMock())
            result = await engine._triage([unit])

        assert result == []


class TestConfidenceAdjustment:
    """Test confidence adjustment logic (Hindsight Eq. 26)."""

    def test_contradict_decreases_by_2alpha(self, config):
        """Contradict should decrease confidence by 2*alpha."""
        alpha = config.alpha
        initial = 1.0
        expected = max(initial - 2 * alpha, 0.0)
        assert expected == pytest.approx(0.8)

    def test_weaken_decreases_by_alpha(self, config):
        """Weaken should decrease confidence by alpha."""
        alpha = config.alpha
        initial = 1.0
        expected = max(initial - alpha, 0.0)
        assert expected == pytest.approx(0.9)

    def test_reinforce_increases_by_alpha(self, config):
        """Reinforce should increase confidence by alpha."""
        alpha = config.alpha
        initial = 0.8
        expected = min(initial + alpha, 1.0)
        assert expected == pytest.approx(0.9)

    def test_confidence_clamped_at_zero(self, config):
        """Confidence should never go below 0."""
        alpha = config.alpha
        initial = 0.1
        expected = max(initial - 2 * alpha, 0.0)
        assert expected == pytest.approx(0.0)

    def test_confidence_clamped_at_one(self, config):
        """Confidence should never exceed 1.0."""
        alpha = config.alpha
        initial = 0.95
        expected = min(initial + alpha, 1.0)
        assert expected == pytest.approx(1.0)


class TestResolveAuthority:
    """Test authority resolution based on temporal ordering."""

    def test_newer_unit_is_authoritative_by_default(self, engine):
        """By default, newer event_date wins when LLM agrees."""
        old = _make_unit(event_date=datetime(2024, 1, 1, tzinfo=timezone.utc))
        new = _make_unit(event_date=datetime(2025, 1, 1, tzinfo=timezone.utc))

        auth, superseded = engine._resolve_authority(new, old, 'new')
        assert auth.id == new.id
        assert superseded.id == old.id

    def test_older_unit_wins_with_llm_override(self, engine):
        """LLM can override temporal heuristic."""
        old = _make_unit(event_date=datetime(2024, 1, 1, tzinfo=timezone.utc))
        new = _make_unit(event_date=datetime(2025, 1, 1, tzinfo=timezone.utc))

        auth, superseded = engine._resolve_authority(new, old, 'existing')
        assert auth.id == old.id
        assert superseded.id == new.id

    def test_same_timestamp_defers_to_llm_existing(self, engine):
        """When timestamps match, LLM hint decides."""
        now = datetime.now(timezone.utc)
        a = _make_unit(event_date=now)
        b = _make_unit(event_date=now)

        auth, superseded = engine._resolve_authority(a, b, 'existing')
        assert auth.id == b.id

    def test_same_timestamp_defers_to_llm_new(self, engine):
        """When timestamps match, new hint picks the new unit."""
        now = datetime.now(timezone.utc)
        a = _make_unit(event_date=now)
        b = _make_unit(event_date=now)

        auth, superseded = engine._resolve_authority(a, b, 'new')
        assert auth.id == a.id

    def test_invalid_hint_falls_back_to_temporal(self, engine):
        """Invalid LLM hint falls back to temporal ordering."""
        old = _make_unit(event_date=datetime(2024, 1, 1, tzinfo=timezone.utc))
        new = _make_unit(event_date=datetime(2025, 1, 1, tzinfo=timezone.utc))

        auth, superseded = engine._resolve_authority(new, old, 'garbage')
        assert auth.id == new.id
        assert superseded.id == old.id


class TestClassify:
    """Test relationship classification."""

    @pytest.mark.asyncio
    async def test_classify_filters_invalid_relations(self, engine):
        """Only valid relations (reinforce/weaken/contradict) should pass through."""
        unit = _make_unit()
        candidates = [_make_unit()]

        mock_result = MagicMock()
        mock_result.relationships = [
            {
                'existing_id': str(candidates[0].id),
                'relation': 'neutral',
                'reasoning': 'unrelated',
            },
            {
                'existing_id': str(candidates[0].id),
                'relation': 'contradict',
                'reasoning': 'directly contradicts',
            },
        ]

        with patch('memex_core.memory.contradiction.engine.run_dspy_operation') as mock_op:
            mock_op.return_value = (mock_result, MagicMock())
            result = await engine._classify(unit, candidates)

        assert len(result) == 1
        assert result[0]['relation'] == 'contradict'

    @pytest.mark.asyncio
    async def test_classify_handles_string_response(self, engine):
        """Classify should handle relationships returned as JSON string."""
        unit = _make_unit()
        candidates = [_make_unit()]

        mock_result = MagicMock()
        mock_result.relationships = json.dumps(
            [
                {
                    'existing_id': str(candidates[0].id),
                    'relation': 'weaken',
                    'reasoning': 'partially outdated',
                },
            ]
        )

        with patch('memex_core.memory.contradiction.engine.run_dspy_operation') as mock_op:
            mock_op.return_value = (mock_result, MagicMock())
            result = await engine._classify(unit, candidates)

        assert len(result) == 1
        assert result[0]['relation'] == 'weaken'

    @pytest.mark.asyncio
    async def test_classify_empty_on_invalid_json(self, engine):
        """If LLM returns garbage, classify should return empty."""
        unit = _make_unit()
        candidates = [_make_unit()]

        mock_result = MagicMock()
        mock_result.relationships = 'not valid json {'

        with patch('memex_core.memory.contradiction.engine.run_dspy_operation') as mock_op:
            mock_op.return_value = (mock_result, MagicMock())
            result = await engine._classify(unit, candidates)

        assert result == []

    @pytest.mark.asyncio
    async def test_classify_returns_all_valid_relations(self, engine):
        """All three valid relation types should pass through."""
        unit = _make_unit()
        c1, c2, c3 = _make_unit(), _make_unit(), _make_unit()
        candidates = [c1, c2, c3]

        mock_result = MagicMock()
        mock_result.relationships = [
            {'existing_id': str(c1.id), 'relation': 'reinforce', 'reasoning': 'agrees'},
            {'existing_id': str(c2.id), 'relation': 'weaken', 'reasoning': 'partial'},
            {'existing_id': str(c3.id), 'relation': 'contradict', 'reasoning': 'opposite'},
        ]

        with patch('memex_core.memory.contradiction.engine.run_dspy_operation') as mock_op:
            mock_op.return_value = (mock_result, MagicMock())
            result = await engine._classify(unit, candidates)

        assert len(result) == 3
        relations = {r['relation'] for r in result}
        assert relations == {'reinforce', 'weaken', 'contradict'}

    @pytest.mark.asyncio
    async def test_classify_handles_none_response(self, engine):
        """Classify should handle None relationships gracefully."""
        unit = _make_unit()
        candidates = [_make_unit()]

        mock_result = MagicMock()
        mock_result.relationships = None

        with patch('memex_core.memory.contradiction.engine.run_dspy_operation') as mock_op:
            mock_op.return_value = (mock_result, MagicMock())
            result = await engine._classify(unit, candidates)

        assert result == []


class TestDetectContradictions:
    """Test the full detect_contradictions flow."""

    @pytest.mark.asyncio
    async def test_catches_exceptions(self, engine):
        """detect_contradictions should never raise -- it's a background task."""
        session_factory = MagicMock()
        session_factory.return_value.__aenter__ = AsyncMock(side_effect=RuntimeError('DB down'))
        session_factory.return_value.__aexit__ = AsyncMock()

        await engine.detect_contradictions(
            session_factory=session_factory,
            document_id='test-doc',
            unit_ids=[uuid4()],
            vault_id=uuid4(),
        )

    @pytest.mark.asyncio
    async def test_empty_unit_ids_is_noop(self, engine):
        """Empty unit_ids should do nothing."""
        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.all.return_value = []
        session.exec.return_value = mock_result

        session_factory = MagicMock()
        session_factory.return_value.__aenter__ = AsyncMock(return_value=session)
        session_factory.return_value.__aexit__ = AsyncMock()

        await engine.detect_contradictions(
            session_factory=session_factory,
            document_id='test-doc',
            unit_ids=[],
            vault_id=uuid4(),
        )


class TestTemporalDefault:
    """Test _temporal_default static method."""

    def test_newer_returns_new(self):
        old = _make_unit(event_date=datetime(2024, 1, 1, tzinfo=timezone.utc))
        new = _make_unit(event_date=datetime(2025, 1, 1, tzinfo=timezone.utc))
        assert ContradictionEngine._temporal_default(new, old) == 'new'

    def test_older_returns_existing(self):
        old = _make_unit(event_date=datetime(2024, 1, 1, tzinfo=timezone.utc))
        new = _make_unit(event_date=datetime(2025, 1, 1, tzinfo=timezone.utc))
        assert ContradictionEngine._temporal_default(old, new) == 'existing'

    def test_equal_dates_returns_new(self):
        now = datetime.now(timezone.utc)
        a = _make_unit(event_date=now)
        b = _make_unit(event_date=now)
        assert ContradictionEngine._temporal_default(a, b) == 'new'
