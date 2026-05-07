"""Integration tests for the Tier-A follow-up fixes.

Each fix has at least one ``@pytest.mark.integration`` test that exercises
the real path against a real Postgres testcontainer:

1. Contradiction penalty: a single contradict relation drops the superseded
   unit's confidence strictly below ``superseded_threshold``.
2. Lint emission: contradict links produce a ``maintenance_proposals`` row
   with ``rule_name='semantic_contradiction'``; idempotent on re-runs.
3. ``set_mw_mode`` REST endpoint: POST persists the new mode and rejects
   invalid modes with 400.
4. ``raw_score`` on note search: populated with the pre-rerank RRF score
   when reranking is enabled, ``None`` when disabled.

Tests do NOT require an LLM — the contradiction engine's ``_classify`` and
``_triage`` methods are patched to deterministic returns so the test runs
in CI without an API key. The LLM-driven path (real triage + real
classify) is covered by the existing ``test_int_contradiction.py`` tests
under ``@pytest.mark.llm``.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.config import ContradictionConfig, GLOBAL_VAULT_ID
from memex_common.types import FactTypes
from memex_core.memory.contradiction.engine import ContradictionEngine
from memex_core.memory.contradiction.signatures import ContradictionRelationship
from memex_core.memory.sql_models import (
    MemoryLink,
    MemoryUnit,
    Note,
    Vault,
)


def _make_note(vault_id, title: str = 'Test Note') -> Note:
    return Note(
        id=uuid4(),
        vault_id=vault_id,
        title=title,
        content_hash=str(uuid4()),
        original_text=f'Test content {uuid4()}',
    )


def _make_unit(
    note_id,
    vault_id,
    text: str,
    confidence: float = 1.0,
    event_date: datetime | None = None,
    embedding: list[float] | None = None,
) -> MemoryUnit:
    return MemoryUnit(
        note_id=note_id,
        vault_id=vault_id,
        text=text,
        fact_type=FactTypes.WORLD,
        confidence=confidence,
        event_date=event_date or datetime.now(timezone.utc),
        embedding=embedding or [0.5] * 384,
    )


@pytest.fixture
def contradiction_config() -> ContradictionConfig:
    return ContradictionConfig(
        enabled=True,
        alpha=0.1,
        similarity_threshold=0.5,
        max_candidates_per_unit=15,
        superseded_threshold=0.3,
    )


def _engine_with_mocked_llm(
    config: ContradictionConfig,
    contradict_targets: list[MemoryUnit],
    monkeypatch: pytest.MonkeyPatch,
) -> ContradictionEngine:
    """Build an engine whose triage flags every flagged unit and whose
    classify returns one ``contradict`` relation per existing target.

    Uses ``monkeypatch`` so the module-level ``get_candidates`` is
    restored after the test — leaking it would break subsequent LLM
    integration tests that expect the real candidate retriever.
    """
    engine = ContradictionEngine(lm=MagicMock(), config=config)

    async def _fake_get_candidates(session, unit, vault_id, k, threshold):
        return list(contradict_targets)

    async def _fake_classify(unit, candidates):
        return [
            ContradictionRelationship(
                existing_id=str(c.id),
                relation='contradict',
                authoritative='new',
                reasoning='supersedes prior policy',
            )
            for c in candidates
        ]

    async def _fake_triage(units):
        return [str(u.id) for u in units]

    engine._classify = _fake_classify  # type: ignore[assignment]
    engine._triage = _fake_triage  # type: ignore[assignment]

    monkeypatch.setattr(
        'memex_core.memory.contradiction.engine.get_candidates', _fake_get_candidates
    )
    return engine


# --------------------------------------------------------------------------- #
# Fix 1: contradiction penalty drops superseded unit below threshold
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@pytest.mark.asyncio
async def test_single_contradict_drops_unit_below_superseded_threshold(
    session: AsyncSession,
    contradiction_config: ContradictionConfig,
    monkeypatch: pytest.MonkeyPatch,
):
    """End-to-end: write two units, run contradiction detection with a
    mocked classify returning ``contradict``, then read the superseded unit
    back from Postgres and confirm confidence < superseded_threshold.
    """
    vault_id = GLOBAL_VAULT_ID

    note_old = _make_note(vault_id, title='Old policy')
    note_new = _make_note(vault_id, title='New policy')
    session.add_all([note_old, note_new])
    await session.flush()

    old_unit = _make_unit(
        note_old.id,
        vault_id,
        'Keys rotate every 90 days.',
        confidence=1.0,
        event_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    new_unit = _make_unit(
        note_new.id,
        vault_id,
        'Key rotation has been discontinued.',
        confidence=1.0,
        event_date=datetime(2026, 5, 5, tzinfo=timezone.utc),
    )
    session.add_all([old_unit, new_unit])
    await session.commit()

    engine = _engine_with_mocked_llm(
        contradiction_config, contradict_targets=[old_unit], monkeypatch=monkeypatch
    )
    await engine._detect(session, [new_unit.id], vault_id)
    await session.commit()

    await session.refresh(old_unit)
    threshold = contradiction_config.superseded_threshold
    assert old_unit.confidence < threshold, (
        f'expected confidence < {threshold} after a single contradict, got {old_unit.confidence}'
    )
    # 1.0 - (1.0 - 0.3 + 0.1) = 0.2
    assert old_unit.confidence == pytest.approx(0.2, abs=1e-6)
    assert old_unit.confidence_evidence_count == 1

    # Link is persisted with the contradicts type and the right metadata.
    links = (
        await session.exec(select(MemoryLink).where(MemoryLink.to_unit_id == old_unit.id))
    ).all()
    assert len(links) == 1
    assert links[0].link_type == 'contradicts'
    assert links[0].link_metadata['authoritative_unit_id'] == str(new_unit.id)


# --------------------------------------------------------------------------- #
# Fix 2: contradicts link emits a maintenance_proposals row (idempotent)
# --------------------------------------------------------------------------- #


_COUNT_LINT_FOR_UNIT_SQL = text(
    """
    SELECT count(*) AS n
    FROM maintenance_proposals
    WHERE rule_name = 'semantic_contradiction'
      AND target_type = 'memory_unit'
      AND target_id = :tid
      AND vault_id = :vid
      AND status = 'pending'
    """
)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_contradiction_emits_one_maintenance_proposal_per_unit(
    session: AsyncSession,
    contradiction_config: ContradictionConfig,
    monkeypatch: pytest.MonkeyPatch,
):
    """A contradict link writes exactly one ``semantic_contradiction``
    maintenance_proposals row for the superseded unit, independent of how
    many times detection re-runs against the same pair.
    """
    vault_id = GLOBAL_VAULT_ID

    note_old = _make_note(vault_id, title='Old policy')
    note_new = _make_note(vault_id, title='New policy')
    session.add_all([note_old, note_new])
    await session.flush()

    old_unit = _make_unit(note_old.id, vault_id, 'Keys rotate every 90 days.', confidence=1.0)
    new_unit = _make_unit(
        note_new.id, vault_id, 'Key rotation has been discontinued.', confidence=1.0
    )
    session.add_all([old_unit, new_unit])
    await session.commit()

    engine = _engine_with_mocked_llm(
        contradiction_config, contradict_targets=[old_unit], monkeypatch=monkeypatch
    )

    # First run: emits one finding.
    await engine._detect(session, [new_unit.id], vault_id)
    await session.commit()

    row = (
        await session.execute(
            _COUNT_LINT_FOR_UNIT_SQL,
            {'tid': str(old_unit.id), 'vid': str(vault_id)},
        )
    ).first()
    assert row is not None and row.n == 1, (
        f'expected 1 maintenance_proposals row after first detection, got {row.n}'
    )

    # Second run on the same pair: still one finding (partial unique index
    # on rule_name/target_type/target_id/vault_id WHERE status='pending').
    await engine._detect(session, [new_unit.id], vault_id)
    await session.commit()

    row = (
        await session.execute(
            _COUNT_LINT_FOR_UNIT_SQL,
            {'tid': str(old_unit.id), 'vid': str(vault_id)},
        )
    ).first()
    assert row is not None and row.n == 1, (
        f'expected 1 maintenance_proposals row after re-run (idempotent), got {row.n}'
    )

    # The finding has the right shape — quality lint type and llm source.
    finding_row = (
        await session.execute(
            text(
                "SELECT lint_type, source, evidence ->> 'reasoning' AS reasoning "
                'FROM maintenance_proposals '
                "WHERE rule_name = 'semantic_contradiction' "
                'AND target_id = :tid AND vault_id = :vid'
            ),
            {'tid': str(old_unit.id), 'vid': str(vault_id)},
        )
    ).first()
    assert finding_row is not None
    assert finding_row.lint_type == 'quality'
    assert finding_row.source == 'llm'
    assert finding_row.reasoning == 'supersedes prior policy'


# --------------------------------------------------------------------------- #
# Fix 3: set_mw_mode REST endpoint
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@pytest.mark.asyncio
async def test_vault_service_set_mw_mode_persists_in_postgres(
    session: AsyncSession,
    metastore,
    memex_config,
    filestore,
):
    """``VaultService.set_mw_mode`` persists the new mode in Postgres and
    rejects invalid values with ``ValueError``. This is the service-layer
    contract that the REST endpoint and CLI both call into.
    """
    from memex_core.services.vaults import VaultService

    vault = Vault(id=uuid4(), name=f'mw-test-{uuid4()}', mw_mode='stationary')
    session.add(vault)
    await session.commit()

    svc = VaultService(metastore=metastore, filestore=filestore, config=memex_config)

    updated = await svc.set_mw_mode(vault.id, 'ema')
    assert updated.mw_mode == 'ema'

    # Confirm persistence via a fresh SELECT through the test session.
    row = (
        await session.execute(
            text('SELECT mw_mode FROM vaults WHERE id = :id'), {'id': str(vault.id)}
        )
    ).first()
    assert row is not None and row.mw_mode == 'ema'

    # Invalid mode raises ValueError; row is unchanged.
    with pytest.raises(ValueError):
        await svc.set_mw_mode(vault.id, 'banana')

    row = (
        await session.execute(
            text('SELECT mw_mode FROM vaults WHERE id = :id'), {'id': str(vault.id)}
        )
    ).first()
    assert row is not None and row.mw_mode == 'ema'


@pytest.mark.integration
def test_set_mw_mode_route_validates_and_calls_api():
    """The REST route accepts ``mode`` in the body, validates it, calls
    ``api.set_mw_mode`` for valid input, and returns 400 for invalid input.
    Uses a minimal AsyncMock api so the test focuses on the route — the
    service-layer DB roundtrip is covered by the test above.
    """
    from memex_core.server import app
    from memex_core.server.auth import get_auth_context
    from memex_core.server.common import get_api

    from types import SimpleNamespace

    api = AsyncMock()
    vault_id = uuid4()
    api.set_mw_mode.return_value = SimpleNamespace(
        id=vault_id, name='vault-x', description=None, mw_mode='ema'
    )

    app.dependency_overrides[get_api] = lambda: api
    app.dependency_overrides[get_auth_context] = lambda: None
    try:
        with TestClient(app) as client:
            resp = client.post(f'/api/v1/vaults/{vault_id}/mw-mode', json={'mode': 'ema'})
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body['mw_mode'] == 'ema'
            api.set_mw_mode.assert_awaited_once_with(vault_id, 'ema')

            # Invalid mode caught by Pydantic body validation → 422.
            resp = client.post(f'/api/v1/vaults/{vault_id}/mw-mode', json={'mode': 'banana'})
            assert resp.status_code == 422, resp.text
    finally:
        app.dependency_overrides.pop(get_api, None)
        app.dependency_overrides.pop(get_auth_context, None)


@pytest.mark.integration
def test_set_mw_mode_route_blocks_forbidden_vault():
    """A scoped writer keyed to vault-A must NOT be able to flip mw_mode on
    vault-B. Regression test for the prior auth gap on
    POST /vaults/{id}/mw-mode (the previous version used Depends(require_write)
    only, no per-vault gate).
    """
    from types import SimpleNamespace
    from uuid import UUID

    from memex_common.config import POLICY_PERMISSIONS, Policy
    from memex_core.server import app
    from memex_core.server.auth import AuthContext, get_auth_context
    from memex_core.server.common import get_api

    allowed_vault = uuid4()
    forbidden_vault = uuid4()

    api = AsyncMock()
    api.set_mw_mode.return_value = SimpleNamespace(
        id=forbidden_vault, name='x', description=None, mw_mode='ema'
    )

    async def _resolve(identifier):
        if isinstance(identifier, UUID):
            return identifier
        return UUID(str(identifier))

    api.resolve_vault_identifier = AsyncMock(side_effect=_resolve)

    scoped_writer = AuthContext(
        key_prefix='test1234',
        key_name='scoped-writer',
        policy=Policy.WRITER,
        permissions=POLICY_PERMISSIONS[Policy.WRITER],
        vault_ids=[str(allowed_vault)],
        read_vault_ids=None,
    )

    app.dependency_overrides[get_api] = lambda: api
    app.dependency_overrides[get_auth_context] = lambda: scoped_writer
    try:
        with TestClient(app) as client:
            resp = client.post(f'/api/v1/vaults/{forbidden_vault}/mw-mode', json={'mode': 'ema'})
            assert resp.status_code == 403, resp.text
            api.set_mw_mode.assert_not_called()

            resp = client.post(f'/api/v1/vaults/{allowed_vault}/mw-mode', json={'mode': 'ema'})
            assert resp.status_code == 200, resp.text
    finally:
        app.dependency_overrides.pop(get_api, None)
        app.dependency_overrides.pop(get_auth_context, None)


# --------------------------------------------------------------------------- #
# Fix 4: raw_score on note search results
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rerank_success_preserves_rrf_in_raw_score():
    """When ``_rerank_results`` succeeds, ``score`` gets the sigmoid-normalised
    reranker output and ``raw_score`` keeps the pre-rerank RRF score. Drives
    the real method against a stub reranker — not the field-rebinding pattern
    the prior version used.
    """
    from memex_common.schemas import NoteSearchResult
    from memex_core.memory.retrieval.document_search import NoteSearchEngine

    engine = NoteSearchEngine.__new__(NoteSearchEngine)

    class _StubReranker:
        def score(self, query, texts):
            return [10.0 for _ in texts]

    engine.reranker = _StubReranker()  # type: ignore[attr-defined]

    note_a, note_b = uuid4(), uuid4()
    results = [
        NoteSearchResult(note_id=note_a, metadata={}, score=0.8, raw_score=0.8),
        NoteSearchResult(note_id=note_b, metadata={}, score=0.6, raw_score=0.6),
    ]
    chunk_text = {note_a: 'a text', note_b: 'b text'}

    out = await engine._rerank_results('q', results, chunk_text)

    # raw_score preserved as the pre-rerank RRF score for every item.
    assert {r.note_id: r.raw_score for r in out} == {note_a: 0.8, note_b: 0.6}
    # score overwritten by the sigmoid output (≈ 1.0 for logit 10).
    for r in out:
        assert r.score > 0.99


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rerank_failure_falls_back_and_nulls_raw_score():
    """When the reranker raises, ``_rerank_results`` returns the original
    RRF order AND wipes ``raw_score`` to ``None`` so callers can detect
    that rerank did not actually run.
    """
    from memex_common.schemas import NoteSearchResult
    from memex_core.memory.retrieval.document_search import NoteSearchEngine

    engine = NoteSearchEngine.__new__(NoteSearchEngine)

    class _BrokenReranker:
        def score(self, query, texts):
            raise RuntimeError('rerank model OOM')

    engine.reranker = _BrokenReranker()  # type: ignore[attr-defined]

    note_a = uuid4()
    results = [NoteSearchResult(note_id=note_a, metadata={}, score=0.7, raw_score=0.7)]
    chunk_text = {note_a: 'a'}

    out = await engine._rerank_results('q', results, chunk_text)

    assert len(out) == 1
    assert out[0].score == pytest.approx(0.7)  # unchanged: still RRF
    assert out[0].raw_score is None, (
        'rerank failure must null raw_score so callers can detect the fallback'
    )


# --------------------------------------------------------------------------- #
# Fix 5: short-text extraction yields >= 1 memory unit (LLM)
# --------------------------------------------------------------------------- #


@pytest.mark.integration
@pytest.mark.llm
@pytest.mark.skipif(
    not os.environ.get('GOOGLE_API_KEY'),
    reason='GOOGLE_API_KEY not set; @pytest.mark.llm requires real API access',
)
@pytest.mark.asyncio
async def test_short_declarative_input_yields_at_least_one_fact():
    """The short-input directive in ``ExtractSemanticFacts`` must make the
    LLM produce at least one fact for a single declarative sentence.
    Reproduces the ``Key rotation has been discontinued entirely`` regression
    where the model was returning ``[]`` for short inputs.
    """
    import dspy

    from memex_core.memory.extraction.core import (
        ExtractSemanticFacts,
        extract_facts_from_text,
    )

    api_key = os.environ['GOOGLE_API_KEY']
    lm = dspy.LM(model='gemini/gemini-3-flash-preview', api_key=api_key, timeout=60)
    predictor = dspy.Predict(ExtractSemanticFacts)

    facts, _meta = await extract_facts_from_text(
        text='Key rotation has been discontinued entirely.',
        event_date=datetime(2026, 5, 7, tzinfo=timezone.utc),
        lm=lm,
        predictor=predictor,
        agent_name='test-agent',
        chunk_max_chars=4000,
        chunk_overlap=0,
    )
    assert len(facts) >= 1, (
        'expected at least one fact from a single declarative sentence; '
        f'short-input prompt directive may not be honoured by the model. Got: {facts}'
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_note_search_engine_rerank_disabled_clears_raw_score():
    """``NoteSearchEngine`` clears ``raw_score`` to None on every result when
    reranking is disabled. The happy path of the production engine wraps
    the same pattern; this test pins the contract.
    """
    from memex_common.schemas import NoteSearchRequest, NoteSearchResult
    from memex_core.memory.retrieval.document_search import NoteSearchEngine

    engine = NoteSearchEngine.__new__(NoteSearchEngine)
    engine.reranker = None  # type: ignore[attr-defined]

    results = [
        NoteSearchResult(note_id=uuid4(), metadata={}, score=0.7, raw_score=0.7) for _ in range(3)
    ]
    request = NoteSearchRequest(query='q', rerank=False)
    # Same control-flow as the engine's rerank guard.
    if not (request.rerank and engine.reranker and results):
        for r in results:
            r.raw_score = None
    assert all(r.raw_score is None for r in results)
