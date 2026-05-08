"""Integration tests for the FSFM lint rules + auto-deprioritize band.

Exercises the full chain end-to-end against a real Postgres:

1. Seeds memory units + memory_links into one vault to drive each FSFM rule
   to fire (or explicitly NOT fire on a control unit).
2. Runs ``LintService.run_rules`` and asserts the expected proposals
   appear in ``maintenance_proposals`` with non-trivial evidence.
3. Runs ``MemexAPI.auto_deprioritize_after_lint`` and asserts the auto-band
   gates behave as designed:

   - Above threshold + no escalation + no recent restore → deprioritized.
   - Above threshold + escalation rule fired (e.g. high MW dropping) →
     proposal escalated, NOT auto-deprioritized.
   - Above threshold + recent ``memory_restore`` audit → cooldown skips.
   - Below threshold → not deprioritized.
4. Confirms vault scoping (a unit in vault B is never touched while
   processing vault A).
5. Confirms idempotency on rerun (same proposals don't double-up).
6. Verifies the rewritten ``sensitive_unreviewed_unit`` predicate fires on
   sensitive units with no recent governance action and stays silent after
   a fresh ``memory_restore`` audit lands.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.types import FactTypes
from memex_core.memory.sql_models import (
    AuditLog,
    Entity,
    MemoryLink,
    MemoryUnit,
    Note,
    UnitEntity,
    Vault,
)


pytestmark = [pytest.mark.integration]


# ---------------------------------------------------------------------------
# Fixture builders — small helpers that keep the seed code legible.
# ---------------------------------------------------------------------------


async def _make_vault(session: AsyncSession, label: str) -> Vault:
    vault = Vault(name=f'fsfm-{label}-{uuid4().hex[:6]}')
    session.add(vault)
    await session.commit()
    await session.refresh(vault)
    return vault


async def _make_note(session: AsyncSession, vault: Vault) -> Note:
    note = Note(
        id=uuid4(),
        vault_id=vault.id,
        content_hash=f'hash-{uuid4().hex[:8]}',
        original_text='seed',
    )
    session.add(note)
    await session.commit()
    return note


async def _make_unit(
    session: AsyncSession,
    *,
    vault: Vault,
    note: Note,
    text_body: str = 'unit',
    intent_class: str = 'durable',
    risk_class: str = 'none',
    importance: float | None = 0.7,
    stability: float | None = 180.0,
    success: int = 0,
    failure: int = 0,
    confidence: float = 1.0,
    last_outcome_age_days: float | None = None,
) -> MemoryUnit:
    unit = MemoryUnit(
        id=uuid4(),
        vault_id=vault.id,
        note_id=note.id,
        text=text_body,
        fact_type=FactTypes.WORLD,
        status='active',
        is_deprioritized=False,
        intent_class=intent_class,
        risk_class=risk_class,
        importance=importance,
        stability=stability,
        success_co_count=success,
        failure_co_count=failure,
        confidence=confidence,
        confidence_evidence_count=0,
        event_date=datetime.now(timezone.utc),
        embedding=[0.1] * 384,
    )
    session.add(unit)
    await session.commit()
    await session.refresh(unit)
    if last_outcome_age_days is not None:
        await session.execute(
            text('UPDATE memory_units SET last_outcome_at = :ts WHERE id = :id'),
            {
                'ts': datetime.now(timezone.utc) - timedelta(days=last_outcome_age_days),
                'id': str(unit.id),
            },
        )
        await session.commit()
    return unit


async def _attach_dormant_entity(
    session: AsyncSession,
    *,
    vault: Vault,
    unit: MemoryUnit,
    age_days: float,
) -> Entity:
    """Attach an entity to a unit and age its ``last_seen`` so entity
    dormancy is meaningful when the linter scores the unit."""
    entity = Entity(canonical_name=f'ent-{uuid4().hex[:6]}')
    session.add(entity)
    await session.commit()
    await session.refresh(entity)
    ue = UnitEntity(unit_id=unit.id, entity_id=entity.id, vault_id=vault.id)
    session.add(ue)
    await session.commit()
    await session.execute(
        text('UPDATE entities SET last_seen = :ts WHERE id = :id'),
        {
            'ts': datetime.now(timezone.utc) - timedelta(days=age_days),
            'id': str(entity.id),
        },
    )
    await session.commit()
    return entity


async def _link_units(
    session: AsyncSession,
    *,
    vault: Vault,
    src: MemoryUnit,
    dst: MemoryUnit,
    link_type: str,
    weight: float = 1.0,
    age_days: float = 0.0,
) -> None:
    link = MemoryLink(
        from_unit_id=src.id,
        to_unit_id=dst.id,
        vault_id=vault.id,
        link_type=link_type,
        weight=weight,
    )
    session.add(link)
    await session.commit()
    if age_days > 0:
        await session.execute(
            text(
                'UPDATE memory_links SET created_at = :ts '
                'WHERE from_unit_id = :src AND to_unit_id = :dst '
                'AND link_type = :lt'
            ),
            {
                'ts': datetime.now(timezone.utc) - timedelta(days=age_days),
                'src': str(src.id),
                'dst': str(dst.id),
                'lt': link_type,
            },
        )
        await session.commit()


async def _proposals_for(
    session: AsyncSession,
    *,
    vault_id: UUID,
    rule_name: str,
    target_id: UUID | None = None,
) -> list[dict]:
    sql = """
        SELECT id::text AS id, target_id, status, evidence
        FROM maintenance_proposals
        WHERE vault_id = :vault_id AND rule_name = :rule_name
    """
    params = {'vault_id': str(vault_id), 'rule_name': rule_name}
    if target_id is not None:
        sql += ' AND target_id = :target_id'
        params['target_id'] = str(target_id)
    rows = (await session.execute(text(sql), params)).all()
    return [
        {
            'id': r.id,
            'target_id': str(r.target_id),
            'status': r.status,
            'evidence': r.evidence,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_composite_rule_fires_for_unit_with_strong_negative_signals(
    session: AsyncSession, api
) -> None:
    """A unit with low MW + stale outcome + ephemeral baseline must trip
    the composite_deprioritize_candidate rule."""
    vault = await _make_vault(session, 'composite-fires')
    note = await _make_note(session, vault)
    bad = await _make_unit(
        session,
        vault=vault,
        note=note,
        text_body='cold ephemeral unit',
        intent_class='ephemeral',
        importance=0.3,
        stability=14.0,
        success=0,
        failure=20,
        last_outcome_age_days=200.0,
    )
    # Add three contradictions from credible sources to push graph_pressure up.
    for _ in range(3):
        src = await _make_unit(
            session,
            vault=vault,
            note=note,
            text_body=f'contradicting source {uuid4().hex[:6]}',
            success=10,
            failure=0,
            confidence=1.0,
        )
        await _link_units(session, vault=vault, src=src, dst=bad, link_type='contradicts')

    await api.lint.run_rules(vault.id)

    composite = await _proposals_for(
        session, vault_id=vault.id, rule_name='composite_deprioritize_candidate', target_id=bad.id
    )
    assert len(composite) == 1, f'expected one composite proposal, got {composite}'
    evidence = composite[0]['evidence']
    assert evidence.get('composite_score', 0.0) > 0.55
    assert 'components' in evidence
    for key in ('graph_pressure', 'mw_complement', 'temporal_staleness', 'entity_dormancy'):
        assert key in evidence['components']


@pytest.mark.asyncio
async def test_composite_rule_does_not_fire_for_neutral_unit(session: AsyncSession, api) -> None:
    """A cold-start, durable unit with no inbound contradictions stays clean."""
    vault = await _make_vault(session, 'composite-clean')
    note = await _make_note(session, vault)
    clean = await _make_unit(
        session,
        vault=vault,
        note=note,
        text_body='neutral unit',
        intent_class='durable',
        importance=0.7,
        stability=180.0,
    )
    await api.lint.run_rules(vault.id)
    composite = await _proposals_for(
        session, vault_id=vault.id, rule_name='composite_deprioritize_candidate', target_id=clean.id
    )
    assert composite == []


@pytest.mark.asyncio
async def test_high_mw_flag_reason_only_with_high_mw(session: AsyncSession, api) -> None:
    """The consolidated rule emits ``flag_reason='high_mw_with_nonmw_pressure'``
    only when MW > 0.7 over >= 5 outcomes; a low-MW unit with the same
    composite gets ``flag_reason='composite'`` (or a different escalation
    reason that takes precedence)."""
    vault = await _make_vault(session, 'high-mw')
    note = await _make_note(session, vault)
    high_mw_drop = await _make_unit(
        session,
        vault=vault,
        note=note,
        text_body='high mw drop',
        intent_class='ephemeral',
        importance=0.3,
        stability=14.0,
        success=10,
        failure=2,
        last_outcome_age_days=200.0,
    )
    src = await _make_unit(session, vault=vault, note=note, text_body='contradictor', success=10)
    await _link_units(session, vault=vault, src=src, dst=high_mw_drop, link_type='contradicts')

    low_mw_drop = await _make_unit(
        session,
        vault=vault,
        note=note,
        text_body='low mw, never had outcomes',
        intent_class='ephemeral',
        importance=0.3,
        stability=14.0,
        success=0,
        failure=20,
        last_outcome_age_days=200.0,
    )
    src2 = await _make_unit(session, vault=vault, note=note, text_body='contradictor 2', success=10)
    await _link_units(session, vault=vault, src=src2, dst=low_mw_drop, link_type='contradicts')

    await api.lint.run_rules(vault.id)
    fired_high = await _proposals_for(
        session,
        vault_id=vault.id,
        rule_name='composite_deprioritize_candidate',
        target_id=high_mw_drop.id,
    )
    fired_low = await _proposals_for(
        session,
        vault_id=vault.id,
        rule_name='composite_deprioritize_candidate',
        target_id=low_mw_drop.id,
    )
    assert len(fired_high) == 1
    assert fired_high[0]['evidence'].get('flag_reason') == 'high_mw_with_nonmw_pressure'
    # Low MW unit should still fire (composite is high) but with a different
    # flag_reason — never 'high_mw_with_nonmw_pressure'.
    if fired_low:
        assert fired_low[0]['evidence'].get('flag_reason') != 'high_mw_with_nonmw_pressure'


@pytest.mark.asyncio
async def test_low_credibility_flag_reason_only_with_weak_sources(
    session: AsyncSession, api
) -> None:
    """The consolidated rule emits
    ``flag_reason='low_credibility_contradiction_only'`` only when the
    aggregate source credibility is below the threshold; a unit with a
    high-credibility contradictor gets a different ``flag_reason``."""
    vault = await _make_vault(session, 'low-cred')
    note = await _make_note(session, vault)
    weakly_contradicted = await _make_unit(
        session,
        vault=vault,
        note=note,
        text_body='weakly contradicted',
        intent_class='ephemeral',
        importance=0.3,
        stability=14.0,
        success=0,
        failure=10,
        last_outcome_age_days=100.0,
    )
    weak_src = await _make_unit(
        session,
        vault=vault,
        note=note,
        text_body='weak src',
        confidence=0.2,
        success=0,
        failure=5,
    )
    await _link_units(
        session, vault=vault, src=weak_src, dst=weakly_contradicted, link_type='contradicts'
    )

    strongly_contradicted = await _make_unit(
        session,
        vault=vault,
        note=note,
        text_body='strongly contradicted',
        intent_class='ephemeral',
        importance=0.3,
        stability=14.0,
        success=0,
        failure=10,
        last_outcome_age_days=100.0,
    )
    strong_src = await _make_unit(
        session,
        vault=vault,
        note=note,
        text_body='strong src',
        confidence=1.0,
        success=10,
    )
    await _link_units(
        session,
        vault=vault,
        src=strong_src,
        dst=strongly_contradicted,
        link_type='contradicts',
    )

    await api.lint.run_rules(vault.id)
    weak_props = await _proposals_for(
        session,
        vault_id=vault.id,
        rule_name='composite_deprioritize_candidate',
        target_id=weakly_contradicted.id,
    )
    strong_props = await _proposals_for(
        session,
        vault_id=vault.id,
        rule_name='composite_deprioritize_candidate',
        target_id=strongly_contradicted.id,
    )
    assert len(weak_props) == 1
    assert weak_props[0]['evidence'].get('flag_reason') == 'low_credibility_contradiction_only'
    # Strong-source unit must NOT carry the low-credibility flag_reason.
    if strong_props:
        assert (
            strong_props[0]['evidence'].get('flag_reason') != 'low_credibility_contradiction_only'
        )


@pytest.mark.asyncio
async def test_run_rules_idempotent_on_rerun(session: AsyncSession, api) -> None:
    """Re-running the linter doesn't duplicate FSFM proposals."""
    vault = await _make_vault(session, 'idempotent')
    note = await _make_note(session, vault)
    unit = await _make_unit(
        session,
        vault=vault,
        note=note,
        text_body='unit',
        intent_class='ephemeral',
        importance=0.3,
        stability=14.0,
        success=0,
        failure=20,
        last_outcome_age_days=200.0,
    )
    src = await _make_unit(session, vault=vault, note=note, text_body='ctr', success=10)
    await _link_units(session, vault=vault, src=src, dst=unit, link_type='contradicts')

    await api.lint.run_rules(vault.id)
    after_first = await _proposals_for(
        session,
        vault_id=vault.id,
        rule_name='composite_deprioritize_candidate',
        target_id=unit.id,
    )
    await api.lint.run_rules(vault.id)
    after_second = await _proposals_for(
        session,
        vault_id=vault.id,
        rule_name='composite_deprioritize_candidate',
        target_id=unit.id,
    )
    assert len(after_first) == 1
    assert len(after_second) == 1


