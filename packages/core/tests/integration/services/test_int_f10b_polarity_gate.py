"""F10b — polarity-discriminating NLI gate integration tests.

Drives ``LintLLMService.maybe_run`` end-to-end against real Postgres + the
real ONNX NLI classifier (``cross-encoder/nli-deberta-v3-small``). Asserts:

* polarity-inversion fixture (POC-002 fixture: "FastAPI is a Python web framework"
  vs "FastAPI is not a Python web framework") clears the gate via the polarity
  branch when cosine surprise is below threshold, the LLM check fires, and a
  ``MaintenanceProposal`` is written;
* topic-drift fixture (compatible facts, "User prefers production" vs "User
  uses Postgres on production") does NOT clear the gate — NLI labels it
  neutral and no proposal is written;
* both-branches case (cosine surprise >= threshold) skips the NLI invocation
  entirely (the F10b cosine pre-filter).

The NLI model is session-scoped because it is ~140 MB.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import numpy as np
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from memex_common.config import MemexConfig, NLIModelConfig
from memex_core.memory.lint_llm.checks import make_semantic_contradiction_check
from memex_core.memory.lint_llm.polarity import (
    PolarityClassifier,
    PolarityRateLimiter,
)
from memex_core.memory.models import get_embedding_model, get_nli_model
from memex_core.memory.models.anisotropy import get_shared_corrector
from memex_core.memory.sql_models import Vault
from memex_core.services.lint_llm import LintLLMService

pytestmark = [pytest.mark.integration]


@pytest_asyncio.fixture(scope='session')
async def real_embedding_model():
    return await get_embedding_model()


@pytest_asyncio.fixture(scope='session')
async def real_nli_model():
    return await get_nli_model(NLIModelConfig())


@pytest.fixture
def polarity_classifier(real_nli_model) -> PolarityClassifier:
    return PolarityClassifier(
        real_nli_model,
        polarity_threshold=0.6,
        rate_limiter=PolarityRateLimiter(max_per_vault_per_hour=None),
    )


def _service(metastore, config: MemexConfig, filestore) -> LintLLMService:
    return LintLLMService(metastore=metastore, filestore=filestore, config=config)


async def _make_vault(session: AsyncSession) -> UUID:
    v = Vault(name=f'F10b-{uuid4().hex[:8]}')
    session.add(v)
    await session.commit()
    await session.refresh(v)
    return v.id


async def _seed_unit(
    session: AsyncSession,
    *,
    vault_id: UUID,
    text_content: str,
    embedding: list[float],
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
            'title': 'F10b test note',
        },
    )
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
            'emb': str(embedding),
            'ed': datetime.now(timezone.utc),
        },
    )
    await session.commit()
    return unit_id


async def _seed_corpus_with_polarity_inversion(
    session: AsyncSession,
    vault_id: UUID,
    real_embedding_model,
) -> tuple[UUID, list[UUID]]:
    """Build a small Python-frameworks corpus + insert one polarity-inverting
    audited unit. Returns ``(audited_id, peer_ids)``.

    The audited unit is a polarity-inverter ("FastAPI is not a Python web
    framework..."); its embedding sits inside the routine corpus distribution
    so cosine surprise is sub-threshold (POC-002 F3 finding).
    """
    routine_corpus = [
        'FastAPI is a modern Python web framework for building APIs.',
        'Django is a high-level Python web framework with batteries included.',
        'Flask is a lightweight Python micro-framework for web apps.',
        'Starlette is the ASGI toolkit that FastAPI builds on.',
        'Tornado is a Python web framework optimized for long-lived connections.',
        'Sanic is an asynchronous Python web framework focused on speed.',
        'Bottle is a fast micro web-framework for Python.',
        'Falcon is a minimalist Python web framework for fast REST APIs.',
        'Pyramid is a flexible Python web framework.',
        'aiohttp is an asynchronous HTTP client and server framework for Python.',
    ]
    polarity_inverter = 'FastAPI is not a Python web framework and cannot be used to build APIs.'

    all_texts = routine_corpus + [polarity_inverter]
    embeddings = real_embedding_model.encode(all_texts)
    embeddings = np.asarray(embeddings, dtype=np.float32)

    peer_ids: list[UUID] = []
    for i, text_content in enumerate(routine_corpus):
        peer_ids.append(
            await _seed_unit(
                session,
                vault_id=vault_id,
                text_content=text_content,
                embedding=embeddings[i].tolist(),
            )
        )

    audited_id = await _seed_unit(
        session,
        vault_id=vault_id,
        text_content=polarity_inverter,
        embedding=embeddings[-1].tolist(),
    )
    return audited_id, peer_ids


async def _seed_corpus_with_compatible_fact(
    session: AsyncSession,
    vault_id: UUID,
    real_embedding_model,
) -> tuple[UUID, list[UUID]]:
    """Build a corpus where the audited unit is COMPATIBLE with the closest
    peer (subject-sharing, complementary fact, NOT a contradiction). NLI
    should label the top-1 pair neutral, gate stays closed.

    The corpus is built around a single subject (``User``) so the top-1
    nearest neighbour is also ``User``-prefixed — keeping the NLI model in
    its trained shape (premise/hypothesis subject overlap) rather than the
    "different subject" regime where NLI tends toward false-contradiction.
    """
    routine_corpus = [
        'User prefers production environment for deployments.',
        'User has multiple production environments configured.',
        'User runs production deployments on weekday mornings.',
        'User maintains a staging mirror of production.',
        'User reviews every production release.',
        'User has dashboards for production traffic.',
        'User keeps production secrets in AWS Secrets Manager.',
        'User scales production servers based on load.',
        'User monitors production latency via Datadog.',
        'User backs up production databases nightly.',
    ]
    compatible_fact = 'User uses Postgres on production.'

    all_texts = routine_corpus + [compatible_fact]
    embeddings = real_embedding_model.encode(all_texts)
    embeddings = np.asarray(embeddings, dtype=np.float32)

    peer_ids: list[UUID] = []
    for i, text_content in enumerate(routine_corpus):
        peer_ids.append(
            await _seed_unit(
                session,
                vault_id=vault_id,
                text_content=text_content,
                embedding=embeddings[i].tolist(),
            )
        )

    audited_id = await _seed_unit(
        session,
        vault_id=vault_id,
        text_content=compatible_fact,
        embedding=embeddings[-1].tolist(),
    )
    return audited_id, peer_ids


@pytest_asyncio.fixture(autouse=True)
async def _warm_corrector():
    corrector = get_shared_corrector()
    rng = np.random.default_rng(0)
    for _ in range(64):
        sample = float(rng.uniform(0.4, 0.85))
        corrector.normalize(sample)
    yield


@pytest.mark.asyncio
async def test_polarity_inversion_clears_gate_and_writes_proposal(
    session: AsyncSession,
    metastore,
    filestore,
    memex_config,
    polarity_classifier,
    real_embedding_model,
    monkeypatch,
):
    """POC-002 fixture (polarity inversion) reaches the LLM check and produces
    a ``MaintenanceProposal`` via the F10b polarity branch.

    Pins ``compute_unit_surprise`` to a sub-threshold value so the test
    isolates the F10b code path: in the POC the polarity-inverter's cosine
    surprise was 0.532 (well below 0.7), and the corrector calibration depends
    on the long-tail of similarities seen in the production retrieval pool.
    Smaller test corpora skew that calibration; pinning the score keeps the
    contract under test (``cosine sub-threshold + NLI contradiction → gate
    clears``) deterministic.
    """
    vault_id = await _make_vault(session)
    audited_id, _peer_ids = await _seed_corpus_with_polarity_inversion(
        session, vault_id, real_embedding_model
    )

    monkeypatch.setattr(
        'memex_core.services.lint_llm.compute_unit_surprise',
        AsyncMock(return_value=0.55),
    )

    fake_prediction = SimpleNamespace(
        has_contradiction=True,
        contradiction_with_unit_indices=[0],
        explanation='Audited inverts the polarity of the corpus claim about FastAPI.',
    )
    mock_run = AsyncMock(return_value=fake_prediction)
    monkeypatch.setattr('memex_core.llm.run_dspy_operation', mock_run)

    check = make_semantic_contradiction_check(lm=object(), k=8)
    svc = _service(metastore, memex_config, filestore)
    outcome = await svc.maybe_run(
        audited_id,
        vault_id,
        run_llm_check=check,
        session=session,
        polarity_classifier=polarity_classifier,
    )
    await session.commit()

    assert outcome.polarity_invoked is True
    assert outcome.polarity_rate_limited is False
    assert outcome.polarity_model_failed is False
    assert outcome.polarity_contradiction_prob is not None
    assert outcome.polarity_contradiction_prob >= 0.6, (
        f'polarity contradiction prob too low: {outcome.polarity_contradiction_prob}'
    )
    assert outcome.skipped_below_threshold is False
    assert outcome.finding_emitted is True
    mock_run.assert_awaited_once()

    rows = (
        await session.execute(
            text("""
                SELECT rule_name, evidence::text AS evidence
                FROM maintenance_proposals
                WHERE vault_id = :vid AND target_id = :tid AND status = 'pending'
            """),
            {'vid': str(vault_id), 'tid': str(audited_id)},
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].rule_name == 'llm_semantic_contradiction'
    assert 'polarity_label' in rows[0].evidence
    assert 'polarity_contradiction_prob' in rows[0].evidence


@pytest.mark.asyncio
async def test_compatible_fact_does_not_clear_gate(
    session: AsyncSession,
    metastore,
    filestore,
    memex_config,
    polarity_classifier,
    real_embedding_model,
    monkeypatch,
):
    """Topic-drift / compatible-fact fixture: NLI returns low contradiction
    probability and the gate stays closed — no proposal is written.

    Pins cosine surprise to sub-threshold (same reasoning as the polarity
    inversion test).
    """
    vault_id = await _make_vault(session)
    audited_id, _peer_ids = await _seed_corpus_with_compatible_fact(
        session, vault_id, real_embedding_model
    )

    monkeypatch.setattr(
        'memex_core.services.lint_llm.compute_unit_surprise',
        AsyncMock(return_value=0.55),
    )

    mock_run = AsyncMock()
    monkeypatch.setattr('memex_core.llm.run_dspy_operation', mock_run)

    check = make_semantic_contradiction_check(lm=object(), k=8)
    svc = _service(metastore, memex_config, filestore)
    outcome = await svc.maybe_run(
        audited_id,
        vault_id,
        run_llm_check=check,
        session=session,
        polarity_classifier=polarity_classifier,
    )
    await session.commit()

    assert outcome.polarity_invoked is True
    assert outcome.polarity_rate_limited is False
    assert outcome.polarity_model_failed is False
    assert outcome.polarity_contradiction_prob is not None
    assert outcome.polarity_contradiction_prob < 0.6, (
        f'polarity contradiction prob too high: {outcome.polarity_contradiction_prob}'
    )
    assert outcome.skipped_below_threshold is True
    assert outcome.finding_emitted is False
    mock_run.assert_not_awaited()

    proposal_count = (
        await session.execute(
            text("""
                SELECT count(*) AS n
                FROM maintenance_proposals
                WHERE vault_id = :vid AND target_id = :tid
            """),
            {'vid': str(vault_id), 'tid': str(audited_id)},
        )
    ).scalar()
    assert proposal_count == 0


@pytest.mark.asyncio
async def test_cosine_already_clears_skips_nli(
    session: AsyncSession,
    metastore,
    filestore,
    memex_config,
    real_embedding_model,
    monkeypatch,
):
    """Cosine surprise at/above threshold MUST skip the NLI invocation
    (F10b cheap pre-filter contract)."""
    nli_model = AsyncMock()
    nli_model.classify = AsyncMock(
        return_value={'contradiction': 0.0, 'entailment': 0.0, 'neutral': 1.0}
    )
    polarity_classifier = PolarityClassifier(nli_model)

    monkeypatch.setattr(
        'memex_core.memory.lint_llm.surprise.compute_unit_surprise',
        AsyncMock(return_value=0.95),
    )
    monkeypatch.setattr(
        'memex_core.services.lint_llm.compute_unit_surprise',
        AsyncMock(return_value=0.95),
    )

    vault_id = await _make_vault(session)
    audited_id, _ = await _seed_corpus_with_polarity_inversion(
        session, vault_id, real_embedding_model
    )

    fake_prediction = SimpleNamespace(
        has_contradiction=True,
        contradiction_with_unit_indices=[0],
        explanation='dummy',
    )
    monkeypatch.setattr(
        'memex_core.llm.run_dspy_operation',
        AsyncMock(return_value=fake_prediction),
    )

    check = make_semantic_contradiction_check(lm=object(), k=8)
    svc = _service(metastore, memex_config, filestore)
    outcome = await svc.maybe_run(
        audited_id,
        vault_id,
        run_llm_check=check,
        session=session,
        polarity_classifier=polarity_classifier,
    )
    await session.commit()

    assert outcome.surprise_score == 0.95
    assert outcome.polarity_invoked is False
    assert outcome.polarity_rate_limited is False
    assert outcome.polarity_model_failed is False
    nli_model.classify.assert_not_awaited()
    assert outcome.finding_emitted is True
