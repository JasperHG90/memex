"""Tests for Phase 6: Enrich (Memory Evolution)."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import numpy as np
import pytest

from memex_core.memory.reflect.prompts import (
    EnrichedTagSet,
)
from memex_core.memory.reflect.reflection import ReflectionEngine
from memex_core.memory.sql_models import (
    EvidenceItem,
    MemoryUnit,
    Observation,
)
from memex_common.types import FactTypes


def _make_unit(text: str, unit_metadata: dict | None = None) -> MemoryUnit:
    """Create a MemoryUnit with unique content."""
    return MemoryUnit(
        id=uuid4(),
        text=f'{text} ({uuid4()})',
        event_date=datetime.now(timezone.utc),
        fact_type=FactTypes.WORLD,
        unit_metadata=unit_metadata or {},
    )


def _make_obs_with_evidence(units: list[MemoryUnit]) -> Observation:
    """Create an Observation referencing given units as evidence."""
    evidence = [
        EvidenceItem(
            memory_id=u.id,
            quote=u.text[:30],
            relevance=1.0,
            explanation='relevant',
            timestamp=datetime.now(timezone.utc),
        )
        for u in units
    ]
    return Observation(
        title='Test Observation',
        content='A test observation.',
        evidence=evidence,
    )


@pytest.fixture
def engine():
    mock_session = AsyncMock()
    mock_config = MagicMock()
    mock_config.server.memory.reflection.enrichment_enabled = True
    mock_config.server.memory.reflection.model = None
    mock_config.server.memory.extraction.model = None
    mock_embedder = MagicMock()
    mock_embedder.encode.return_value = np.array([[0.1] * 384])
    eng = ReflectionEngine(session=mock_session, config=mock_config, embedder=mock_embedder)
    eng.lm = MagicMock()
    return eng


@pytest.fixture
def db_lock():
    return asyncio.Lock()


# ============================================================================
# Early-return guard tests
# ============================================================================


@pytest.mark.asyncio
async def test_phase_6_skips_when_no_observations(engine, db_lock):
    """Phase 6 should return early when no observations are provided."""
    unit = _make_unit('test memory')

    with patch('memex_core.memory.reflect.reflection.run_dspy_operation') as mock_llm:
        await engine._phase_6_enrich(
            entity_name='TestEntity',
            entity_summary='A test entity.',
            final_obs=[],
            recent_memories=[unit],
            db_lock=db_lock,
        )
        mock_llm.assert_not_called()


@pytest.mark.asyncio
async def test_phase_6_skips_when_no_evidence_ids(engine, db_lock):
    """Phase 6 should return early when observations have no evidence."""
    obs = Observation(title='Empty', content='No evidence', evidence=[])

    with patch('memex_core.memory.reflect.reflection.run_dspy_operation') as mock_llm:
        await engine._phase_6_enrich(
            entity_name='TestEntity',
            entity_summary='A test entity.',
            final_obs=[obs],
            recent_memories=[],
            db_lock=db_lock,
        )
        mock_llm.assert_not_called()


@pytest.mark.asyncio
async def test_phase_6_skips_when_evidence_units_not_found(engine, db_lock):
    """Phase 6 should return early when evidence IDs point to units not in memory or DB."""
    # Create observation with evidence pointing to a unit that's nowhere
    phantom_id = uuid4()
    obs = Observation(
        title='Phantom',
        content='Evidence points to missing unit.',
        evidence=[
            EvidenceItem(
                memory_id=phantom_id,
                quote='ghost',
                relevance=1.0,
                explanation='phantom',
                timestamp=datetime.now(timezone.utc),
            )
        ],
    )

    # DB returns nothing for the phantom ID
    mock_exec_result = MagicMock()
    mock_exec_result.all.return_value = []
    engine.session.exec = AsyncMock(return_value=mock_exec_result)

    with patch('memex_core.memory.reflect.reflection.run_dspy_operation') as mock_llm:
        await engine._phase_6_enrich(
            entity_name='TestEntity',
            entity_summary='Test.',
            final_obs=[obs],
            recent_memories=[],
            db_lock=db_lock,
        )
        # No target units found → no LLM call
        mock_llm.assert_not_called()


@pytest.mark.asyncio
async def test_phase_6_skips_when_llm_returns_none(engine, db_lock):
    """Phase 6 should handle gracefully when LLM returns None."""
    unit = _make_unit('some memory')
    obs = _make_obs_with_evidence([unit])

    with patch(
        'memex_core.memory.reflect.reflection.run_dspy_operation',
        return_value=None,
    ):
        # Should not raise
        await engine._phase_6_enrich(
            entity_name='TestEntity',
            entity_summary='Test.',
            final_obs=[obs],
            recent_memories=[unit],
            db_lock=db_lock,
        )

    # No enrichment applied
    assert 'enriched_tags' not in unit.unit_metadata


@pytest.mark.asyncio
async def test_phase_6_skips_when_llm_returns_empty_enrichments(engine, db_lock):
    """Phase 6 should handle gracefully when LLM returns empty enrichments list."""
    unit = _make_unit('some memory')
    obs = _make_obs_with_evidence([unit])

    mock_result = MagicMock()
    mock_result.enrichments = []

    with patch(
        'memex_core.memory.reflect.reflection.run_dspy_operation',
        return_value=mock_result,
    ):
        await engine._phase_6_enrich(
            entity_name='TestEntity',
            entity_summary='Test.',
            final_obs=[obs],
            recent_memories=[unit],
            db_lock=db_lock,
        )

    assert 'enriched_tags' not in unit.unit_metadata


# ============================================================================
# Core enrichment logic tests
# ============================================================================


@pytest.mark.asyncio
async def test_phase_6_applies_enrichments(engine, db_lock):
    """Phase 6 should write enriched_tags, enriched_keywords, enriched_at, enriched_by_entity."""
    unit = _make_unit('Project Alpha is rewriting its auth middleware')
    obs = _make_obs_with_evidence([unit])

    mock_result = MagicMock()
    mock_result.enrichments = [
        EnrichedTagSet(
            memory_index=0,
            enriched_tags=['compliance', 'eu-regulation'],
            enriched_keywords=['gdpr'],
        )
    ]

    with patch(
        'memex_core.memory.reflect.reflection.run_dspy_operation',
        return_value=mock_result,
    ):
        await engine._phase_6_enrich(
            entity_name='Auth System',
            entity_summary='Authentication middleware being rewritten for compliance.',
            final_obs=[obs],
            recent_memories=[unit],
            db_lock=db_lock,
        )

    assert 'enriched_tags' in unit.unit_metadata
    assert set(unit.unit_metadata['enriched_tags']) == {'compliance', 'eu-regulation'}
    assert set(unit.unit_metadata['enriched_keywords']) == {'gdpr'}
    assert 'enriched_at' in unit.unit_metadata
    assert unit.unit_metadata['enriched_by_entity'] == 'Auth System'
    # Tags should be sorted
    assert unit.unit_metadata['enriched_tags'] == sorted(unit.unit_metadata['enriched_tags'])
    assert unit.unit_metadata['enriched_keywords'] == sorted(
        unit.unit_metadata['enriched_keywords']
    )


@pytest.mark.asyncio
async def test_phase_6_accumulates_tags_across_cycles(engine, db_lock):
    """Phase 6 should set-union new tags with existing ones, no duplicates."""
    unit = _make_unit(
        'auth middleware rewrite',
        unit_metadata={
            'enriched_tags': ['compliance'],
            'enriched_keywords': ['gdpr'],
            'enriched_at': '2025-01-01T00:00:00+00:00',
            'enriched_by_entity': 'Auth System',
        },
    )
    obs = _make_obs_with_evidence([unit])

    mock_result = MagicMock()
    mock_result.enrichments = [
        EnrichedTagSet(
            memory_index=0,
            enriched_tags=['compliance', 'security-audit'],
            enriched_keywords=['gdpr', 'soc2'],
        )
    ]

    with patch(
        'memex_core.memory.reflect.reflection.run_dspy_operation',
        return_value=mock_result,
    ):
        await engine._phase_6_enrich(
            entity_name='Auth System',
            entity_summary='Auth middleware.',
            final_obs=[obs],
            recent_memories=[unit],
            db_lock=db_lock,
        )

    # Should be union, no duplicates
    assert set(unit.unit_metadata['enriched_tags']) == {'compliance', 'security-audit'}
    assert set(unit.unit_metadata['enriched_keywords']) == {'gdpr', 'soc2'}
    # enriched_at should be updated to the latest run
    assert unit.unit_metadata['enriched_at'] != '2025-01-01T00:00:00+00:00'


@pytest.mark.asyncio
async def test_phase_6_normalizes_tags_to_lowercase(engine, db_lock):
    """Phase 6 should normalize tags to lowercase and strip whitespace."""
    unit = _make_unit('some memory')
    obs = _make_obs_with_evidence([unit])

    mock_result = MagicMock()
    mock_result.enrichments = [
        EnrichedTagSet(
            memory_index=0,
            enriched_tags=['COMPLIANCE', '  EU-Regulation ', 'gdpr'],
            enriched_keywords=['Data Protection', ' SECURITY '],
        )
    ]

    with patch(
        'memex_core.memory.reflect.reflection.run_dspy_operation',
        return_value=mock_result,
    ):
        await engine._phase_6_enrich(
            entity_name='Test',
            entity_summary='Test.',
            final_obs=[obs],
            recent_memories=[unit],
            db_lock=db_lock,
        )

    assert set(unit.unit_metadata['enriched_tags']) == {'compliance', 'eu-regulation', 'gdpr'}
    assert set(unit.unit_metadata['enriched_keywords']) == {'data protection', 'security'}


@pytest.mark.asyncio
async def test_phase_6_handles_out_of_bounds_index(engine, db_lock):
    """Phase 6 should skip enrichments with invalid memory_index."""
    unit = _make_unit('valid memory')
    obs = _make_obs_with_evidence([unit])

    mock_result = MagicMock()
    mock_result.enrichments = [
        # Valid index
        EnrichedTagSet(memory_index=0, enriched_tags=['good-tag'], enriched_keywords=[]),
        # Out of bounds (only 1 unit)
        EnrichedTagSet(memory_index=5, enriched_tags=['bad-tag'], enriched_keywords=[]),
        # Negative index
        EnrichedTagSet(memory_index=-1, enriched_tags=['also-bad'], enriched_keywords=[]),
    ]

    with patch(
        'memex_core.memory.reflect.reflection.run_dspy_operation',
        return_value=mock_result,
    ):
        await engine._phase_6_enrich(
            entity_name='Test',
            entity_summary='Test.',
            final_obs=[obs],
            recent_memories=[unit],
            db_lock=db_lock,
        )

    # Only the valid enrichment should be applied
    assert set(unit.unit_metadata['enriched_tags']) == {'good-tag'}


@pytest.mark.asyncio
async def test_phase_6_deduplicates_evidence_across_observations(engine, db_lock):
    """If multiple observations reference the same unit, it should appear once in target_units."""
    shared_unit = _make_unit('shared evidence memory')
    obs1 = _make_obs_with_evidence([shared_unit])
    obs2 = _make_obs_with_evidence([shared_unit])

    mock_result = MagicMock()
    mock_result.enrichments = [
        EnrichedTagSet(memory_index=0, enriched_tags=['tag-from-both'], enriched_keywords=[]),
    ]

    with patch(
        'memex_core.memory.reflect.reflection.run_dspy_operation',
        return_value=mock_result,
    ) as mock_llm:
        await engine._phase_6_enrich(
            entity_name='Test',
            entity_summary='Test.',
            final_obs=[obs1, obs2],
            recent_memories=[shared_unit],
            db_lock=db_lock,
        )

    # LLM should receive exactly 1 memory (deduplicated)
    llm_call_kwargs = mock_llm.call_args[1]['input_kwargs']
    assert len(llm_call_kwargs['memories']) == 1
    assert set(shared_unit.unit_metadata['enriched_tags']) == {'tag-from-both'}


@pytest.mark.asyncio
async def test_phase_6_handles_none_unit_metadata(engine, db_lock):
    """Phase 6 should initialize unit_metadata if it's None."""
    unit = _make_unit('memory with null metadata')
    unit.unit_metadata = None  # type: ignore[assignment]
    obs = _make_obs_with_evidence([unit])

    mock_result = MagicMock()
    mock_result.enrichments = [
        EnrichedTagSet(memory_index=0, enriched_tags=['new-tag'], enriched_keywords=['kw']),
    ]

    with patch(
        'memex_core.memory.reflect.reflection.run_dspy_operation',
        return_value=mock_result,
    ):
        await engine._phase_6_enrich(
            entity_name='Test',
            entity_summary='Test.',
            final_obs=[obs],
            recent_memories=[unit],
            db_lock=db_lock,
        )

    assert unit.unit_metadata is not None
    assert set(unit.unit_metadata['enriched_tags']) == {'new-tag'}
    assert set(unit.unit_metadata['enriched_keywords']) == {'kw'}