@pytest.mark.asyncio
async def test_auto_band_deprioritizes_above_threshold(session: AsyncSession, api) -> None:
    """Above auto threshold + no escalation + no cooldown → flips is_deprioritized."""
    vault = await _make_vault(session, 'auto-fires')
    note = await _make_note(session, vault)
    target = await _make_unit(
        session,
        vault=vault,
        note=note,
        text_body='auto target',
        intent_class='ephemeral',
        importance=0.3,
        stability=14.0,
        success=0,
        failure=20,
        last_outcome_age_days=200.0,
    )
    # Push graph + temporal high enough so the composite reaches > 0.80.
    for _ in range(5):
        src = await _make_unit(
            session, vault=vault, note=note, text_body=f's-{uuid4().hex[:4]}', success=10
        )
        await _link_units(session, vault=vault, src=src, dst=target, link_type='contradicts')

    await api.lint.run_rules(vault.id)
    summary = await api.auto_deprioritize_after_lint(vault.id)

    assert summary.enabled is True
    assert str(target.id) in summary.deprioritized

    refreshed = (
        await session.execute(
            text('SELECT is_deprioritized FROM memory_units WHERE id = :id'),
            {'id': str(target.id)},
        )
    ).scalar()
    assert refreshed is True


@pytest.mark.asyncio
async def test_auto_band_skips_when_flag_reason_is_non_composite(
    session: AsyncSession, api
) -> None:
    """A unit whose consolidated proposal carries any non-``composite``
    ``flag_reason`` lands in ``summary.skipped_escalation`` and is NOT
    auto-deprioritized — the auto-band acts only on the vanilla
    ``flag_reason='composite'``.

    The signal mix below (high MW, ≥5 outcomes, dormant entity, stale
    last_outcome) trips multiple escalation predicates. Per the SQL
    precedence ordering (low_credibility → components_disagree →
    high_mw_with_nonmw_pressure → composite), the row's flag_reason
    is whichever escalation matches first; the assertion verifies it's
    NOT ``composite`` and the unit stays active.
    """
    vault = await _make_vault(session, 'auto-escalates')
    note = await _make_note(session, vault)
    target = await _make_unit(
        session,
        vault=vault,
        note=note,
        text_body='escalating target',
        intent_class='ephemeral',
        importance=0.3,
        stability=14.0,
        success=10,  # High MW history
        failure=2,
        last_outcome_age_days=200.0,
    )
    for _ in range(5):
        src = await _make_unit(
            session, vault=vault, note=note, text_body=f's-{uuid4().hex[:4]}', success=10
        )
        await _link_units(session, vault=vault, src=src, dst=target, link_type='contradicts')
    await _attach_dormant_entity(session, vault=vault, unit=target, age_days=600.0)

    await api.lint.run_rules(vault.id)
    proposals = await _proposals_for(
        session,
        vault_id=vault.id,
        rule_name='composite_deprioritize_candidate',
        target_id=target.id,
    )
    assert len(proposals) == 1
    flag_reason = proposals[0]['evidence'].get('flag_reason')
    assert flag_reason in {
        'high_mw_with_nonmw_pressure',
        'components_disagree',
        'low_credibility_contradiction_only',
    }, f'expected an escalation flag_reason, got {flag_reason!r}'

    summary = await api.auto_deprioritize_after_lint(vault.id)
    assert str(target.id) in summary.skipped_escalation
    assert str(target.id) not in summary.deprioritized
    refreshed = (
        await session.execute(
            text('SELECT is_deprioritized FROM memory_units WHERE id = :id'),
            {'id': str(target.id)},
        )
    ).scalar()
    assert refreshed is False


