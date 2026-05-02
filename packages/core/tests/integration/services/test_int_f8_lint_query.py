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
