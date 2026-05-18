"""Phase 6 cleanup must not expire orchestrator-session unit attributes.

Regression test for an async-context lazy-load bug: the Phase 6 cleanup loop
used ``self.session.expire(unit)`` to clear the dirty flag set by
``flag_modified(unit, 'unit_metadata')``. ``session.expire`` marks EVERY
loaded attribute as needing reload, so any subsequent attribute access —
e.g. the next entity's reflection reusing an overlapping evidence unit and
reading ``m.is_deprioritized`` (Phase 6 dedup) or ``m.occurred_start``
(Phase 1 → ``formatted_fact_text``) — triggers SA's sync lazy-load path.
That sync path cannot await from an async session and raises
``sqlalchemy.exc.MissingGreenlet``.

The fix is ``set_committed_value(unit, 'unit_metadata', unit.unit_metadata)``
— it clears the dirty bit on the single attribute Phase 6 touched without
expiring anything else.

This test exercises real Postgres so the failure mode is reproducible
(unit tests with mocked sessions cannot trigger the async lazy-load path).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
from uuid import uuid4

import numpy as np
import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.types import FactTypes
from memex_core.memory.reflect.prompts import EnrichedTagSet
from memex_core.memory.reflect.reflection import ReflectionEngine
from memex_core.memory.sql_models import (
    EvidenceItem,
    MemoryUnit,
    Observation,
)

pytestmark = [pytest.mark.integration]


@pytest.fixture
def reflection_engine(session: AsyncSession):
    """ReflectionEngine bound to the real Postgres session fixture."""
    config = MagicMock()
    config.server.memory.reflection.enrichment_enabled = True
    config.server.memory.reflection.model = None
    config.server.memory.extraction.model = None
    embedder = MagicMock()
    embedder.encode.return_value = np.array([[0.1] * 384])
    eng = ReflectionEngine(session=session, config=config, embedder=embedder)
    eng.lm = MagicMock()
    return eng


def _make_unit(vault_id) -> MemoryUnit:
    """Build a MemoryUnit ready to insert. Includes occurred_start (the
    attribute accessed by ``formatted_fact_text`` that previously tripped
    the lazy-load) so the regression assertion is meaningful."""
    return MemoryUnit(
        id=uuid4(),
        text=f'integration-test-fact-{uuid4()}',
        event_date=datetime.now(timezone.utc),
        occurred_start=datetime(2026, 1, 15, tzinfo=timezone.utc),
        fact_type=FactTypes.WORLD,
        unit_metadata={},
        vault_id=vault_id,
        embedding=[0.1] * 384,
    )


def _make_obs_with_evidence(units: list[MemoryUnit]) -> Observation:
    return Observation(
        id=uuid4(),
        title='Integration test observation',
        content='Phase 6 should enrich the evidence units without expiring '
        'their loaded attributes.',
        evidence=[EvidenceItem(memory_id=u.id) for u in units],
    )


@pytest.mark.asyncio
async def test_phase_6_cleanup_does_not_expire_unit_attributes(
    session: AsyncSession,
    reflection_engine: ReflectionEngine,
):
    """End-to-end: after Phase 6 commits, the orchestrator-session units must
    still serve attribute reads without triggering an async lazy-load.

    With the old ``session.expire(unit)`` cleanup, reading
    ``unit.occurred_start`` or ``unit.is_deprioritized`` after Phase 6
    returns would raise ``MissingGreenlet`` because the attributes were
    marked expired and the access tries a sync SELECT from async code.
    """
    from memex_common.config import GLOBAL_VAULT_ID

    # Two units belonging to two "entities" that share evidence — the
    # cross-entity reuse pattern that originally exposed the bug.
    unit_a = _make_unit(GLOBAL_VAULT_ID)
    unit_b = _make_unit(GLOBAL_VAULT_ID)
    session.add(unit_a)
    session.add(unit_b)
    await session.commit()

    obs = _make_obs_with_evidence([unit_a, unit_b])

    enrichments = MagicMock()
    enrichments.enrichments = [
        EnrichedTagSet(memory_index=0, enriched_tags=['alpha'], enriched_keywords=['k1']),
        EnrichedTagSet(memory_index=1, enriched_tags=['beta'], enriched_keywords=['k2']),
    ]

    with patch(
        'memex_core.memory.reflect.reflection.run_dspy_operation',
        return_value=enrichments,
    ):
        await reflection_engine._phase_6_enrich(
            entity_name='IntegrationTestEntity',
            entity_summary='Pin the no-expire contract.',
            final_obs=[obs],
            recent_memories=[unit_a, unit_b],
        )

    # The actual regression assertion: every attribute access below would
    # raise ``sqlalchemy.exc.MissingGreenlet`` under the old
    # ``session.expire(unit)`` cleanup. Each attribute is one that the
    # downstream reflection phases reach for.
    assert unit_a.occurred_start == datetime(2026, 1, 15, tzinfo=timezone.utc)
    assert unit_b.occurred_start == datetime(2026, 1, 15, tzinfo=timezone.utc)
    assert unit_a.is_deprioritized is False
    assert unit_b.is_deprioritized is False
    assert unit_a.text.startswith('integration-test-fact-')
    assert unit_b.event_date is not None

    # Enrichments actually landed in the DB (sanity — proves the UPDATE ran).
    await session.refresh(unit_a)
    await session.refresh(unit_b)
    assert 'alpha' in unit_a.unit_metadata['enriched_tags']
    assert 'beta' in unit_b.unit_metadata['enriched_tags']