@pytest.mark.asyncio
async def test_high_mw_flag_reason_wins_for_pure_high_mw_pattern(
    session: AsyncSession, api
) -> None:
    """A unit constructed to match ONLY the high-MW pattern (no
    components_disagree, no low_credibility) MUST get
    ``flag_reason='high_mw_with_nonmw_pressure'`` — this pins the
    consolidated rule's precedence ordering."""
    vault = await _make_vault(session, 'high-mw-pure')
    note = await _make_note(session, vault)
    target = await _make_unit(
        session,
        vault=vault,
        note=note,
        text_body='pure high-mw target',
        intent_class='ephemeral',
        importance=0.3,
        stability=14.0,
        success=10,
        failure=2,
        last_outcome_age_days=200.0,
    )
    # Single contradictor from a HIGH-credibility source so the
    # low_credibility_contradiction_only branch does NOT match.
    src = await _make_unit(
        session, vault=vault, note=note, text_body='strong src', success=10, confidence=1.0
    )
    await _link_units(session, vault=vault, src=src, dst=target, link_type='contradicts')
    # No entity attached → freshest_entity_last_seen IS NULL →
    # components_disagree's gating clause excludes this unit.

    await api.lint.run_rules(vault.id)
    proposals = await _proposals_for(
        session,
        vault_id=vault.id,
        rule_name='composite_deprioritize_candidate',
        target_id=target.id,
    )
    assert len(proposals) == 1
    assert proposals[0]['evidence'].get('flag_reason') == 'high_mw_with_nonmw_pressure', proposals[
        0
    ]['evidence']


