"""Real-DB proof that an entity-resolution statement_timeout PROPAGATES out of
_resolve_entities rather than being swallowed in place.

This is the data-integrity invariant: because resolution shares the caller's
single ingest transaction (no savepoint), the timeout must propagate so the
caller's AsyncTransaction rolls back the WHOLE note — never a silent half-commit
of facts whose entity links never landed. An earlier attempt rolled back the
shared txn here and then kept writing, discarding the note's facts; this test
guards against that.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.memory.extraction.engine import ExtractionEngine


def _make_processed_fact() -> MagicMock:
    fact = MagicMock()
    fact.entities = [MagicMock(text='TestEntity', entity_type='Concept')]
    fact.occurred_start = None
    fact.mentioned_at = datetime.now(timezone.utc)
    fact.who = None
    fact.where = None
    return fact


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resolution_timeout_propagates(session: AsyncSession) -> None:
    async def _cause_real_timeout(sess, *args, **kwargs):
        await sess.exec(text("SET LOCAL statement_timeout = '80ms'"))
        await sess.exec(text('SELECT pg_sleep(1)'))  # real 57014, aborts the txn

    engine = ExtractionEngine.__new__(ExtractionEngine)
    engine.entity_resolver = MagicMock()
    engine.entity_resolver.resolve_entities_batch = AsyncMock(side_effect=_cause_real_timeout)
    engine.entity_resolver.link_units_to_entities_batch = AsyncMock()

    facts = [_make_processed_fact()]

    # Must propagate (not swallow) — the caller owns the rollback of the whole note.
    with pytest.raises(DBAPIError) as ei:
        await engine._resolve_entities(session, [str(uuid4())], facts)
    assert getattr(ei.value.orig, 'sqlstate', None) == '57014'
    # Linking must not have run after the resolution failure.
    engine.entity_resolver.link_units_to_entities_batch.assert_not_called()
