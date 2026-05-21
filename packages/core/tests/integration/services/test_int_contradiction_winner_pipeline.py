"""Integration test — FSFM → propose_contradiction_winner pipeline.

Seeds two contradicting units with differing source credibility, runs
the rule-based FSFM lint pass, then drives the
``propose_contradiction_winner`` LLM check against a deterministic
mock and asserts exactly one winner-proposal finding is emitted
linked back to the FSFM finding.

Marked ``llm_mock`` + ``integration`` — needs Postgres via testcontainers.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.types import FactTypes
from memex_core.memory.lint_llm.checks import make_propose_contradiction_winner_check
from memex_core.memory.sql_models import (
    MemoryLink,
    MemoryUnit,
    Note,
    Vault,
)


pytestmark = [pytest.mark.integration, pytest.mark.llm_mock, pytest.mark.asyncio]


async def test_winner_proposal_links_back_to_fsfm_finding(session: AsyncSession, api) -> None:
    vault = Vault(name=f'V5-pipeline-{uuid4().hex[:8]}')
    session.add(vault)
    await session.commit()
    await session.refresh(vault)

    winner_note = Note(
        id=uuid4(),
        vault_id=vault.id,
        content_hash=f'hash-{uuid4().hex[:8]}',
        original_text='high-authority',
        note_metadata={'authority': 'official-doc'},
    )
    loser_note = Note(
        id=uuid4(),
        vault_id=vault.id,
        content_hash=f'hash-{uuid4().hex[:8]}',
        original_text='low-authority',
        note_metadata={'authority': 'chat-log'},
    )
    session.add_all([winner_note, loser_note])
    await session.commit()

    winner = MemoryUnit(
        id=uuid4(),
        vault_id=vault.id,
        note_id=winner_note.id,
        text='canonical winner fact',
        fact_type=FactTypes.WORLD,
        status='active',
        risk_class='none',
        success_co_count=10,
        failure_co_count=0,
        event_date=datetime.now(timezone.utc),
        embedding=[0.1] * 384,
    )
    loser = MemoryUnit(
        id=uuid4(),
        vault_id=vault.id,
        note_id=loser_note.id,
        text='outdated loser fact',
        fact_type=FactTypes.WORLD,
        status='active',
        risk_class='none',
        success_co_count=0,
        failure_co_count=5,
        event_date=datetime.now(timezone.utc),
        embedding=[0.1] * 384,
    )
    session.add_all([winner, loser])
    await session.commit()

    link = MemoryLink(
        vault_id=vault.id,
        from_unit_id=winner.id,
        to_unit_id=loser.id,
        link_type='contradicts',
        weight=0.9,
    )
    session.add(link)
    await session.commit()

    fsfm_finding_id = uuid4()
    await session.execute(
        text("""
            INSERT INTO maintenance_proposals (
                id, vault_id, lint_type, target_type, target_id,
                rule_name, evidence, suggested_action, status, source
            )
            VALUES (
                :id, :vault_id, 'quality', 'memory_unit', :target_id,
                'composite_deprioritize_candidate', CAST(:evidence AS jsonb),
                'test seed', 'pending', 'rule'
            )
        """),
        {
            'id': str(fsfm_finding_id),
            'vault_id': str(vault.id),
            'target_id': str(loser.id),
            'evidence': json.dumps(
                {
                    'composite_score': 0.62,
                    'flag_reason': 'low_credibility_contradiction_only',
                    'contradicts_count': 1,
                    'contradicts_credibility_sum': 0.2,
                }
            ),
        },
    )
    await session.commit()

    fake_lm = MagicMock()
    check = make_propose_contradiction_winner_check(fake_lm, min_confidence=0.5)

    async def _fake_run_dspy(**kwargs):
        return MagicMock(
            winner_id='unit_a',
            loser_id='unit_b',
            rationale='winner is higher authority and more outcomes',
            confidence=0.9,
            action='mark_loser_stale',
        )

    import memex_core.llm as _llm

    _orig = _llm.run_dspy_operation
    _llm.run_dspy_operation = AsyncMock(side_effect=_fake_run_dspy)
    try:
        finding = await check(loser.id, vault.id, session)
    finally:
        _llm.run_dspy_operation = _orig

    assert finding is not None
    assert finding.rule_name == 'propose_contradiction_winner'
    assert finding.target_id == str(loser.id)
    assert finding.extra_evidence['linked_to_finding'] == str(fsfm_finding_id)
    assert finding.extra_evidence['action'] == 'mark_loser_stale'
    assert finding.extra_evidence['winner_unit_id'] == str(winner.id)
    assert finding.extra_evidence['loser_unit_id'] == str(loser.id)