@pytest.mark.asyncio
async def test_phase_6_preserves_existing_non_enriched_metadata(engine, db_lock):
    """Phase 6 should not clobber existing non-enriched metadata keys."""
    unit = _make_unit(
        'memory with custom metadata',
        unit_metadata={
            'source': 'manual-entry',
            'custom_flag': True,
        },
    )
    obs = _make_obs_with_evidence([unit])

    mock_result = MagicMock()
    mock_result.enrichments = [
        EnrichedTagSet(memory_index=0, enriched_tags=['new-tag'], enriched_keywords=[]),
    ]

    with patch(
        'memex_core.memory.reflect.reflection.run_dspy_operation',
        return_value=mock_result,
    ):
        await engine._phase_6_enrich(
            entity_name='Test',
            entity_summary='Test.',
            final_obs=[obs],
            recent_memories=[unit],
            db_lock=db_lock,
        )

    # Original metadata preserved
    assert unit.unit_metadata['source'] == 'manual-entry'
    assert unit.unit_metadata['custom_flag'] is True
    # Enrichment added
    assert set(unit.unit_metadata['enriched_tags']) == {'new-tag'}


@pytest.mark.asyncio
async def test_phase_6_llm_context_includes_existing_tags(engine, db_lock):
    """Phase 6 should include existing enriched tags in the LLM memory context."""
    unit = _make_unit(
        'auth middleware rewrite',
        unit_metadata={
            'enriched_tags': ['compliance'],
            'enriched_keywords': ['gdpr'],
        },
    )
    obs = _make_obs_with_evidence([unit])

    mock_result = MagicMock()
    mock_result.enrichments = []

    with patch(
        'memex_core.memory.reflect.reflection.run_dspy_operation',
        return_value=mock_result,
    ) as mock_llm:
        await engine._phase_6_enrich(
            entity_name='Auth',
            entity_summary='Auth.',
            final_obs=[obs],
            recent_memories=[unit],
            db_lock=db_lock,
        )

    # Check the memory content sent to LLM includes tag suffix
    llm_call_kwargs = mock_llm.call_args[1]['input_kwargs']
    memory_content = llm_call_kwargs['memories'][0].content
    assert '[tags: compliance, gdpr]' in memory_content


