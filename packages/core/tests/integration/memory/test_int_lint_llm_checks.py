"""F10 — DSPy lint check factory integration tests.

Drives ``make_semantic_contradiction_check`` and ``make_schema_drift_check``
through real Postgres (units + embeddings inserted directly) with the
``run_dspy_operation`` call mocked at the central seam. Substantive
content-quality testing of the signatures themselves is the
``@pytest.mark.llm`` real-LLM test.

Maps to RFC-006 §"Required Tests":
- ``test_semantic_contradiction_signature_writes_evidence``
- ``test_schema_drift_signature_writes_evidence``
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from memex_core.memory.lint_llm.checks import (
    make_schema_drift_check,
    make_semantic_contradiction_check,
)
from memex_core.memory.sql_models import LintType, Vault
from memex_core.services.lint_llm import LLMLintFinding


pytestmark = [pytest.mark.integration]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_vault(session: AsyncSession) -> UUID:
    v = Vault(name=f'F10-checks-{uuid4().hex[:8]}')
    session.add(v)
    await session.commit()
    await session.refresh(v)
    return v.id


async def _seed_unit(
    session: AsyncSession,
    *,
    vault_id: UUID,
    text_content: str,
    embedding: list[float] | None = None,
) -> UUID:
    unit_id = uuid4()
    note_id = uuid4()
    await session.execute(
        text("""
            INSERT INTO notes (id, vault_id, content_hash, title)
            VALUES (:id, :vid, :hash, :title)
        """),
        {
            'id': str(note_id),
            'vid': str(vault_id),
            'hash': uuid4().hex,
            'title': 'F10 test note',
        },
    )
    emb = embedding if embedding is not None else [0.1] * 384
    await session.execute(
        text("""
            INSERT INTO memory_units (
                id, note_id, vault_id, text, fact_type, status, embedding, event_date
            )
            VALUES (
                :id, :nid, :vid, :text, 'observation', 'active', :emb, :ed
            )
        """),
        {
            'id': str(unit_id),
            'nid': str(note_id),
            'vid': str(vault_id),
            'text': text_content,
            'emb': str(emb),
            'ed': datetime.now(timezone.utc),
        },
    )
    await session.commit()
    return unit_id


# ---------------------------------------------------------------------------
# Semantic contradiction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_semantic_contradiction_emits_finding_when_llm_says_yes(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the LLM signals has_contradiction=True, the factory returns a
    LLMLintFinding with rule_name='llm_semantic_contradiction', the unit
    surprise score, the explanation, and the cited related unit-ids."""
    vault_id = await _make_vault(session)

    # Audited unit + 8 peers — embeddings stable so top-k is deterministic.
    audited_id = await _seed_unit(
        session, vault_id=vault_id, text_content='User prefers production environment.'
    )
    peer_ids = []
    for i in range(8):
        peer_ids.append(
            await _seed_unit(
                session,
                vault_id=vault_id,
                text_content=f'User prefers staging environment ({i}).',
            )
        )

    # Patch run_dspy_operation to return a contradiction signal.
    fake_prediction = SimpleNamespace(
        has_contradiction=True,
        contradiction_with_unit_indices=[0, 2],
        explanation='Audited prefers production; peers state staging.',
    )
    mock_run = AsyncMock(return_value=fake_prediction)
    monkeypatch.setattr('memex_core.llm.run_dspy_operation', mock_run)

    check = make_semantic_contradiction_check(lm=object(), k=8)
    finding = await check(audited_id, vault_id, session)

    assert isinstance(finding, LLMLintFinding)
    assert finding.rule_name == 'llm_semantic_contradiction'
    assert finding.check_type == 'semantic_contradiction'
    assert finding.target_id == str(audited_id)
    assert finding.target_type == 'memory_unit'
    assert finding.lint_type == LintType.QUALITY
    assert 0.0 <= finding.surprise_score <= 1.0
    assert finding.explanation == 'Audited prefers production; peers state staging.'
    # Cited indices were [0, 2]; cited unit-ids must come from the top-k peers.
    assert len(finding.related_unit_ids) == 2
    for cited in finding.related_unit_ids:
        assert UUID(cited) in peer_ids
    mock_run.assert_awaited_once()


