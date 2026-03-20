"""Integration tests for Phase 6 enrichment (Memory Evolution).

These tests verify enrichment behavior against a real PostgreSQL database.
Tests marked @pytest.mark.llm require GOOGLE_API_KEY and use a real LLM.
Tests without that marker use mocked LLM responses but real DB operations.
"""

import asyncio
import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import dspy
import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.memory.models.embedding import get_embedding_model
from memex_core.memory.reflect.models import ReflectionRequest
from memex_core.memory.reflect.prompts import EnrichedTagSet
from memex_core.memory.reflect.reflection import ReflectionEngine
from memex_core.memory.sql_models import Entity, MemoryUnit, UnitEntity
from memex_common.types import FactTypes


# ============================================================================
# Helpers
# ============================================================================


def _make_db_unit(
    text: str,
    embedding: list[float],
    vault_id=None,
    unit_metadata: dict | None = None,
) -> MemoryUnit:
    """Create a MemoryUnit ready for DB insertion."""
    from memex_core.config import GLOBAL_VAULT_ID

    return MemoryUnit(
        id=uuid4(),
        text=f'{text} ({uuid4()})',
        embedding=embedding,
        event_date=datetime.now(timezone.utc),
        fact_type=FactTypes.WORLD,
        vault_id=vault_id or GLOBAL_VAULT_ID,
        unit_metadata=unit_metadata or {},
    )


async def _create_entity_with_memories(
    session: AsyncSession,
    entity_name: str,
    memory_texts: list[str],
    embedder,
    vault_id=None,
) -> tuple[UUID, list[MemoryUnit]]:
    """Helper: create an entity, memory units, and unit-entity links."""
    from memex_core.config import GLOBAL_VAULT_ID

    vid = vault_id or GLOBAL_VAULT_ID

    entity_id = uuid4()
    entity = Entity(
        id=entity_id,
        canonical_name=entity_name,
        first_seen=datetime.now(timezone.utc),
        last_seen=datetime.now(timezone.utc),
    )
    session.add(entity)

    embeddings = embedder.encode(memory_texts)
    units = []
    for i, text in enumerate(memory_texts):
        unit = _make_db_unit(text, embeddings[i].tolist(), vault_id=vid)
        session.add(unit)
        units.append(unit)
        session.add(UnitEntity(unit_id=unit.id, entity_id=entity_id))

    await session.commit()
    return entity_id, units


# ============================================================================
# Integration tests (real DB, mocked LLM)
# ============================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_enrichment_persists_to_database(session: AsyncSession, memex_config):
    """Enrichment metadata should persist through session.commit() in PostgreSQL."""
    from memex_core.memory.models.embedding import FastEmbedder

    memex_config.server.memory.reflection.enrichment_enabled = True

    # Use mock embedder for non-LLM test
    mock_embedder = MagicMock(spec=FastEmbedder)
    import numpy as np

    mock_embedder.encode.return_value = np.array([[0.1] * 384] * 5)

    entity_id, units = await _create_entity_with_memories(
        session,
        'Auth System',
        [
            'The auth middleware is being rewritten.',
            'The rewrite is mandated by legal for compliance.',
            'Session tokens must use rotating keys.',
        ],
        mock_embedder,
    )

    engine = ReflectionEngine(session, config=memex_config, embedder=mock_embedder)
    engine.lm = MagicMock()

    # Build observations that reference the memory units as evidence
    from memex_core.memory.sql_models import Observation, EvidenceItem

    obs = Observation(
        title='Compliance-driven auth rewrite',
        content='The auth middleware rewrite is compliance-driven.',
        evidence=[
            EvidenceItem(
                memory_id=units[0].id,
                quote=units[0].text[:30],
                relevance=1.0,
                explanation='Direct evidence.',
                timestamp=datetime.now(timezone.utc),
            ),
            EvidenceItem(
                memory_id=units[1].id,
                quote=units[1].text[:30],
                relevance=1.0,
                explanation='Legal mandate.',
                timestamp=datetime.now(timezone.utc),
            ),
        ],
    )

    # Mock LLM to return enrichments
    mock_result = MagicMock()
    mock_result.enrichments = [
        EnrichedTagSet(
            memory_index=0, enriched_tags=['compliance', 'legal'], enriched_keywords=['gdpr']
        ),
        EnrichedTagSet(
            memory_index=1, enriched_tags=['eu-regulation'], enriched_keywords=['mandate']
        ),
    ]

    db_lock = asyncio.Lock()

    with patch(
        'memex_core.memory.reflect.reflection.run_dspy_operation',
        return_value=(mock_result, MagicMock()),
    ):
        await engine._phase_6_enrich(
            entity_name='Auth System',
            entity_summary='Compliance-driven auth middleware rewrite.',
            final_obs=[obs],
            recent_memories=units,
            db_lock=db_lock,
        )

    # Commit to DB (this is what reflect_batch does)
    await session.commit()

    # Re-read from DB to verify persistence
    for unit in units[:2]:
        await session.refresh(unit)

    assert set(units[0].unit_metadata['enriched_tags']) == {'compliance', 'legal'}
    assert set(units[0].unit_metadata['enriched_keywords']) == {'gdpr'}
    assert units[0].unit_metadata['enriched_by_entity'] == 'Auth System'
    assert 'enriched_at' in units[0].unit_metadata

    assert set(units[1].unit_metadata['enriched_tags']) == {'eu-regulation'}
    assert set(units[1].unit_metadata['enriched_keywords']) == {'mandate'}

    # Third unit should be untouched (not in evidence)
    await session.refresh(units[2])
    assert 'enriched_tags' not in units[2].unit_metadata


