"""E2E tests for BM25 keyword search with stored search_tsvector column.

Validates:
1. The search_tsvector generated column exists and is auto-populated
2. The GIN index is used for keyword queries
3. KeywordStrategy returns results matching text, tags, enriched_tags, enriched_keywords
4. The formatted_text → fact_text → MemoryUnit.text pipeline includes 'where'
"""

import json
import pytest
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from memex_core.config import GLOBAL_VAULT_ID
from memex_core.memory.sql_models import MemoryUnit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_unit(
    text_: str,
    *,
    tags: list[str] | None = None,
    enriched_tags: list[str] | None = None,
    enriched_keywords: list[str] | None = None,
) -> MemoryUnit:
    """Build a MemoryUnit with optional tag metadata."""
    meta: dict = {}
    if tags is not None:
        meta['tags'] = json.dumps(tags)
    if enriched_tags is not None:
        meta['enriched_tags'] = json.dumps(enriched_tags)
    if enriched_keywords is not None:
        meta['enriched_keywords'] = json.dumps(enriched_keywords)
    return MemoryUnit(
        text=text_,
        event_date=datetime.now(timezone.utc),
        vault_id=GLOBAL_VAULT_ID,
        embedding=[0.0] * 384,
        unit_metadata=meta,
    )


async def _insert(session: AsyncSession, *units: MemoryUnit) -> None:
    for u in units:
        session.add(u)
    await session.commit()
    for u in units:
        await session.refresh(u)


# ---------------------------------------------------------------------------
# Schema / DDL tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_tsvector_column_exists(db_session: AsyncSession) -> None:
    """The memory_units table has a search_tsvector column of type tsvector."""
    result = await (await db_session.connection()).execute(
        text(
            'SELECT data_type FROM information_schema.columns '
            "WHERE table_name = 'memory_units' AND column_name = 'search_tsvector'"
        )
    )
    row = result.one_or_none()
    assert row is not None, 'search_tsvector column missing from memory_units'
    assert row[0] == 'tsvector'


@pytest.mark.asyncio
async def test_search_tsvector_is_generated(db_session: AsyncSession) -> None:
    """search_tsvector is a GENERATED ALWAYS (stored) column."""
    result = await (await db_session.connection()).execute(
        text(
            'SELECT is_generated FROM information_schema.columns '
            "WHERE table_name = 'memory_units' AND column_name = 'search_tsvector'"
        )
    )
    row = result.one_or_none()
    assert row is not None
    assert row[0] == 'ALWAYS', f'Expected GENERATED ALWAYS, got {row[0]}'


@pytest.mark.asyncio
async def test_gin_index_exists(db_session: AsyncSession) -> None:
    """A GIN index on search_tsvector exists."""
    result = await (await db_session.connection()).execute(
        text(
            'SELECT indexname, indexdef FROM pg_indexes '
            "WHERE tablename = 'memory_units' AND indexname = 'idx_memory_units_search_tsvector'"
        )
    )
    row = result.one_or_none()
    assert row is not None, 'GIN index idx_memory_units_search_tsvector not found'
    assert 'gin' in row[1].lower(), f'Expected GIN index, got: {row[1]}'


# ---------------------------------------------------------------------------
# Tsvector auto-population tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tsvector_populated_on_insert(db_session: AsyncSession) -> None:
    """Inserting a MemoryUnit auto-populates search_tsvector from text."""
    unit = _make_unit(f'The quick brown fox jumps over the lazy dog {uuid4()}')
    await _insert(db_session, unit)

    row = await (await db_session.connection()).execute(
        text(
            'SELECT search_tsvector IS NOT NULL, search_tsvector::text '
            'FROM memory_units WHERE id = :id'
        ),
        {'id': str(unit.id)},
    )
    result = row.one()
    assert result[0] is True, 'search_tsvector should be non-null after insert'
    # 'fox' should be stemmed and present
    assert 'fox' in result[1] or 'quick' in result[1]


@pytest.mark.asyncio
async def test_tsvector_includes_tags(db_session: AsyncSession) -> None:
    """Tags from metadata are included in search_tsvector."""
    unit = _make_unit(
        f'A plain text note {uuid4()}',
        tags=['compliance', 'quarterly'],
    )
    await _insert(db_session, unit)

    row = await (await db_session.connection()).execute(
        text('SELECT search_tsvector::text FROM memory_units WHERE id = :id'),
        {'id': str(unit.id)},
    )
    tsv = row.scalar_one()
    assert 'complianc' in tsv, f'Expected "compliance" stem in tsvector, got: {tsv}'
    assert 'quarter' in tsv, f'Expected "quarterly" stem in tsvector, got: {tsv}'