@pytest.mark.asyncio
async def test_semantic_contradiction_returns_none_when_llm_says_no(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault_id = await _make_vault(session)
    audited_id = await _seed_unit(
        session, vault_id=vault_id, text_content='User prefers production environment.'
    )
    for i in range(8):
        await _seed_unit(
            session,
            vault_id=vault_id,
            text_content=f'User uses Postgres on production ({i}).',
        )

    fake_prediction = SimpleNamespace(
        has_contradiction=False,
        contradiction_with_unit_indices=[],
        explanation='',
    )
    monkeypatch.setattr(
        'memex_core.llm.run_dspy_operation', AsyncMock(return_value=fake_prediction)
    )
    check = make_semantic_contradiction_check(lm=object(), k=8)

    assert await check(audited_id, vault_id, session) is None


@pytest.mark.asyncio
async def test_semantic_contradiction_skips_when_unit_missing(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault_id = await _make_vault(session)
    mock_run = AsyncMock()
    monkeypatch.setattr('memex_core.llm.run_dspy_operation', mock_run)

    check = make_semantic_contradiction_check(lm=object(), k=8)
    result = await check(uuid4(), vault_id, session)

    assert result is None
    mock_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_semantic_contradiction_skips_when_corpus_too_sparse(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Below 2 peers, the contradiction call is meaningless — skip it."""
    vault_id = await _make_vault(session)
    audited_id = await _seed_unit(session, vault_id=vault_id, text_content='Single fact.')
    # Only one peer — under the >= 2 threshold.
    await _seed_unit(session, vault_id=vault_id, text_content='Lone other fact.')

    mock_run = AsyncMock()
    monkeypatch.setattr('memex_core.llm.run_dspy_operation', mock_run)

    check = make_semantic_contradiction_check(lm=object(), k=8)
    result = await check(audited_id, vault_id, session)

    assert result is None
    mock_run.assert_not_awaited()


# ---------------------------------------------------------------------------
# Schema drift
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schema_drift_emits_finding_when_llm_says_yes(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the LLM signals has_drift=True, the factory returns an
    LLMLintFinding with check_type='schema_drift', drift_kind in evidence,
    and lint_type=SCHEMA."""
    vault_id = await _make_vault(session)
    audited_id = await _seed_unit(session, vault_id=vault_id, text_content='Date: 12/31/2025')
    for i in range(8):
        await _seed_unit(
            session,
            vault_id=vault_id,
            text_content=f'Date: 2025-12-{i + 1:02d}',
        )

    fake_prediction = SimpleNamespace(
        has_drift=True,
        drift_kind='date_format',
        explanation='Audited uses MM/DD/YYYY; corpus norm is YYYY-MM-DD.',
    )
    mock_run = AsyncMock(return_value=fake_prediction)
    monkeypatch.setattr('memex_core.llm.run_dspy_operation', mock_run)

    check = make_schema_drift_check(lm=object(), k=8)
    finding = await check(audited_id, vault_id, session)

    assert isinstance(finding, LLMLintFinding)
    assert finding.rule_name == 'llm_schema_drift'
    assert finding.check_type == 'schema_drift'
    assert finding.lint_type == LintType.SCHEMA
    assert finding.target_id == str(audited_id)
    assert finding.extra_evidence == {'drift_kind': 'date_format'}
    assert finding.explanation == 'Audited uses MM/DD/YYYY; corpus norm is YYYY-MM-DD.'
    assert finding.related_unit_ids == []
    mock_run.assert_awaited_once()


@pytest.mark.asyncio
async def test_schema_drift_returns_none_when_llm_says_no(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault_id = await _make_vault(session)
    audited_id = await _seed_unit(session, vault_id=vault_id, text_content='Date: 2025-12-31')
    for i in range(8):
        await _seed_unit(
            session,
            vault_id=vault_id,
            text_content=f'Date: 2025-12-{i + 1:02d}',
        )

    fake_prediction = SimpleNamespace(has_drift=False, drift_kind='', explanation='')
    monkeypatch.setattr(
        'memex_core.llm.run_dspy_operation', AsyncMock(return_value=fake_prediction)
    )
    check = make_schema_drift_check(lm=object(), k=8)

    assert await check(audited_id, vault_id, session) is None


@pytest.mark.asyncio
async def test_schema_drift_default_drift_kind_when_signature_omits_it(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault_id = await _make_vault(session)
    audited_id = await _seed_unit(session, vault_id=vault_id, text_content='Audited unit.')
    for i in range(8):
        await _seed_unit(session, vault_id=vault_id, text_content=f'Sample {i}.')

    fake_prediction = SimpleNamespace(has_drift=True, drift_kind='', explanation='Some drift.')
    monkeypatch.setattr(
        'memex_core.llm.run_dspy_operation', AsyncMock(return_value=fake_prediction)
    )
    check = make_schema_drift_check(lm=object(), k=8)
    finding = await check(audited_id, vault_id, session)

    assert finding is not None
    assert finding.extra_evidence == {'drift_kind': 'other'}