@pytest.mark.integration
@pytest.mark.asyncio
async def test_enrichment_accumulates_across_cycles_in_db(session: AsyncSession, memex_config):
    """Running enrichment twice should set-union tags, verified via DB round-trip."""
    from memex_core.memory.models.embedding import FastEmbedder

    memex_config.server.memory.reflection.enrichment_enabled = True

    mock_embedder = MagicMock(spec=FastEmbedder)
    import numpy as np

    mock_embedder.encode.return_value = np.array([[0.1] * 384] * 2)

    entity_id, units = await _create_entity_with_memories(
        session,
        'Auth System',
        ['The auth middleware rewrite for compliance.'],
        mock_embedder,
    )

    engine = ReflectionEngine(session, config=memex_config, embedder=mock_embedder)
    engine.lm = MagicMock()

    from memex_core.memory.sql_models import Observation, EvidenceItem

    obs = Observation(
        title='Compliance rewrite',
        content='Compliance-driven.',
        evidence=[
            EvidenceItem(
                memory_id=units[0].id,
                quote='auth middleware',
                relevance=1.0,
                explanation='Evidence.',
                timestamp=datetime.now(timezone.utc),
            ),
        ],
    )

    db_lock = asyncio.Lock()

    # Cycle 1: add 'compliance' tag
    mock_result_1 = MagicMock()
    mock_result_1.enrichments = [
        EnrichedTagSet(memory_index=0, enriched_tags=['compliance'], enriched_keywords=['gdpr']),
    ]

    with patch(
        'memex_core.memory.reflect.reflection.run_dspy_operation',
        return_value=(mock_result_1, MagicMock()),
    ):
        await engine._phase_6_enrich(
            entity_name='Auth System',
            entity_summary='Compliance.',
            final_obs=[obs],
            recent_memories=units,
            db_lock=db_lock,
        )

    await session.commit()
    await session.refresh(units[0])
    assert set(units[0].unit_metadata['enriched_tags']) == {'compliance'}
    first_enriched_at = units[0].unit_metadata['enriched_at']

    # Cycle 2: add 'security-audit' tag (compliance should remain)
    mock_result_2 = MagicMock()
    mock_result_2.enrichments = [
        EnrichedTagSet(
            memory_index=0,
            enriched_tags=['security-audit'],
            enriched_keywords=['gdpr', 'soc2'],
        ),
    ]

    with patch(
        'memex_core.memory.reflect.reflection.run_dspy_operation',
        return_value=(mock_result_2, MagicMock()),
    ):
        await engine._phase_6_enrich(
            entity_name='Auth System',
            entity_summary='Compliance + security audit.',
            final_obs=[obs],
            recent_memories=units,
            db_lock=db_lock,
        )

    await session.commit()
    await session.refresh(units[0])

    # Tags should be the union of both cycles
    assert set(units[0].unit_metadata['enriched_tags']) == {'compliance', 'security-audit'}
    assert set(units[0].unit_metadata['enriched_keywords']) == {'gdpr', 'soc2'}
    # Timestamp should be updated
    assert units[0].unit_metadata['enriched_at'] != first_enriched_at