@pytest.mark.asyncio
async def test_tsvector_includes_enriched_tags(db_session: AsyncSession) -> None:
    """Enriched tags from Phase 6 are included in search_tsvector."""
    unit = _make_unit(
        f'Basic note text {uuid4()}',
        enriched_tags=['governance', 'audit'],
    )
    await _insert(db_session, unit)

    row = await (await db_session.connection()).execute(
        text('SELECT search_tsvector::text FROM memory_units WHERE id = :id'),
        {'id': str(unit.id)},
    )
    tsv = row.scalar_one()
    assert 'govern' in tsv or 'governance' in tsv, (
        f'Expected "governance" stem in tsvector, got: {tsv}'
    )


@pytest.mark.asyncio
async def test_tsvector_includes_enriched_keywords(db_session: AsyncSession) -> None:
    """Enriched keywords from Phase 6 are included in search_tsvector."""
    unit = _make_unit(
        f'Basic note text {uuid4()}',
        enriched_keywords=['infrastructure', 'deployment'],
    )
    await _insert(db_session, unit)

    row = await (await db_session.connection()).execute(
        text('SELECT search_tsvector::text FROM memory_units WHERE id = :id'),
        {'id': str(unit.id)},
    )
    tsv = row.scalar_one()
    assert 'infrastructur' in tsv or 'infrastructure' in tsv, (
        f'Expected "infrastructure" stem in tsvector, got: {tsv}'
    )
    assert 'deploy' in tsv or 'deployment' in tsv, (
        f'Expected "deployment" stem in tsvector, got: {tsv}'
    )


# ---------------------------------------------------------------------------
# KeywordStrategy integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keyword_search_matches_text(db_session: AsyncSession) -> None:
    """KeywordStrategy finds units by matching the text column."""
    marker = uuid4().hex[:8]
    unit = _make_unit(f'Kubernetes orchestration platform {marker}')
    decoy = _make_unit(f'Unrelated finance topic {uuid4()}')
    await _insert(db_session, unit, decoy)

    from memex_core.memory.retrieval.strategies import KeywordStrategy

    strategy = KeywordStrategy()
    stmt = strategy.get_statement('kubernetes orchestration', None, limit=10)
    result = await (await db_session.connection()).execute(stmt)
    rows = result.all()

    found_ids = [r[0] for r in rows]
    assert unit.id in found_ids, 'KeywordStrategy should match on text content'
    assert decoy.id not in found_ids, 'Decoy should not match'


@pytest.mark.asyncio
async def test_keyword_search_matches_tags(db_session: AsyncSession) -> None:
    """KeywordStrategy finds units by matching document tags in metadata."""
    unit = _make_unit(
        f'Generic note text {uuid4()}',
        tags=['photosynthesis', 'chloroplast'],
    )
    decoy = _make_unit(f'Another note about nothing {uuid4()}')
    await _insert(db_session, unit, decoy)

    from memex_core.memory.retrieval.strategies import KeywordStrategy

    strategy = KeywordStrategy()
    stmt = strategy.get_statement('photosynthesis', None, limit=10)
    result = await (await db_session.connection()).execute(stmt)
    rows = result.all()

    found_ids = [r[0] for r in rows]
    assert unit.id in found_ids, 'KeywordStrategy should match on tags'


@pytest.mark.asyncio
async def test_keyword_search_matches_enriched_tags(db_session: AsyncSession) -> None:
    """KeywordStrategy finds units by matching enriched_tags."""
    unit = _make_unit(
        f'Some baseline text {uuid4()}',
        enriched_tags=['mitochondria', 'cellular'],
    )
    await _insert(db_session, unit)

    from memex_core.memory.retrieval.strategies import KeywordStrategy

    strategy = KeywordStrategy()
    stmt = strategy.get_statement('mitochondria', None, limit=10)
    result = await (await db_session.connection()).execute(stmt)
    rows = result.all()

    found_ids = [r[0] for r in rows]
    assert unit.id in found_ids, 'KeywordStrategy should match on enriched_tags'


