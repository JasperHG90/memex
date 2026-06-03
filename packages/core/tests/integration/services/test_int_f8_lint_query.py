"""F8 — LintService.get_findings integration tests.

Covers:
  * TC-21-1 — filter composition (vault, lint_type, target_type, status)
  * TC-21-2 — read-only assertion (no INSERT/UPDATE/DELETE on
    maintenance_proposals via before_cursor_execute hook, AC-F8-3)
  * TC-21-4 — cursor pagination ends with next_cursor=null on the final page
  * TC-21-6 — DTO surfaces every field documented in AC-F8-1
  * TC-21-7 — AC-F8-5: missing maintenance_proposals raises
    LintSubsystemNotInitializedError
  * TC-21-8 — AC-X-7 vault scoping
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, text
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.memory.sql_models import Vault
from memex_core.services.lint import (
    LintFindingsPage,
    LintSubsystemNotInitializedError,
)


pytestmark = [pytest.mark.integration]


async def _make_vault(session: AsyncSession) -> UUID:
    v = Vault(name=f'F8-int-{uuid4().hex[:8]}')
    session.add(v)
    await session.commit()
    await session.refresh(v)
    return v.id


async def _seed_finding(
    session: AsyncSession,
    *,
    vault_id: UUID | None,
    lint_type: str = 'quality',
    target_type: str = 'memory_unit',
    status: str = 'pending',
    rule_name: str | None = None,
    target_id: str | None = None,
    created_at: datetime | None = None,
) -> UUID:
    """Insert a maintenance_proposals row directly — bypasses LintService rules."""
    finding_id = uuid4()
    rule_name = rule_name or f'rule-{uuid4().hex[:6]}'
    target_id = target_id or str(uuid4())
    created_at = created_at or datetime.now(timezone.utc)
    await session.execute(
        text(
            'INSERT INTO maintenance_proposals '
            '(id, vault_id, lint_type, target_type, target_id, rule_name, '
            ' evidence, suggested_action, status, source, created_at) '
            'VALUES (:id, CAST(:vault_id AS uuid), :lint_type, :target_type, '
            ' :target_id, :rule_name, CAST(:evidence AS jsonb), :sa, '
            ' :status, :source, :created_at)'
        ),
        {
            'id': str(finding_id),
            'vault_id': str(vault_id) if vault_id else None,
            'lint_type': lint_type,
            'target_type': target_type,
            'target_id': target_id,
            'rule_name': rule_name,
            'evidence': '{}',
            'sa': 'review',
            'status': status,
            'source': 'rule',
            'created_at': created_at,
        },
    )
    await session.commit()
    return finding_id


# ---------------------------------------------------------------------------
# TC-21-1 — filter composition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_filters_returns_all_pending_in_default_vault(session: AsyncSession, api) -> None:
    vault_id = await _make_vault(session)
    fids = [
        await _seed_finding(session, vault_id=vault_id, lint_type='quality'),
        await _seed_finding(session, vault_id=vault_id, lint_type='structural'),
        await _seed_finding(session, vault_id=vault_id, lint_type='governance'),
    ]
    page = await api.lint.get_findings(vault_id=vault_id)
    returned = {f.finding_id for f in page.findings}
    assert returned == set(fids)
    assert page.next_cursor is None


@pytest.mark.asyncio
async def test_lint_type_filter_narrows_results(session: AsyncSession, api) -> None:
    vault_id = await _make_vault(session)
    quality_id = await _seed_finding(session, vault_id=vault_id, lint_type='quality')
    await _seed_finding(session, vault_id=vault_id, lint_type='structural')

    page = await api.lint.get_findings(vault_id=vault_id, lint_type='quality')
    assert {f.finding_id for f in page.findings} == {quality_id}


@pytest.mark.asyncio
async def test_status_filter_returns_resolved(session: AsyncSession, api) -> None:
    vault_id = await _make_vault(session)
    resolved_id = await _seed_finding(session, vault_id=vault_id, status='resolved')
    await _seed_finding(session, vault_id=vault_id, status='pending')

    page = await api.lint.get_findings(vault_id=vault_id, status='resolved')
    assert {f.finding_id for f in page.findings} == {resolved_id}


@pytest.mark.asyncio
async def test_target_type_filter(session: AsyncSession, api) -> None:
    vault_id = await _make_vault(session)
    mu_id = await _seed_finding(session, vault_id=vault_id, target_type='memory_unit')
    await _seed_finding(session, vault_id=vault_id, target_type='mental_model')

    page = await api.lint.get_findings(vault_id=vault_id, target_type='memory_unit')
    assert {f.finding_id for f in page.findings} == {mu_id}


@pytest.mark.asyncio
async def test_invalid_status_raises(session: AsyncSession, api) -> None:
    vault_id = await _make_vault(session)
    with pytest.raises(ValueError, match='status must be one of'):
        await api.lint.get_findings(vault_id=vault_id, status='garbage')


@pytest.mark.asyncio
async def test_findings_endpoint_resolves_target_label_per_type(session: AsyncSession, api) -> None:
    """The /lint/findings SELECT resolves a human-readable label for each
    target_type — exercises the correlated-subquery enrichment against real
    Postgres (otherwise a column typo would ship undetected)."""
    from uuid import uuid4 as _uuid4

    from memex_core.memory.sql_models import Entity, MemoryUnit, MentalModel, Note
    from memex_core.server.lint import lint_findings

    vault_id = await _make_vault(session)

    note = Note(
        id=_uuid4(), vault_id=vault_id, title='Quarterly Planning', content_hash=uuid4().hex
    )
    session.add(note)
    await session.commit()
    unit = MemoryUnit(
        id=_uuid4(),
        vault_id=vault_id,
        note_id=note.id,
        text='The release captain signs off on staging.',
        event_date=datetime.now(timezone.utc),
    )
    entity = Entity(id=_uuid4(), canonical_name='Marc de Haas', phonetic_code='MRTKS')
    session.add_all([unit, entity])
    await session.commit()
    mm = MentalModel(
        id=_uuid4(), vault_id=vault_id, entity_id=entity.id, name='Marc de Haas', observations=[]
    )
    session.add(mm)
    await session.commit()

    unit_fid = await _seed_finding(
        session, vault_id=vault_id, target_type='memory_unit', target_id=str(unit.id)
    )
    mm_fid = await _seed_finding(
        session, vault_id=vault_id, target_type='mental_model', target_id=str(mm.id)
    )
    ent_fid = await _seed_finding(
        session, vault_id=vault_id, target_type='entity', target_id=str(entity.id)
    )

    # Pass every param explicitly: FastAPI Query(...) defaults are sentinel
    # objects, not real values, when the route function is called directly.
    # vault_id=None skips the per-vault auth gate; we filter to our ids below.
    payload = await lint_findings(
        api=api,
        vault_id=None,
        lint_type=None,
        status='pending',
        flagged=None,
        limit=500,
        offset=0,
        auth=None,
    )
    by_id = {f['id']: f for f in payload['findings']}

    assert by_id[str(unit_fid)]['target_label'] == 'Quarterly Planning'
    assert by_id[str(unit_fid)]['target_text'] == 'The release captain signs off on staging.'
    assert by_id[str(mm_fid)]['target_label'] == 'Marc de Haas'
    assert by_id[str(ent_fid)]['target_label'] == 'Marc de Haas'

    # Same enrichment must flow through the service DTO path (the MCP
    # `memex_get_lint_flags` agent surface), not just the HTTP endpoint.
    page = await api.lint.get_findings(vault_id=vault_id)
    dto_by_id = {f.finding_id: f for f in page.findings}
    assert dto_by_id[unit_fid].target_label == 'Quarterly Planning'
    assert dto_by_id[unit_fid].target_text == 'The release captain signs off on staging.'
    assert dto_by_id[mm_fid].target_label == 'Marc de Haas'
    assert dto_by_id[ent_fid].target_label == 'Marc de Haas'


@pytest.mark.asyncio
async def test_llm_deferred_rows_excluded_from_findings(session: AsyncSession, api) -> None:
    """``llm_deferred`` bookkeeping rows must not surface in the review list.

    They are internal cost-cap deferrals (retried by ``process_deferred``);
    only genuine findings are operator-actionable.
    """
    vault_id = await _make_vault(session)
    real_id = await _seed_finding(session, vault_id=vault_id, rule_name='cold_low_mw_unit')
    await _seed_finding(session, vault_id=vault_id, rule_name='llm_deferred')

    page = await api.lint.get_findings(vault_id=vault_id)
    returned = {f.finding_id for f in page.findings}
    assert returned == {real_id}

    # The deferred row is also excluded from the pending count.
    assert await api.lint.count_pending(vault_id=vault_id) == 1


# ---------------------------------------------------------------------------
# TC-21-6 — DTO surfaces every documented field
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_finding_dto_surfaces_all_documented_fields(session: AsyncSession, api) -> None:
    vault_id = await _make_vault(session)
    finding_id = await _seed_finding(
        session,
        vault_id=vault_id,
        lint_type='governance',
        target_type='memory_unit',
        rule_name='sensitive_unreviewed_unit',
        target_id='target-abc',
    )
    page = await api.lint.get_findings(vault_id=vault_id)
    assert len(page.findings) == 1
    f = page.findings[0]
    assert f.finding_id == finding_id
    assert f.target_id == 'target-abc'
    assert f.target_type == 'memory_unit'
    assert f.lint_type == 'governance'
    assert f.rule_name == 'sensitive_unreviewed_unit'
    assert f.evidence == {}
    assert f.suggested_action == 'review'
    assert f.status == 'pending'
    assert f.source == 'rule'
    assert f.vault_id == vault_id
    assert f.created_at is not None
    assert f.resolved_at is None
    assert f.resolved_by is None


# ---------------------------------------------------------------------------
# Issue #34 — resolved_by column population on set_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_status_populates_resolved_by_with_actor(
    session: AsyncSession,
    api,
) -> None:
    """A pending finding flipped to ``resolved`` records the actor in resolved_by."""
    vault_id = await _make_vault(session)
    finding_id = await _seed_finding(session, vault_id=vault_id)

    pending = await api.lint.get_findings(vault_id=vault_id, status='pending')
    assert len(pending.findings) == 1
    assert pending.findings[0].resolved_by is None

    flipped = await api.lint.set_status(finding_id, 'resolved', actor='agent:claude')
    assert flipped is True

    resolved = await api.lint.get_findings(vault_id=vault_id, status='resolved')
    assert len(resolved.findings) == 1
    f = resolved.findings[0]
    assert f.finding_id == finding_id
    assert f.status == 'resolved'
    assert f.resolved_at is not None
    assert f.resolved_by == 'agent:claude'


@pytest.mark.asyncio
async def test_set_status_without_actor_leaves_resolved_by_null(
    session: AsyncSession,
    api,
) -> None:
    """When no actor is supplied the column stays NULL (back-compat)."""
    vault_id = await _make_vault(session)
    finding_id = await _seed_finding(session, vault_id=vault_id)

    flipped = await api.lint.set_status(finding_id, 'dismissed')
    assert flipped is True

    page = await api.lint.get_findings(vault_id=vault_id, status='dismissed')
    assert len(page.findings) == 1
    f = page.findings[0]
    assert f.finding_id == finding_id
    assert f.status == 'dismissed'
    assert f.resolved_at is not None
    assert f.resolved_by is None


# ---------------------------------------------------------------------------
# TC-21-4 — cursor pagination
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pagination_cursor_walks_to_last_page(session: AsyncSession, api) -> None:
    """25 findings, limit=10 — three pages: 10, 10, 5; final next_cursor is None."""
    vault_id = await _make_vault(session)
    base_ts = datetime.now(timezone.utc) - timedelta(days=1)
    seeded: list[UUID] = []
    for i in range(25):
        # Distinct created_at so cursor ordering is deterministic.
        seeded.append(
            await _seed_finding(
                session,
                vault_id=vault_id,
                created_at=base_ts + timedelta(seconds=i),
            )
        )

    seen: list[UUID] = []
    cursor: str | None = None
    pages: list[int] = []
    for _ in range(5):  # bound the loop
        page = await api.lint.get_findings(vault_id=vault_id, limit=10, cursor=cursor)
        pages.append(len(page.findings))
        seen.extend(f.finding_id for f in page.findings)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor

    assert pages == [10, 10, 5]
    assert set(seen) == set(seeded)
    # Last page MUST set next_cursor=None (shape-stable; never missing).
    final_page = await api.lint.get_findings(vault_id=vault_id, limit=10, cursor=cursor)
    assert isinstance(final_page, LintFindingsPage)
    assert final_page.next_cursor is None


@pytest.mark.asyncio
async def test_empty_result_returns_shape_stable_envelope(session: AsyncSession, api) -> None:
    """Empty results NEVER collapse to a bare null or missing key."""
    vault_id = await _make_vault(session)
    page = await api.lint.get_findings(vault_id=vault_id)
    assert isinstance(page, LintFindingsPage)
    assert page.findings == []
    assert page.next_cursor is None


# ---------------------------------------------------------------------------
# TC-21-2 — read-only assertion (AC-F8-3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_findings_does_not_write(session: AsyncSession, api, metastore) -> None:
    """Capture every SQL statement during get_findings; assert no
    INSERT/UPDATE/DELETE/TRUNCATE against any table."""
    vault_id = await _make_vault(session)
    await _seed_finding(session, vault_id=vault_id)

    captured: list[str] = []

    def _before_cursor(conn, cursor, statement, parameters, context, executemany):
        captured.append(statement)

    sync_engine = metastore.engine.sync_engine
    event.listen(sync_engine, 'before_cursor_execute', _before_cursor)
    try:
        await api.lint.get_findings(vault_id=vault_id)
    finally:
        event.remove(sync_engine, 'before_cursor_execute', _before_cursor)

    write_violations = [
        s
        for s in captured
        if ' '.join(s.strip().split())
        .upper()
        .startswith(('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'COPY'))
    ]
    assert not write_violations, 'LintService.get_findings issued write SQL:\n  ' + '\n  '.join(
        write_violations
    )


# ---------------------------------------------------------------------------
# TC-21-8 — vault scoping (AC-X-7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vault_scoping(session: AsyncSession, api) -> None:
    """Findings inserted into vault_a are invisible to vault_b queries."""
    vault_a = await _make_vault(session)
    vault_b = await _make_vault(session)
    a_finding = await _seed_finding(session, vault_id=vault_a)
    await _seed_finding(session, vault_id=vault_b)

    page_a = await api.lint.get_findings(vault_id=vault_a)
    page_b = await api.lint.get_findings(vault_id=vault_b)

    assert {f.finding_id for f in page_a.findings} == {a_finding}
    assert a_finding not in {f.finding_id for f in page_b.findings}


# ---------------------------------------------------------------------------
# TC-21-7 — AC-F8-5 missing-table envelope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_table_raises_initialization_error(
    session: AsyncSession, api, metastore, engine
) -> None:
    """Drop maintenance_proposals; the next get_findings raises the documented error.

    The table is recreated in ``finally`` so downstream tests in the same
    session do not observe the simulated uninitialized state.
    """
    from sqlmodel import SQLModel

    await session.execute(text('DROP TABLE IF EXISTS maintenance_proposals CASCADE'))
    await session.commit()
    try:
        with pytest.raises(LintSubsystemNotInitializedError) as ei:
            await api.lint.get_findings()
        assert 'alembic upgrade head' in str(ei.value)
    finally:
        table = SQLModel.metadata.tables['maintenance_proposals']
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync_conn: table.create(sync_conn, checkfirst=True))