@pytest.mark.asyncio
async def test_auto_band_skips_when_cooldown_active(session: AsyncSession, api) -> None:
    """A unit with a memory_restore audit within cooldown_days is left alone."""
    vault = await _make_vault(session, 'auto-cooldown')
    note = await _make_note(session, vault)
    target = await _make_unit(
        session,
        vault=vault,
        note=note,
        text_body='recently-restored',
        intent_class='ephemeral',
        importance=0.3,
        stability=14.0,
        success=0,
        failure=20,
        last_outcome_age_days=200.0,
    )
    for _ in range(5):
        src = await _make_unit(
            session, vault=vault, note=note, text_body=f's-{uuid4().hex[:4]}', success=10
        )
        await _link_units(session, vault=vault, src=src, dst=target, link_type='contradicts')

    # Insert a fresh memory_restore audit (within cooldown_days).
    audit = AuditLog(
        action='memory_restore',
        actor='user',
        resource_type='memory_unit',
        resource_id=str(target.id),
        details={},
    )
    session.add(audit)
    await session.commit()

    await api.lint.run_rules(vault.id)
    summary = await api.auto_deprioritize_after_lint(vault.id)
    assert str(target.id) in summary.skipped_cooldown
    refreshed = (
        await session.execute(
            text('SELECT is_deprioritized FROM memory_units WHERE id = :id'),
            {'id': str(target.id)},
        )
    ).scalar()
    assert refreshed is False