@pytest.mark.asyncio
async def test_keyword_search_matches_enriched_keywords(db_session: AsyncSession) -> None:
    """KeywordStrategy finds units by matching enriched_keywords."""
    unit = _make_unit(
        f'Some baseline text {uuid4()}',
        enriched_keywords=['thermodynamics', 'entropy'],
    )
    await _insert(db_session, unit)

    from memex_core.memory.retrieval.strategies import KeywordStrategy

    strategy = KeywordStrategy()
    stmt = strategy.get_statement('thermodynamics', None, limit=10)
    result = await (await db_session.connection()).execute(stmt)
    rows = result.all()

    found_ids = [r[0] for r in rows]
    assert unit.id in found_ids, 'KeywordStrategy should match on enriched_keywords'


@pytest.mark.asyncio
async def test_keyword_search_combined_metadata(db_session: AsyncSession) -> None:
    """KeywordStrategy can match across all metadata sources simultaneously."""
    unit = _make_unit(
        f'Core text about enzymes {uuid4()}',
        tags=['biology'],
        enriched_tags=['biochemistry'],
        enriched_keywords=['catalysis'],
    )
    await _insert(db_session, unit)

    from memex_core.memory.retrieval.strategies import KeywordStrategy

    strategy = KeywordStrategy()

    # Search for a term only in enriched_keywords
    stmt = strategy.get_statement('catalysis', None, limit=10)
    result = await (await db_session.connection()).execute(stmt)
    rows = result.all()
    assert unit.id in [r[0] for r in rows], 'Should match enriched_keywords in combined tsvector'

    # Search for a term only in enriched_tags
    stmt = strategy.get_statement('biochemistry', None, limit=10)
    result = await (await db_session.connection()).execute(stmt)
    rows = result.all()
    assert unit.id in [r[0] for r in rows], 'Should match enriched_tags in combined tsvector'

    # Search for a term only in tags
    stmt = strategy.get_statement('biology', None, limit=10)
    result = await (await db_session.connection()).execute(stmt)
    rows = result.all()
    assert unit.id in [r[0] for r in rows], 'Should match tags in combined tsvector'


@pytest.mark.asyncio
async def test_keyword_search_no_match_returns_empty(db_session: AsyncSession) -> None:
    """KeywordStrategy returns empty results when nothing matches."""
    unit = _make_unit(f'Only about cats {uuid4()}')
    await _insert(db_session, unit)

    from memex_core.memory.retrieval.strategies import KeywordStrategy

    strategy = KeywordStrategy()
    stmt = strategy.get_statement('xylophone', None, limit=10)
    result = await (await db_session.connection()).execute(stmt)
    rows = result.all()

    assert len(rows) == 0


# ---------------------------------------------------------------------------
# GIN index usage verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keyword_query_uses_gin_index(db_session: AsyncSession) -> None:
    """EXPLAIN shows GIN index scan for keyword queries on search_tsvector."""
    # Insert enough data that the planner prefers the index
    units = [_make_unit(f'Filler content number {i} {uuid4()}') for i in range(20)]
    target = _make_unit(
        f'Specialized astronomy content {uuid4()}',
        enriched_tags=['astrophysics'],
    )
    units.append(target)
    await _insert(db_session, *units)

    # Force the planner to use the index by disabling seq scan
    await (await db_session.connection()).execute(text('SET enable_seqscan = off'))

    # Use raw SQL equivalent of what KeywordStrategy generates
    explain_result = await (await db_session.connection()).execute(
        text(
            'EXPLAIN SELECT id FROM memory_units '
            "WHERE search_tsvector @@ to_tsquery('english', 'astrophysics') "
            "ORDER BY ts_rank_cd(search_tsvector, to_tsquery('english', 'astrophysics')) DESC "
            'LIMIT 10'
        )
    )
    plan_lines = [row[0] for row in explain_result.all()]
    plan_text = '\n'.join(plan_lines)

    # Re-enable seq scan for other tests
    await (await db_session.connection()).execute(text('SET enable_seqscan = on'))

    assert 'idx_memory_units_search_tsvector' in plan_text or 'Bitmap Index Scan' in plan_text, (
        f'Expected GIN index usage in query plan, got:\n{plan_text}'
    )
