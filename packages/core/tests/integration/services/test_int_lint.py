"""F6 — LintService integration tests (TC-20-3).

Three cases:

  * ``test_run_rules_emits_findings_and_summary`` — happy path: seed all four
    rules with crafted-fires fixtures, run, assert (a) per-rule
    ``findings_emitted`` matches the seeded rows, (b) summary.total_findings
    is the sum, (c) every finding lands in ``maintenance_proposals`` with
    ``status='pending'`` and ``source='rule'``.
  * ``test_run_rules_is_idempotent_on_rerun`` — running twice produces no
    duplicates (partial unique index ``uq_maintenance_proposals_pending``
    catches re-inserts; second run reports 0 findings emitted).
  * ``test_run_rules_is_read_only_on_inspected_tables`` — captures every SQL
    statement issued during ``run_rules`` via SQLAlchemy
    ``before_cursor_execute``; asserts no INSERT/UPDATE/DELETE against any
    table except ``maintenance_proposals``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, text
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.types import FactTypes
from memex_core.memory.sql_models import (
    Entity,
    MemoryUnit,
    MentalModel,
    Note,
    UnitEntity,
    Vault,
)


pytestmark = [pytest.mark.integration]


# ---------------------------------------------------------------------------
# Multi-rule fixture: seeds one fires-case for every v1 rule in one vault
# ---------------------------------------------------------------------------


async def _seed_all_rules_fire(session: AsyncSession) -> tuple[UUID, dict[str, str]]:
    """Seed each of the 4 v1 rules with exactly one fires-case in one vault.

    Returns: (vault_id, expected_targets) where expected_targets maps
    rule_name → target_id of the seeded row that should fire.
    """
    vault = Vault(name=f'F6-int-{uuid4().hex[:8]}')
    session.add(vault)
    await session.commit()
    await session.refresh(vault)

    note = Note(
        id=uuid4(),
        vault_id=vault.id,
        content_hash=f'hash-{uuid4().hex[:8]}',
        original_text='seed',
    )
    session.add(note)
    await session.commit()

    # 1) orphan_mental_model — old MM with no active linked unit
    orphan_ent = Entity(canonical_name=f'Orphaned-{uuid4().hex[:6]}')
    session.add(orphan_ent)
    await session.commit()
    await session.refresh(orphan_ent)
    orphan_mm = MentalModel(
        id=uuid4(),
        vault_id=vault.id,
        entity_id=orphan_ent.id,
        name=orphan_ent.canonical_name,
        observations=[],
    )
    session.add(orphan_mm)
    await session.commit()
    await session.execute(
        text('UPDATE mental_models SET last_refreshed = :ts WHERE id = :id'),
        {
            'ts': datetime.now(timezone.utc) - timedelta(days=45),
            'id': str(orphan_mm.id),
        },
    )
    await session.commit()

    # 2) cold_low_mw_unit — 6 outcomes, MW=0.25, idle 45d
    cold_unit = MemoryUnit(
        id=uuid4(),
        vault_id=vault.id,
        note_id=note.id,
        text='cold unit',
        fact_type=FactTypes.WORLD,
        status='active',
        is_deprioritized=False,
        risk_class='none',
        success_co_count=1,
        failure_co_count=5,
        event_date=datetime.now(timezone.utc),
        embedding=[0.1] * 384,
    )
    session.add(cold_unit)
    await session.commit()
    await session.execute(
        text('UPDATE memory_units SET updated_at = :ts WHERE id = :id'),
        {
            'ts': datetime.now(timezone.utc) - timedelta(days=45),
            'id': str(cold_unit.id),
        },
    )
    await session.commit()

    # 3) sensitive_unreviewed_unit — sensitive, never reviewed
    sensitive_unit = MemoryUnit(
        id=uuid4(),
        vault_id=vault.id,
        note_id=note.id,
        text='sensitive unit',
        fact_type=FactTypes.WORLD,
        status='active',
        is_deprioritized=False,
        risk_class='sensitive',
        event_date=datetime.now(timezone.utc),
        embedding=[0.1] * 384,
    )
    session.add(sensitive_unit)
    await session.commit()

    # 4) dangling_entity_ref_in_unit — drop FK, delete entity, restore FK NOT VALID
    dangling_unit = MemoryUnit(
        id=uuid4(),
        vault_id=vault.id,
        note_id=note.id,
        text='dangling unit',
        fact_type=FactTypes.WORLD,
        status='active',
        is_deprioritized=False,
        risk_class='none',
        event_date=datetime.now(timezone.utc),
        embedding=[0.1] * 384,
    )
    session.add(dangling_unit)
    await session.commit()
    dangling_ent = Entity(canonical_name=f'Dangling-{uuid4().hex[:6]}')
    session.add(dangling_ent)
    await session.commit()
    await session.refresh(dangling_ent)
    ue = UnitEntity(
        unit_id=dangling_unit.id,
        entity_id=dangling_ent.id,
        vault_id=vault.id,
    )
    session.add(ue)
    await session.commit()
    await session.execute(
        text('ALTER TABLE unit_entities DROP CONSTRAINT unit_entities_entity_id_fkey')
    )
    try:
        await session.execute(
            text('DELETE FROM entities WHERE id = :id'), {'id': str(dangling_ent.id)}
        )
        await session.commit()
    finally:
        # Always restore the FK so a mid-fixture failure can't leave the
        # schema with a dropped constraint that bleeds into other tests.
        # NOT VALID is required because the dangling row deliberately
        # remains for the lint rule to detect.
        # Roll back first: if the DELETE above raised, SQLAlchemy puts the
        # session in a "needs rollback" state and the subsequent ALTER would
        # itself raise, silently dropping the FK-restore and bleeding schema
        # corruption into other tests.
        await session.rollback()
        await session.execute(
            text(
                'ALTER TABLE unit_entities ADD CONSTRAINT unit_entities_entity_id_fkey '
                'FOREIGN KEY (entity_id) REFERENCES entities(id) ON DELETE CASCADE '
                'NOT VALID'
            )
        )
        await session.commit()

    targets = {
        'orphan_mental_model': str(orphan_mm.id),
        'cold_low_mw_unit': str(cold_unit.id),
        'sensitive_unreviewed_unit': str(sensitive_unit.id),
        'dangling_entity_ref_in_unit': str(dangling_unit.id),
    }
    return vault.id, targets


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_rules_emits_findings_and_summary(session: AsyncSession, api) -> None:
    """All 4 v1 rules each emit exactly one finding; summary totals match."""
    vault_id, expected_targets = await _seed_all_rules_fire(session)

    summary = await api.lint.run_rules(vault_id)

    assert summary.vault_id == vault_id
    assert summary.total_findings == 4

    by_rule = {r.rule_name: r for r in summary.rules}
    assert set(by_rule.keys()) == {
        'orphan_mental_model',
        'cold_low_mw_unit',
        'sensitive_unreviewed_unit',
        'dangling_entity_ref_in_unit',
    }
    for rule_name, expected_target in expected_targets.items():
        assert by_rule[rule_name].findings_emitted == 1, f'rule {rule_name} did not emit'

    # Verify ledger contents.
    rows = await session.execute(
        text(
            'SELECT rule_name, target_id, status, source, lint_type '
            'FROM maintenance_proposals WHERE vault_id = :v'
        ),
        {'v': str(vault_id)},
    )
    by_rule_row = {row['rule_name']: dict(row) for row in rows.mappings().all()}
    assert len(by_rule_row) == 4
    for rule_name, expected_target in expected_targets.items():
        row = by_rule_row[rule_name]
        assert row['target_id'] == expected_target
        assert row['status'] == 'pending'
        assert row['source'] == 'rule'


@pytest.mark.asyncio
async def test_run_rules_is_idempotent_on_rerun(session: AsyncSession, api) -> None:
    """Re-running against the same fixture produces no new rows."""
    vault_id, _ = await _seed_all_rules_fire(session)

    first = await api.lint.run_rules(vault_id)
    assert first.total_findings == 4

    second = await api.lint.run_rules(vault_id)
    assert second.total_findings == 0, 'Second run should hit ON CONFLICT DO NOTHING for every row'

    # Ledger still has exactly 4 rows.
    count = (
        await session.execute(
            text('SELECT count(*) FROM maintenance_proposals WHERE vault_id = :v'),
            {'v': str(vault_id)},
        )
    ).scalar()
    assert count == 4


@pytest.mark.asyncio
async def test_run_rules_is_read_only_on_inspected_tables(
    session: AsyncSession, api, metastore
) -> None:
    """Capture every SQL statement issued during ``run_rules`` and assert no
    INSERT/UPDATE/DELETE against tables other than ``maintenance_proposals``.

    Rationale (RFC-003): rules are read-only against the resources they
    inspect; the only writes are to the ledger itself.
    """
    vault_id, _ = await _seed_all_rules_fire(session)

    captured: list[str] = []

    def _before_cursor(conn, cursor, statement, parameters, context, executemany):
        captured.append(statement)

    sync_engine = metastore.engine.sync_engine
    event.listen(sync_engine, 'before_cursor_execute', _before_cursor)
    try:
        await api.lint.run_rules(vault_id)
    finally:
        event.remove(sync_engine, 'before_cursor_execute', _before_cursor)

    write_violations: list[str] = []
    for stmt in captured:
        normalised = ' '.join(stmt.strip().split())
        head = normalised.upper()
        if head.startswith(('INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'COPY')):
            if 'maintenance_proposals' not in normalised.lower():
                write_violations.append(normalised)

    assert not write_violations, (
        'LintService.run_rules wrote to a non-ledger table:\n  ' + '\n  '.join(write_violations)
    )


@pytest.mark.asyncio
async def test_count_pending_global_excludes_vault_scoped_rows(session: AsyncSession, api) -> None:
    """``count_pending(None)`` returns ONLY ``vault_id IS NULL`` pending rows.

    Regression: previously, the ``vault_id is None`` branch counted EVERY
    pending row (global + per-vault), which broke ``scope='global'`` on
    ``GET /api/v1/lint/status``.
    """
    vault_id, _ = await _seed_all_rules_fire(session)

    # Run the rules so we have some vault-scoped pending rows.
    summary = await api.lint.run_rules(vault_id)
    assert summary.total_findings == 4

    # Insert one global (vault_id IS NULL) pending finding directly.
    await session.execute(
        text(
            'INSERT INTO maintenance_proposals '
            '(vault_id, lint_type, target_type, target_id, rule_name, '
            ' evidence, suggested_action, status, source) '
            "VALUES (NULL, 'structural', 'memory_unit', :tid, 'global_test_rule', "
            "       CAST('{}' AS jsonb), 'noop', 'pending', 'rule')"
        ),
        {'tid': str(uuid4())},
    )
    await session.commit()

    # Global count should be exactly 1 (only the NULL-vault row).
    global_count = await api.lint.count_pending(None)
    assert global_count == 1, (
        f'count_pending(None) returned {global_count}; global scope must exclude vault-scoped rows'
    )

    # Vault count should be exactly 4 (the seeded rules; the global row excluded).
    vault_count = await api.lint.count_pending(vault_id)
    assert vault_count == 4


@pytest.mark.asyncio
async def test_run_rules_records_failed_rule_in_summary(session: AsyncSession, api) -> None:
    """A rule whose execution raises must appear in the summary with ``error``
    set, so callers can distinguish "rule ran, no findings" from "rule did
    not run". Surviving rules still emit their findings normally.
    """
    vault_id, _ = await _seed_all_rules_fire(session)

    real_run_one = api.lint._run_one
    target_rule = 'cold_low_mw_unit'

    async def _flaky_run_one(session, spec, v):
        if spec.name == target_rule:
            raise RuntimeError('synthetic rule failure')
        return await real_run_one(session, spec, v)

    with patch.object(api.lint, '_run_one', side_effect=_flaky_run_one):
        summary = await api.lint.run_rules(vault_id)

    by_rule = {r.rule_name: r for r in summary.rules}
    assert target_rule in by_rule, 'failed rule must still appear in summary.rules'
    failed = by_rule[target_rule]
    assert failed.error is not None
    assert 'synthetic rule failure' in failed.error
    assert failed.findings_emitted == 0

    # Surviving rules still recorded normally, with no error.
    for name in (
        'orphan_mental_model',
        'sensitive_unreviewed_unit',
        'dangling_entity_ref_in_unit',
    ):
        assert name in by_rule, f'rule {name} should still have run'
        assert by_rule[name].error is None
        assert by_rule[name].findings_emitted == 1

    # Failed rule's SAVEPOINT rolled back: no ledger row for cold_low_mw_unit.
    rows = (
        (
            await session.execute(
                text('SELECT rule_name FROM maintenance_proposals WHERE vault_id = :v'),
                {'v': str(vault_id)},
            )
        )
        .mappings()
        .all()
    )
    rule_names = {row['rule_name'] for row in rows}
    assert target_rule not in rule_names
