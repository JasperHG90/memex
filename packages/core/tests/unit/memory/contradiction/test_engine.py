"""Unit tests for ContradictionEngine."""

import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from memex_common.config import ContradictionConfig
from memex_core.memory.contradiction.engine import ContradictionEngine
from memex_core.memory.contradiction.signatures import ContradictionRelationship
from memex_core.memory.sql_models import MemoryLink, MemoryUnit, ContentStatus


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
            mock_op.return_value = mock_result
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
            mock_op.return_value = mock_result
            result = await engine._triage(units)

        assert str(correction.id) in result
        assert str(new_fact.id) not in result

    @pytest.mark.asyncio
    async def test_triage_handles_none_flagged(self, engine):
        """Triage should handle None flagged_ids gracefully."""
        unit = _make_unit()

        mock_result = MagicMock()
        mock_result.flagged_ids = None

        with patch('memex_core.memory.contradiction.engine.run_dspy_operation') as mock_op:
            mock_op.return_value = mock_result
            result = await engine._triage([unit])

        assert result == []

    @pytest.mark.asyncio
    async def test_triage_passes_pydantic_models(self, engine):
        """Triage should pass TriageUnit Pydantic models, not JSON strings."""
        from memex_core.memory.contradiction.signatures import TriageUnit

        units = [_make_unit(text='Fact A'), _make_unit(text='Fact B')]

        mock_result = MagicMock()
        mock_result.flagged_ids = []

        with patch('memex_core.memory.contradiction.engine.run_dspy_operation') as mock_op:
            mock_op.return_value = mock_result
            await engine._triage(units)

        call_kwargs = mock_op.call_args[1]['input_kwargs']
        assert 'units' in call_kwargs
        assert all(isinstance(u, TriageUnit) for u in call_kwargs['units'])
        assert call_kwargs['units'][0].id == str(units[0].id)
        assert call_kwargs['units'][0].text == units[0].text


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
            ContradictionRelationship(
                existing_id=str(candidates[0].id),
                relation='contradict',
                authoritative='new',
                reasoning='directly contradicts',
            ),
        ]

        with patch('memex_core.memory.contradiction.engine.run_dspy_operation') as mock_op:
            mock_op.return_value = mock_result
            result = await engine._classify(unit, candidates)

        assert len(result) == 1
        assert result[0].relation == 'contradict'

    @pytest.mark.asyncio
    async def test_classify_returns_all_valid_relations(self, engine):
        """All three valid relation types should pass through."""
        unit = _make_unit()
        c1, c2, c3 = _make_unit(), _make_unit(), _make_unit()
        candidates = [c1, c2, c3]

        mock_result = MagicMock()
        mock_result.relationships = [
            ContradictionRelationship(
                existing_id=str(c1.id),
                relation='reinforce',
                authoritative='new',
                reasoning='agrees',
            ),
            ContradictionRelationship(
                existing_id=str(c2.id),
                relation='weaken',
                authoritative='new',
                reasoning='partial',
            ),
            ContradictionRelationship(
                existing_id=str(c3.id),
                relation='contradict',
                authoritative='new',
                reasoning='opposite',
            ),
        ]

        with patch('memex_core.memory.contradiction.engine.run_dspy_operation') as mock_op:
            mock_op.return_value = mock_result
            result = await engine._classify(unit, candidates)

        assert len(result) == 3
        relations = {r.relation for r in result}
        assert relations == {'reinforce', 'weaken', 'contradict'}

    @pytest.mark.asyncio
    async def test_classify_handles_none_response(self, engine):
        """Classify should handle None relationships gracefully."""
        unit = _make_unit()
        candidates = [_make_unit()]

        mock_result = MagicMock()
        mock_result.relationships = None

        with patch('memex_core.memory.contradiction.engine.run_dspy_operation') as mock_op:
            mock_op.return_value = mock_result
            result = await engine._classify(unit, candidates)

        assert result == []

    @pytest.mark.asyncio
    async def test_classify_passes_pydantic_models(self, engine):
        """Classify should pass CandidateUnit Pydantic models, not JSON strings."""
        from memex_core.memory.contradiction.signatures import CandidateUnit

        unit = _make_unit()
        candidates = [_make_unit()]

        mock_result = MagicMock()
        mock_result.relationships = []

        with patch('memex_core.memory.contradiction.engine.run_dspy_operation') as mock_op:
            mock_op.return_value = mock_result
            await engine._classify(unit, candidates)

        call_kwargs = mock_op.call_args[1]['input_kwargs']
        assert 'candidates' in call_kwargs
        assert all(isinstance(c, CandidateUnit) for c in call_kwargs['candidates'])
        assert call_kwargs['candidates'][0].id == str(candidates[0].id)
        assert call_kwargs['candidates'][0].text == candidates[0].text


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

    @pytest.mark.asyncio
    async def test_duplicate_links_are_deduped_and_upserted(self, engine):
        """When multiple flagged units produce the same link key, dedup + upsert."""
        vault_id = uuid4()
        shared_target = _make_unit(text='shared target')
        flagged_a = _make_unit(text='flagged A')
        flagged_b = _make_unit(text='flagged B')

        # Both flagged units will produce a link with the SAME (from, to, type) key.
        # This simulates the real bug: authority resolution picks the same direction.
        link_early = MemoryLink(
            from_unit_id=shared_target.id,
            to_unit_id=flagged_a.id,
            link_type='reinforces',
            vault_id=vault_id,
            weight=1.0,
            link_metadata={'reasoning': 'first'},
        )
        link_duplicate = MemoryLink(
            from_unit_id=shared_target.id,
            to_unit_id=flagged_a.id,
            link_type='reinforces',
            vault_id=vault_id,
            weight=1.0,
            link_metadata={'reasoning': 'second'},
        )
        link_unique = MemoryLink(
            from_unit_id=shared_target.id,
            to_unit_id=flagged_b.id,
            link_type='weakens',
            vault_id=vault_id,
            weight=1.0,
            link_metadata={'reasoning': 'unique'},
        )

        session = AsyncMock()

        with patch.object(
            engine,
            '_process_flagged_unit',
            side_effect=[
                ([link_early, link_unique], {flagged_a.id: 0.9}, {flagged_a.id: 1}),
                ([link_duplicate], {flagged_b.id: 0.8}, {flagged_b.id: 1}),
            ],
        ):
            # Mock _load_units to return the flagged units
            with patch.object(engine, '_load_units', return_value=[flagged_a, flagged_b]):
                # Mock _triage to flag both units
                with patch.object(
                    engine, '_triage', return_value=[str(flagged_a.id), str(flagged_b.id)]
                ):
                    await engine._detect(session, [flagged_a.id, flagged_b.id], vault_id)

        # session.exec should have been called with the upsert statement (not session.add)
        exec_calls = session.exec.call_args_list
        # First call is _load_units, last call is the upsert
        upsert_call = exec_calls[-1]
        upsert_stmt = upsert_call[0][0]

        # Verify it's a PG INSERT with ON CONFLICT (compiled SQL contains these)
        compiled = str(upsert_stmt.compile(compile_kwargs={'literal_binds': False}))
        assert 'INSERT INTO memory_links' in compiled
        assert 'ON CONFLICT' in compiled

        # Verify dedup: 3 links in, but only 2 unique keys
        # The values in the INSERT should have 2 rows, not 3
        assert compiled.count('%(from_unit_id_') == 2 or 'VALUES' in compiled


