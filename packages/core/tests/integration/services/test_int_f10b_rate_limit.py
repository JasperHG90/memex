"""F10b — per-vault NLI rate-limit integration test.

Drives N pairs through ``LintLLMService.maybe_run`` with a stub NLI model
behind a ``PolarityRateLimiter`` and asserts the per-vault counter increments
and stops invoking NLI past the cap. Rate-limit is in-memory (process-local
counters); the F10 lint scheduler is single-leader so a process-local counter
is sufficient.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from memex_common.config import MemexConfig
from memex_core.memory.lint_llm.polarity import (
    PolarityClassifier,
    PolarityRateLimiter,
)
from memex_core.memory.sql_models import Vault
from memex_core.services.lint_llm import LintLLMService

pytestmark = [pytest.mark.integration]


def _service(metastore, config: MemexConfig, filestore) -> LintLLMService:
    return LintLLMService(metastore=metastore, filestore=filestore, config=config)


async def _make_vault(session: AsyncSession) -> UUID:
    v = Vault(name=f'F10b-rl-{uuid4().hex[:8]}')
    session.add(v)
    await session.commit()
    await session.refresh(v)
    return v.id


async def _seed_unit(session: AsyncSession, vault_id: UUID, txt: str) -> UUID:
    unit_id = uuid4()
    note_id = uuid4()
    await session.execute(
        text("""
            INSERT INTO notes (id, vault_id, content_hash, title)
            VALUES (:id, :vid, :hash, 't')
        """),
        {'id': str(note_id), 'vid': str(vault_id), 'hash': uuid4().hex},
    )
    emb = [0.1] * 384
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
            'text': txt,
            'emb': str(emb),
            'ed': datetime.now(timezone.utc),
        },
    )
    await session.commit()
    return unit_id


@pytest.mark.asyncio
async def test_per_vault_rate_limit_caps_nli_invocations(
    session: AsyncSession,
    metastore,
    filestore,
    memex_config,
    monkeypatch,
):
    """First ``cap`` units invoke NLI; subsequent units fall back to cosine-only
    (gate stays closed because cosine surprise is below threshold)."""
    monkeypatch.setattr(
        'memex_core.services.lint_llm.compute_unit_surprise',
        AsyncMock(return_value=0.4),
    )

    nli_model = AsyncMock()
    nli_model.classify = AsyncMock(
        return_value={'contradiction': 0.05, 'entailment': 0.05, 'neutral': 0.9}
    )
    cap = 3
    classifier = PolarityClassifier(
        nli_model,
        rate_limiter=PolarityRateLimiter(max_per_vault_per_hour=cap),
    )

    vault_id = await _make_vault(session)
    unit_ids = [await _seed_unit(session, vault_id, f'fact {i}') for i in range(5)]
    for i in range(8):
        await _seed_unit(session, vault_id, f'peer {i}')

    svc = _service(metastore, memex_config, filestore)
    invoked_count = 0
    fallback_count = 0
    for uid in unit_ids:
        outcome = await svc.maybe_run(
            uid,
            vault_id,
            run_llm_check=AsyncMock(return_value=None),
            session=session,
            polarity_classifier=classifier,
        )
        if outcome.polarity_invoked and outcome.polarity_contradiction_prob is not None:
            invoked_count += 1
        elif outcome.polarity_invoked:
            fallback_count += 1
        await session.commit()

    assert nli_model.classify.await_count == cap
    assert invoked_count == cap
    assert fallback_count == len(unit_ids) - cap


@pytest.mark.asyncio
async def test_rate_limit_per_vault_isolated(
    session: AsyncSession,
    metastore,
    filestore,
    memex_config,
    monkeypatch,
):
    """A single classifier with a per-vault cap of 1 admits one call per vault
    independently — three vaults, three calls."""
    monkeypatch.setattr(
        'memex_core.services.lint_llm.compute_unit_surprise',
        AsyncMock(return_value=0.4),
    )

    nli_model = AsyncMock()
    nli_model.classify = AsyncMock(
        return_value={'contradiction': 0.1, 'entailment': 0.1, 'neutral': 0.8}
    )
    classifier = PolarityClassifier(
        nli_model,
        rate_limiter=PolarityRateLimiter(max_per_vault_per_hour=1),
    )

    svc = _service(metastore, memex_config, filestore)
    for _ in range(3):
        vault_id = await _make_vault(session)
        for j in range(8):
            await _seed_unit(session, vault_id, f'peer {j} {uuid4().hex[:4]}')
        unit_id = await _seed_unit(session, vault_id, 'audited')
        await svc.maybe_run(
            unit_id,
            vault_id,
            run_llm_check=AsyncMock(return_value=None),
            session=session,
            polarity_classifier=classifier,
        )
        await session.commit()

    assert nli_model.classify.await_count == 3