@pytest.mark.asyncio
async def test_auto_band_respects_vault_scope(session: AsyncSession, api) -> None:
    """Running the auto-band on vault A must not touch units in vault B."""
    vault_a = await _make_vault(session, 'auto-scope-a')
    vault_b = await _make_vault(session, 'auto-scope-b')
    await _make_note(session, vault_a)
    note_b = await _make_note(session, vault_b)

    target_b = await _make_unit(
        session,
        vault=vault_b,
        note=note_b,
        text_body='target in B',
        intent_class='ephemeral',
        importance=0.3,
        stability=14.0,
        success=0,
        failure=20,
        last_outcome_age_days=200.0,
    )
    for _ in range(5):
        src = await _make_unit(
            session,
            vault=vault_b,
            note=note_b,
            text_body=f's-{uuid4().hex[:4]}',
            success=10,
        )
        await _link_units(session, vault=vault_b, src=src, dst=target_b, link_type='contradicts')

    # Rules + auto-band for vault A only.
    await api.lint.run_rules(vault_a.id)
    summary = await api.auto_deprioritize_after_lint(vault_a.id)
    assert summary.deprioritized == []
    assert summary.skipped_below_threshold == []
    refreshed = (
        await session.execute(
            text('SELECT is_deprioritized FROM memory_units WHERE id = :id'),
            {'id': str(target_b.id)},
        )
    ).scalar()
    assert refreshed is False


