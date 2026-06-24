"""Real-DB proof that the extraction statement_timeout skip rolls back the aborted
transaction, so the session stays usable (no 25P02 in_failed_sql_transaction).

The unit tests (test_resolve_entities_timeout) use an AsyncMock session and can't
surface 25P02; this drives a REAL Postgres statement_timeout (57014) through
_resolve_entities and asserts a subsequent statement on the same session succeeds.
Without the rollback in the timeout branch this test fails with 25P02.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import text
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
async def test_resolution_timeout_rolls_back_so_session_is_reusable(
    session: AsyncSession,
) -> None:
    async def _cause_real_timeout(sess, *args, **kwargs):
        # SET LOCAL keeps the low timeout transaction-scoped; pg_sleep exceeds it,
        # raising a real 57014 and aborting the transaction.
        await sess.exec(text("SET LOCAL statement_timeout = '80ms'"))
        await sess.exec(text('SELECT pg_sleep(1)'))

    engine = ExtractionEngine.__new__(ExtractionEngine)
    engine.entity_resolver = MagicMock()
    engine.entity_resolver.resolve_entities_batch = AsyncMock(side_effect=_cause_real_timeout)
    engine.entity_resolver.link_units_to_entities_batch = AsyncMock()

    facts = [_make_processed_fact()]
    result = await engine._resolve_entities(session, [str(uuid4())], facts)

    assert result == set()  # gracefully skipped
    # The branch rolled back the aborted txn, so this subsequent statement succeeds
    # instead of failing with 25P02 in_failed_sql_transaction.
    row = (await session.exec(text('SELECT 1'))).first()
    assert row[0] == 1