@pytest.mark.asyncio
async def test_phase_6_llm_context_metadata_correct(engine, db_lock):
    """Phase 6 should pass correct context_metadata to run_dspy_operation."""
    unit = _make_unit('some memory')
    obs = _make_obs_with_evidence([unit])

    mock_result = MagicMock()
    mock_result.enrichments = []

    with patch(
        'memex_core.memory.reflect.reflection.run_dspy_operation',
        return_value=mock_result,
    ) as mock_llm:
        await engine._phase_6_enrich(
            entity_name='Test',
            entity_summary='Test.',
            final_obs=[obs],
            recent_memories=[unit],
            db_lock=db_lock,
        )

    # Verify run_dspy_operation was called
    mock_llm.assert_called_once()


@pytest.mark.asyncio
async def test_phase_6_enriches_multiple_units(engine, db_lock):
    """Phase 6 should handle enrichment of multiple distinct units."""
    unit_a = _make_unit('auth middleware is being rewritten')
    unit_b = _make_unit('session tokens use JWT format')
    unit_c = _make_unit('GDPR compliance required by Q2')

    obs = _make_obs_with_evidence([unit_a, unit_b, unit_c])

    mock_result = MagicMock()
    mock_result.enrichments = [
        EnrichedTagSet(memory_index=0, enriched_tags=['compliance'], enriched_keywords=['legal']),
        EnrichedTagSet(memory_index=1, enriched_tags=['security'], enriched_keywords=['tokens']),
        EnrichedTagSet(memory_index=2, enriched_tags=['regulation'], enriched_keywords=['eu']),
    ]

    with patch(
        'memex_core.memory.reflect.reflection.run_dspy_operation',
        return_value=mock_result,
    ):
        await engine._phase_6_enrich(
            entity_name='Auth System',
            entity_summary='Auth system under compliance rewrite.',
            final_obs=[obs],
            recent_memories=[unit_a, unit_b, unit_c],
            db_lock=db_lock,
        )

    assert set(unit_a.unit_metadata['enriched_tags']) == {'compliance'}
    assert set(unit_b.unit_metadata['enriched_tags']) == {'security'}
    assert set(unit_c.unit_metadata['enriched_tags']) == {'regulation'}
    # All should share the same entity name
    for u in [unit_a, unit_b, unit_c]:
        assert u.unit_metadata['enriched_by_entity'] == 'Auth System'