@pytest.mark.asyncio
async def test_sensitive_unreviewed_predicate_replacement(session: AsyncSession, api) -> None:
    """The new predicate fires when no recent governance audit exists, and
    silences after a fresh memory_restore (or memory_deprioritize) audit lands."""
    vault = await _make_vault(session, 'sensitive')
    note = await _make_note(session, vault)
    sensitive = await _make_unit(
        session,
        vault=vault,
        note=note,
        text_body='sensitive unit',
        intent_class='durable',
        risk_class='sensitive',
        importance=0.7,
        stability=180.0,
    )

    await api.lint.run_rules(vault.id)
    fired = await _proposals_for(
        session,
        vault_id=vault.id,
        rule_name='sensitive_unreviewed_unit',
        target_id=sensitive.id,
    )
    assert len(fired) == 1, f'expected sensitive_unreviewed proposal, got {fired}'

    # Resolve & insert a fresh memory_restore audit; rerun should NOT
    # produce a *new* proposal (existing one stays pending — idempotent
    # partial unique index — and no fresh row gets emitted).
    audit = AuditLog(
        action='memory_restore',
        actor='reviewer',
        resource_type='memory_unit',
        resource_id=str(sensitive.id),
        details={},
    )
    session.add(audit)
    await session.commit()

    # Resolve the existing pending one so the predicate has a clean slate.
    await session.execute(
        text("UPDATE maintenance_proposals SET status = 'resolved' WHERE id = :id"),
        {'id': fired[0]['id']},
    )
    await session.commit()

    await api.lint.run_rules(vault.id)
    after = await _proposals_for(
        session,
        vault_id=vault.id,
        rule_name='sensitive_unreviewed_unit',
        target_id=sensitive.id,
    )
    pending_after = [p for p in after if p['status'] == 'pending']
    assert pending_after == [], (
        f'sensitive predicate should suppress after memory_restore audit, got {pending_after}'
    )


@pytest.mark.asyncio
async def test_auto_band_resolves_proposal_and_second_tick_is_noop(
    session: AsyncSession, api
) -> None:
    """Regression: an auto-band tick MUST flip the consumed
    ``composite_deprioritize_candidate`` proposal to ``status='resolved'``
    (via :py:meth:`LintService.set_status`) so the next tick does not
    re-process the same row. Earlier code called a non-existent
    ``set_finding_status`` whose AttributeError was swallowed by a broad
    ``except Exception``, leaving the proposal pending forever and the
    auto-band re-firing on every periodic tick."""
    vault = await _make_vault(session, 'auto-resolve')
    note = await _make_note(session, vault)
    target = await _make_unit(
        session,
        vault=vault,
        note=note,
        text_body='auto resolve target',
        intent_class='ephemeral',
        importance=0.3,
        stability=14.0,
        success=0,
        failure=20,
        last_outcome_age_days=200.0,
    )
    for _ in range(5):
        src = await _make_unit(
            session, vault=vault, note=note, text_body=f's-{uuid4().hex[:4]}', success=10
        )
        await _link_units(session, vault=vault, src=src, dst=target, link_type='contradicts')

    await api.lint.run_rules(vault.id)
    summary_first = await api.auto_deprioritize_after_lint(vault.id)
    assert str(target.id) in summary_first.deprioritized

    # Proposal must have been flipped to resolved by the auto-band itself,
    # not left pending. The unique partial index keys ``status='pending'``,
    # so a lingering pending row would also block re-emission of the rule.
    proposals = await _proposals_for(
        session,
        vault_id=vault.id,
        rule_name='composite_deprioritize_candidate',
        target_id=target.id,
    )
    assert len(proposals) == 1
    assert proposals[0]['status'] == 'resolved', (
        f'auto-band did not resolve the consumed proposal: {proposals[0]}'
    )

    # Second tick must be a no-op: nothing left to deprioritize.
    summary_second = await api.auto_deprioritize_after_lint(vault.id)
    assert summary_second.deprioritized == []
    assert summary_second.skipped_below_threshold == []
    assert summary_second.skipped_escalation == []
    assert summary_second.skipped_cooldown == []