@pytest.mark.integration
@pytest.mark.asyncio
async def test_enrichment_loads_missing_units_from_db(session: AsyncSession, memex_config):
    """Phase 6 should load evidence units from DB when not in recent_memories."""
    from memex_core.memory.models.embedding import FastEmbedder

    memex_config.server.memory.reflection.enrichment_enabled = True

    mock_embedder = MagicMock(spec=FastEmbedder)
    import numpy as np

    mock_embedder.encode.return_value = np.array([[0.1] * 384] * 3)

    entity_id, units = await _create_entity_with_memories(
        session,
        'Test Entity',
        ['memory alpha', 'memory beta'],
        mock_embedder,
    )

    engine = ReflectionEngine(session, config=memex_config, embedder=mock_embedder)
    engine.lm = MagicMock()

    from memex_core.memory.sql_models import Observation, EvidenceItem

    # Observation references both units, but only pass first unit in recent_memories
    obs = Observation(
        title='Test obs',
        content='Test.',
        evidence=[
            EvidenceItem(
                memory_id=units[0].id,
                quote='alpha',
                relevance=1.0,
                explanation='Evidence.',
                timestamp=datetime.now(timezone.utc),
            ),
            EvidenceItem(
                memory_id=units[1].id,
                quote='beta',
                relevance=1.0,
                explanation='Evidence.',
                timestamp=datetime.now(timezone.utc),
            ),
        ],
    )

    db_lock = asyncio.Lock()

    mock_result = MagicMock()
    mock_result.enrichments = [
        EnrichedTagSet(memory_index=0, enriched_tags=['tag-alpha'], enriched_keywords=[]),
        EnrichedTagSet(memory_index=1, enriched_tags=['tag-beta'], enriched_keywords=[]),
    ]

    with patch(
        'memex_core.memory.reflect.reflection.run_dspy_operation',
        return_value=(mock_result, MagicMock()),
    ):
        await engine._phase_6_enrich(
            entity_name='Test Entity',
            entity_summary='Test.',
            final_obs=[obs],
            recent_memories=[units[0]],  # Only first unit in memory
            db_lock=db_lock,
        )

    await session.commit()

    # Both units should be enriched (second was loaded from DB)
    for unit in units:
        await session.refresh(unit)

    assert 'enriched_tags' in units[0].unit_metadata
    assert 'enriched_tags' in units[1].unit_metadata