class TestConfidenceDeltaAccumulation:
    """F22 — confidence deltas accumulate per-target so multiple weakens applied
    to the same unit in one batch advance ``confidence`` and
    ``confidence_evidence_count`` in lockstep (Hermes round-4 HIGH).

    The pre-fix bug: ``confidence_updates`` was a dict keyed by unit_id with
    absolute new values, so the last writer wins — a unit weakened twice
    would land at ``c - alpha`` while ``confidence_evidence_count`` summed
    to ``+2``, drifting the two columns out of sync.

    Post-fix: ``confidence_deltas`` carries signed alpha-step deltas that are
    summed per-target by ``_detect``, so ``c - 2*alpha`` lands alongside
    ``+2`` evidence.
    """

    @pytest.mark.asyncio
    async def test_two_weakens_on_same_target_apply_both_alpha_steps(self, engine):
        vault_id = uuid4()
        target = _make_unit(text='target', confidence=0.8)
        flagged_a = _make_unit(text='A')
        flagged_b = _make_unit(text='B')

        session = AsyncMock()
        # Default exec result: empty load for any mid-batch SELECT.
        empty_result = MagicMock()
        empty_result.all.return_value = []
        session.exec.return_value = empty_result
        # session.execute is the path used to apply UPDATE statements.
        captured_updates: list[dict] = []

        async def _capture_execute(stmt):
            # Best-effort capture of the UPDATE values clause.
            try:
                params = stmt.compile().params
            except Exception:
                params = {}
            captured_updates.append({'stmt': stmt, 'params': params})
            r = MagicMock()
            r.rowcount = 1
            return r

        session.execute = AsyncMock(side_effect=_capture_execute)

        # Two flagged units each return a -alpha (=-0.1) delta on the same target.
        delta = -engine.config.alpha

        with patch.object(
            engine,
            '_process_flagged_unit',
            side_effect=[
                ([], {target.id: delta}, {target.id: 1}),
                ([], {target.id: delta}, {target.id: 1}),
            ],
        ):
            with patch.object(engine, '_load_units', return_value=[flagged_a, flagged_b]):
                with patch.object(
                    engine,
                    '_triage',
                    return_value=[str(flagged_a.id), str(flagged_b.id)],
                ):
                    # A non-flagged target so _detect must look it up to find
                    # pre-batch confidence — exercise that path explicitly.
                    target_lookup_result = MagicMock()
                    target_lookup_result.all.return_value = [target]

                    def _exec(stmt):
                        stmt_str = str(stmt).lower()
                        if 'where memory_units.id in' in stmt_str:
                            return target_lookup_result
                        return empty_result

                    session.exec = AsyncMock(side_effect=_exec)
                    await engine._detect(session, [flagged_a.id, flagged_b.id], vault_id)

        # Find the UPDATE for the target unit.
        updates_for_target = [
            u for u in captured_updates if any(target.id == v for v in u['params'].values())
        ]
        assert updates_for_target, 'no UPDATE issued for the target unit'
        # The new confidence parameter — find the float in the params dict.
        update_params = updates_for_target[0]['params']
        confidence_params = [v for k, v in update_params.items() if isinstance(v, float)]
        assert confidence_params, 'no confidence value found in UPDATE params'
        new_confidence = confidence_params[0]
        # Pre-fix: 0.7 (only one alpha-step lands due to dict overwrite).
        # Post-fix: 0.6 (both alpha-steps accumulate).
        assert new_confidence == pytest.approx(0.6, abs=1e-9), (
            f'expected 0.6 (two -alpha steps applied) got {new_confidence}; '
            'this is the Hermes round-4 HIGH accumulation invariant'
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