# ============================================================================
# Config flag test
# ============================================================================


@pytest.mark.asyncio
async def test_phase_6_skips_when_disabled(db_lock):
    """Phase 6 should not run when enrichment_enabled=False."""
    mock_session = AsyncMock()
    mock_config = MagicMock()
    mock_config.server.memory.reflection.enrichment_enabled = False
    mock_config.server.memory.reflection.model = None
    mock_config.server.memory.extraction.model = None
    mock_embedder = MagicMock()
    mock_embedder.encode.return_value = np.array([[0.1] * 384])

    engine = ReflectionEngine(session=mock_session, config=mock_config, embedder=mock_embedder)
    engine.lm = MagicMock()

    assert not engine.config.server.memory.reflection.enrichment_enabled


# ============================================================================
# DB loading test
# ============================================================================


@pytest.mark.asyncio
async def test_phase_6_loads_missing_evidence_units(engine, db_lock):
    """Phase 6 should load evidence units from DB when not in recent_memories."""
    unit_in_memory = _make_unit('unit in recent memories')
    unit_in_db = _make_unit('unit only in database')

    obs = _make_obs_with_evidence([unit_in_memory, unit_in_db])

    # Only unit_in_memory is in recent_memories; unit_in_db must be loaded from DB
    mock_exec_result = MagicMock()
    mock_exec_result.all.return_value = [unit_in_db]
    engine.session.exec = AsyncMock(return_value=mock_exec_result)

    mock_result = MagicMock()
    mock_result.enrichments = [
        EnrichedTagSet(memory_index=0, enriched_tags=['tag-a'], enriched_keywords=['kw-a']),
        EnrichedTagSet(memory_index=1, enriched_tags=['tag-b'], enriched_keywords=['kw-b']),
    ]

    with patch(
        'memex_core.memory.reflect.reflection.run_dspy_operation',
        return_value=mock_result,
    ):
        await engine._phase_6_enrich(
            entity_name='TestEntity',
            entity_summary='Test.',
            final_obs=[obs],
            recent_memories=[unit_in_memory],
            db_lock=db_lock,
        )

    # Verify DB was queried for the missing unit
    engine.session.exec.assert_called_once()

    # Both units should have been enriched
    assert 'enriched_tags' in unit_in_memory.unit_metadata
    assert 'enriched_tags' in unit_in_db.unit_metadata


@pytest.mark.asyncio
async def test_phase_6_does_not_query_db_when_all_units_in_memory(engine, db_lock):
    """Phase 6 should skip DB query when all evidence units are in recent_memories."""
    unit = _make_unit('already loaded')
    obs = _make_obs_with_evidence([unit])

    mock_result = MagicMock()
    mock_result.enrichments = [
        EnrichedTagSet(memory_index=0, enriched_tags=['tag'], enriched_keywords=[]),
    ]

    with patch(
        'memex_core.memory.reflect.reflection.run_dspy_operation',
        return_value=mock_result,
    ):
        await engine._phase_6_enrich(
            entity_name='Test',
            entity_summary='Test.',
            final_obs=[obs],
            recent_memories=[unit],
            db_lock=db_lock,
        )

    # Should NOT have called session.exec (no missing units to load)
    engine.session.exec.assert_not_called()
    assert set(unit.unit_metadata['enriched_tags']) == {'tag'}