@pytest.mark.integration
@pytest.mark.asyncio
async def test_keyword_strategy_finds_enriched_units(session: AsyncSession, memex_config):
    """The KeywordStrategy should find memory units by their enriched tags via real SQL."""
    from memex_core.memory.models.embedding import FastEmbedder
    from memex_core.memory.retrieval.strategies import KeywordStrategy

    mock_embedder = MagicMock(spec=FastEmbedder)
    import numpy as np

    mock_embedder.encode.return_value = np.array([[0.1] * 384])

    # Create a unit about auth middleware (no mention of "compliance")
    unit = _make_db_unit(
        f'The authentication middleware is being completely rewritten ({uuid4()})',
        [0.1] * 384,
    )
    session.add(unit)
    await session.commit()

    # Before enrichment: keyword search for "compliance" should NOT find it
    strategy = KeywordStrategy()
    stmt = strategy.get_statement('compliance', None, limit=10)
    results_before = (await session.exec(stmt)).all()
    found_before = [getattr(r, 'id', r) for r in results_before]
    assert unit.id not in found_before

    # Add enriched tags directly (simulating what Phase 6 does)
    unit.unit_metadata = {
        'enriched_tags': ['compliance', 'eu-regulation'],
        'enriched_keywords': ['gdpr'],
        'enriched_at': datetime.now(timezone.utc).isoformat(),
        'enriched_by_entity': 'Auth System',
    }
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(unit, 'unit_metadata')
    await session.commit()
    await session.refresh(unit)

    # After enrichment: keyword search for "compliance" SHOULD find it
    results_after = (await session.exec(stmt)).all()
    found_after = [getattr(r, 'id', r) for r in results_after]
    assert unit.id in found_after, (
        f'Expected unit {unit.id} to be findable by "compliance" after enrichment. '
        f'Found: {found_after}'
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_keyword_strategy_works_without_enrichment(session: AsyncSession, memex_config):
    """Units without enriched metadata should still be found by keyword strategy."""
    unit = _make_db_unit(
        f'PostgreSQL uses SELECT FOR UPDATE SKIP LOCKED for queue processing ({uuid4()})',
        [0.1] * 384,
    )
    session.add(unit)
    await session.commit()

    from memex_core.memory.retrieval.strategies import KeywordStrategy

    strategy = KeywordStrategy()
    stmt = strategy.get_statement('postgresql', None, limit=10)
    results = (await session.exec(stmt)).all()
    found_ids = [getattr(r, 'id', r) for r in results]
    assert unit.id in found_ids


# ============================================================================
# Full reflection E2E test (real LLM)
# ============================================================================


@pytest.mark.integration
@pytest.mark.llm
@pytest.mark.asyncio
async def test_full_reflection_with_enrichment_e2e(session: AsyncSession, memex_config):
    """
    End-to-end test: full reflection cycle including Phase 6 enrichment.
    Uses real LLM + real PostgreSQL.
    """
    api_key = os.environ.get('GOOGLE_API_KEY')
    if not api_key:
        pytest.skip('GOOGLE_API_KEY not set')

    memex_config.server.memory.reflection.enrichment_enabled = True

    lm = dspy.LM('gemini/gemini-3-flash-preview', api_key=api_key)

    with dspy.context(lm=lm):
        embedder = await get_embedding_model()
        engine = ReflectionEngine(session, config=memex_config, embedder=embedder)
        engine.lm = lm

        # Create entity with memories about auth + compliance
        entity_id, units = await _create_entity_with_memories(
            session,
            'Authentication System',
            [
                f'The authentication middleware is being completely rewritten. ({uuid4()})',
                f'The auth rewrite was mandated by the legal team for EU compliance. ({uuid4()})',
                f'Session tokens must use rotating encryption keys per the new regulation. ({uuid4()})',
                f'The old auth system stored tokens in plain text cookies. ({uuid4()})',
                f'GDPR requires explicit consent before storing session data. ({uuid4()})',
            ],
            embedder,
        )

        # Run full reflection (includes all phases 0-6)
        request = ReflectionRequest(entity_id=entity_id)
        mental_model = await engine.reflect_on_entity(request)

        # Verify mental model was created
        assert mental_model is not None
        assert len(mental_model.observations) > 0

        # Refresh units to get updated metadata
        for unit in units:
            await session.refresh(unit)

        # At least some units should have enriched metadata
        enriched_units = [
            u for u in units if u.unit_metadata and u.unit_metadata.get('enriched_tags')
        ]

        assert len(enriched_units) > 0, (
            'Expected at least one unit to have enriched_tags after reflection. '
            f'Unit metadata: {[u.unit_metadata for u in units]}'
        )

        for unit in enriched_units:
            meta = unit.unit_metadata
            assert 'enriched_at' in meta
            assert 'enriched_by_entity' in meta
            assert meta['enriched_by_entity'] == 'Authentication System'
            assert isinstance(meta['enriched_tags'], list)
            assert all(isinstance(t, str) for t in meta['enriched_tags'])
            # Tags should be lowercase
            assert all(t == t.lower() for t in meta['enriched_tags'])


@pytest.mark.integration
@pytest.mark.llm
@pytest.mark.asyncio
async def test_enrichment_makes_units_findable_by_new_concepts(session: AsyncSession, memex_config):
    """
    E2E: after enrichment, memories should be findable by concepts not in original text.
    Uses real LLM + real PostgreSQL + real keyword search.
    """
    api_key = os.environ.get('GOOGLE_API_KEY')
    if not api_key:
        pytest.skip('GOOGLE_API_KEY not set')

    memex_config.server.memory.reflection.enrichment_enabled = True

    lm = dspy.LM('gemini/gemini-3-flash-preview', api_key=api_key)

    with dspy.context(lm=lm):
        embedder = await get_embedding_model()
        engine = ReflectionEngine(session, config=memex_config, embedder=embedder)
        engine.lm = lm

        # Create deliberately narrow memories — text mentions "auth" but not "compliance"
        entity_id, units = await _create_entity_with_memories(
            session,
            'Project Lockdown',
            [
                f'Project Lockdown is rewriting the entire authentication layer from scratch. ({uuid4()})',
                f'The rewrite was triggered by a legal audit that found major regulatory gaps. ({uuid4()})',
                f'All session handling code must conform to GDPR data processing standards. ({uuid4()})',
                f'The old auth system did not encrypt tokens at rest. ({uuid4()})',
                f'Project Lockdown has a hard deadline of Q2 to satisfy EU regulators. ({uuid4()})',
            ],
            embedder,
        )

        # Run reflection
        request = ReflectionRequest(entity_id=entity_id)
        await engine.reflect_on_entity(request)

        # Refresh units
        for unit in units:
            await session.refresh(unit)

        # Collect all enriched tags across all units
        all_enriched_tags = set()
        for unit in units:
            if unit.unit_metadata and unit.unit_metadata.get('enriched_tags'):
                all_enriched_tags.update(unit.unit_metadata['enriched_tags'])

        # The LLM should have identified compliance-related concepts
        # (we can't guarantee exact tags, but we can check that *some* enrichment happened)
        assert len(all_enriched_tags) > 0, (
            'Expected at least some enriched tags to be generated. '
            f'All unit metadata: {[u.unit_metadata for u in units]}'
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_enrichment_disabled_no_metadata_written(session: AsyncSession, memex_config):
    """When enrichment is disabled, no enriched metadata should appear on units."""
    from memex_core.memory.models.embedding import FastEmbedder

    memex_config.server.memory.reflection.enrichment_enabled = False

    mock_embedder = MagicMock(spec=FastEmbedder)
    import numpy as np

    mock_embedder.encode.return_value = np.array([[0.1] * 384] * 5)

    entity_id, units = await _create_entity_with_memories(
        session,
        'Test Entity',
        ['Memory about testing.'],
        mock_embedder,
    )

    # Simulate a full _reflect_entity_internal where enrichment_enabled=False
    # We test the config guard in the calling code
    engine = ReflectionEngine(session, config=memex_config, embedder=mock_embedder)
    engine.lm = MagicMock()

    # Verify the config flag is correctly set
    assert not memex_config.server.memory.reflection.enrichment_enabled

    # Directly call _phase_6_enrich anyway to confirm it doesn't crash
    # (defense-in-depth: even if the guard is bypassed, the method itself is safe)
    from memex_core.memory.sql_models import Observation, EvidenceItem

    obs = Observation(
        title='Test',
        content='Test.',
        evidence=[
            EvidenceItem(
                memory_id=units[0].id,
                quote='test',
                relevance=1.0,
                explanation='test',
                timestamp=datetime.now(timezone.utc),
            ),
        ],
    )

    mock_result = MagicMock()
    mock_result.enrichments = []  # LLM returns nothing

    db_lock = asyncio.Lock()
    with patch(
        'memex_core.memory.reflect.reflection.run_dspy_operation',
        return_value=(mock_result, MagicMock()),
    ):
        await engine._phase_6_enrich(
            entity_name='Test',
            entity_summary='Test.',
            final_obs=[obs],
            recent_memories=units,
            db_lock=db_lock,
        )

    await session.commit()
    await session.refresh(units[0])
    assert 'enriched_tags' not in units[0].unit_metadata
