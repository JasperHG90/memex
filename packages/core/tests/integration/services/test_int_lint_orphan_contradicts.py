"""V16 — orphan_contradicts_links_post_stale lint rule focused tests.

Negative-case coverage that the broad ``_seed_all_rules_fire`` fixture
doesn't capture:

  * Active source + active target → rule does NOT fire (the link is
    not orphaned).
  * Drifted-vault rows (any of ``ml.vault_id`` / ``source.vault_id`` /
    ``target.vault_id`` disagreeing with the run vault) are excluded;
    parameterised across the three drift dimensions so each predicate is
    load-bearing.
  * Multiple stale targets from the same source unit collapse into ONE
    proposal (per the partial unique index on
    ``(rule_name, target_type, target_id, vault_id)``); ``evidence``
    aggregates the stale target ids.
  * A non-``contradicts`` link to a stale target (e.g. ``reinforces``)
    does NOT fire — only the ``contradicts`` link_type is in scope.
  * An ``is_deprioritized=true`` target with ``status='active'`` does
    NOT fire — broadening to deprioritized lands with the FSFM
    ``archived`` migration, not here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.types import FactTypes
from memex_core.memory.sql_models import MemoryLink, MemoryUnit, Note, Vault


pytestmark = [pytest.mark.integration]


async def _seed_vault_with_note(session: AsyncSession) -> tuple[Vault, Note]:
    vault = Vault(name=f'V16-orphan-{uuid4().hex[:8]}')
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
    return vault, note


def _make_unit(
    vault_id, note_id, *, status: str = 'active', text_label: str = 'unit'
) -> MemoryUnit:
    return MemoryUnit(
        id=uuid4(),
        vault_id=vault_id,
        note_id=note_id,
        text=text_label,
        fact_type=FactTypes.WORLD,
        status=status,
        is_deprioritized=False,
        risk_class='none',
        event_date=datetime.now(timezone.utc),
        embedding=[0.1] * 384,
    )


@pytest.mark.asyncio
async def test_orphan_contradicts_link_does_not_fire_when_target_active(
    session: AsyncSession, api
) -> None:
    """Active source + active target → no orphan; rule must not fire."""
    vault, note = await _seed_vault_with_note(session)
    source = _make_unit(vault.id, note.id, text_label='source')
    target = _make_unit(vault.id, note.id, status='active', text_label='active target')
    session.add(source)
    session.add(target)
    await session.commit()
    link = MemoryLink(
        link_type='contradicts',
        from_unit_id=source.id,
        to_unit_id=target.id,
        vault_id=vault.id,
    )
    session.add(link)
    await session.commit()

    summary = await api.lint.run_rules(vault.id)
    by_rule = {r.rule_name: r for r in summary.rules}
    assert by_rule['orphan_contradicts_links_post_stale'].findings_emitted == 0


@pytest.mark.asyncio
async def test_orphan_contradicts_does_not_fire_when_target_active_but_deprioritized(
    session: AsyncSession, api
) -> None:
    """The rule's predicate is ``target.status='stale'``. An
    ``is_deprioritized=true`` target with ``status='active'`` (the
    archived-via-FSFM future state) must NOT fire under the current
    predicate; broadening to cover deprioritized targets is a separate
    follow-up that lands with the FSFM ``archived`` migration."""
    vault, note = await _seed_vault_with_note(session)
    source = _make_unit(vault.id, note.id, text_label='source')
    deprioritized_target = _make_unit(
        vault.id, note.id, status='active', text_label='deprioritized target'
    )
    deprioritized_target.is_deprioritized = True
    session.add(source)
    session.add(deprioritized_target)
    await session.commit()
    session.add(
        MemoryLink(
            link_type='contradicts',
            from_unit_id=source.id,
            to_unit_id=deprioritized_target.id,
            vault_id=vault.id,
        )
    )
    await session.commit()

    summary = await api.lint.run_rules(vault.id)
    by_rule = {r.rule_name: r for r in summary.rules}
    assert by_rule['orphan_contradicts_links_post_stale'].findings_emitted == 0


@pytest.mark.parametrize('drift', ['source', 'target', 'link', 'all'])
@pytest.mark.asyncio
async def test_orphan_contradicts_excludes_drifted_cross_vault_units(
    session: AsyncSession, api, drift: str
) -> None:
    """A row where one or more of ``MemoryLink.vault_id``,
    ``source.vault_id``, or ``target.vault_id`` disagrees with the run's
    vault must NOT fire. Parameterised across each independent drift
    dimension so removing any one of the three predicates from the SQL
    must fail at least one case. A control row inside vault_a is also
    seeded so a regression that returned zero findings universally would
    fail the positive-control assert at the end."""
    vault_a, note_a = await _seed_vault_with_note(session)
    vault_b, note_b = await _seed_vault_with_note(session)

    src_vault = vault_b.id if drift in ('source', 'all') else vault_a.id
    src_note = note_b.id if drift in ('source', 'all') else note_a.id
    tgt_vault = vault_b.id if drift in ('target', 'all') else vault_a.id
    tgt_note = note_b.id if drift in ('target', 'all') else note_a.id
    link_vault = vault_b.id if drift == 'link' else vault_a.id

    source = _make_unit(src_vault, src_note, text_label='source')
    target = _make_unit(tgt_vault, tgt_note, status='stale', text_label='stale')
    session.add(source)
    session.add(target)
    await session.commit()
    session.add(
        MemoryLink(
            link_type='contradicts',
            from_unit_id=source.id,
            to_unit_id=target.id,
            vault_id=link_vault,
        )
    )
    control_source = _make_unit(vault_a.id, note_a.id, text_label='control-source')
    control_target = _make_unit(vault_a.id, note_a.id, status='stale', text_label='control-stale')
    session.add(control_source)
    session.add(control_target)
    await session.commit()
    session.add(
        MemoryLink(
            link_type='contradicts',
            from_unit_id=control_source.id,
            to_unit_id=control_target.id,
            vault_id=vault_a.id,
        )
    )
    await session.commit()

    summary = await api.lint.run_rules(vault_a.id)
    by_rule = {r.rule_name: r for r in summary.rules}
    assert by_rule['orphan_contradicts_links_post_stale'].findings_emitted == 1


@pytest.mark.asyncio
async def test_orphan_contradicts_aggregates_multiple_stale_targets_per_source(
    session: AsyncSession, api
) -> None:
    """Two stale targets reached from one source unit collapse into a
    single MaintenanceProposal whose ``evidence.orphan_link_count`` is 2
    and whose ``evidence.stale_target_unit_ids`` lists both."""
    vault, note = await _seed_vault_with_note(session)
    source = _make_unit(vault.id, note.id, text_label='source')
    stale_a = _make_unit(vault.id, note.id, status='stale', text_label='stale-a')
    stale_b = _make_unit(vault.id, note.id, status='stale', text_label='stale-b')
    session.add(source)
    session.add(stale_a)
    session.add(stale_b)
    await session.commit()
    session.add(
        MemoryLink(
            link_type='contradicts',
            from_unit_id=source.id,
            to_unit_id=stale_a.id,
            vault_id=vault.id,
        )
    )
    session.add(
        MemoryLink(
            link_type='contradicts',
            from_unit_id=source.id,
            to_unit_id=stale_b.id,
            vault_id=vault.id,
        )
    )
    await session.commit()

    summary = await api.lint.run_rules(vault.id)
    by_rule = {r.rule_name: r for r in summary.rules}
    assert by_rule['orphan_contradicts_links_post_stale'].findings_emitted == 1

    rows = await session.execute(
        text(
            'SELECT target_id, evidence FROM maintenance_proposals '
            "WHERE rule_name = 'orphan_contradicts_links_post_stale' "
            'AND vault_id = :v'
        ),
        {'v': str(vault.id)},
    )
    row = rows.mappings().one()
    assert row['target_id'] == str(source.id)
    evidence = row['evidence']
    assert evidence['orphan_link_count'] == 2
    assert sorted(evidence['stale_target_unit_ids']) == sorted([str(stale_a.id), str(stale_b.id)])
    oldest = evidence['stale_targets_oldest_updated_at']
    assert isinstance(oldest, str)
    parsed = datetime.fromisoformat(oldest)
    assert parsed.tzinfo is not None


@pytest.mark.asyncio
async def test_orphan_contradicts_ignores_non_contradicts_link_types(
    session: AsyncSession, api
) -> None:
    """A ``reinforces`` link to a stale target must NOT fire — the rule
    is link-type-scoped to ``contradicts``."""
    vault, note = await _seed_vault_with_note(session)
    source = _make_unit(vault.id, note.id, text_label='source')
    stale_target = _make_unit(
        vault.id, note.id, status='stale', text_label='stale reinforces target'
    )
    session.add(source)
    session.add(stale_target)
    await session.commit()
    session.add(
        MemoryLink(
            link_type='reinforces',  # NOT 'contradicts'
            from_unit_id=source.id,
            to_unit_id=stale_target.id,
            vault_id=vault.id,
        )
    )
    await session.commit()

    summary = await api.lint.run_rules(vault.id)
    by_rule = {r.rule_name: r for r in summary.rules}
    assert by_rule['orphan_contradicts_links_post_stale'].findings_emitted == 0
