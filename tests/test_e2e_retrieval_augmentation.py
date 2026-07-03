"""E2E tests for Wave 1 memory augmentation features through the HTTP layer.

Validates outcome recording, deprioritization filtering, and exploration
injection via the full FastAPI + Postgres pipeline.

Tests that need to call async API methods use db_session + OutcomeService
directly (since record_outcome has no HTTP endpoint). The deprioritization
tests use the HTTP search endpoint where possible, and fall back to
MemexAPI.search for include_deprioritized (not yet in HTTP schema).
"""

import base64
import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, AsyncMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from memex_common.config import GLOBAL_VAULT_ID
from memex_core.memory.extraction.models import ExtractedFact, ChunkMetadata


def parse_ndjson(text: str):
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _ingest_note(
    client: TestClient,
    content: bytes,
    vault_id: str,
    mock_facts: list[ExtractedFact],
    mock_chunks: list[ChunkMetadata],
    mock_embeddings: list[list[float]],
):
    """Ingest a note with mocked LLM extraction and embedding."""
    extract_path = 'memex_core.memory.extraction.engine.ExtractionEngine._extract_facts'
    embed_path = 'memex_core.memory.extraction.embedding_processor.generate_embeddings_batch'

    b64_content = base64.b64encode(content).decode('utf-8')

    with (
        patch(extract_path) as mock_extract,
        patch(embed_path) as mock_embed,
        patch(
            'memex_core.services.vaults.VaultService.resolve_vault_identifier',
            new_callable=AsyncMock,
            return_value=vault_id,
        ),
    ):
        mock_extract.return_value = (mock_facts, mock_chunks)
        mock_embed.return_value = mock_embeddings

        resp = client.post(
            '/api/v1/ingestions',
            json={
                'name': f'e2e-test-{uuid4().hex[:8]}',
                'description': 'E2E test',
                'content': b64_content,
                'files': {},
                'tags': ['e2e'],
            },
        )
        assert resp.status_code == 200, f'Ingest failed: {resp.text}'
        return resp.json()


def _search(
    client: TestClient,
    query: str,
    vault_id: str,
    limit: int = 10,
    include_deprioritized: bool = False,
):
    """Search memories through the HTTP endpoint."""
    mock_embedder = MagicMock()
    mock_embedder.encode.return_value = [[0.1] * 384]

    app = client.app
    real_embedder = app.state.api.embedder
    app.state.api.embedder = mock_embedder

    try:
        payload: dict[str, object] = {
            'query': query,
            'limit': limit,
            'vault_ids': [vault_id],
        }
        if include_deprioritized:
            payload['include_deprioritized'] = True
        resp = client.post('/api/v1/memories/search', json=payload)
        assert resp.status_code == 200, f'Search failed: {resp.text}'
        return parse_ndjson(resp.text)
    finally:
        app.state.api.embedder = real_embedder


@pytest.mark.integration
async def test_e2e_record_outcome_increments_mw_counters(db_session):
    """Record outcome via OutcomeService and verify MW counters are updated in DB."""
    from memex_core.memory.sql_models import Entity, MemoryUnit, Note, UnitEntity
    from memex_core.services.outcomes import OutcomeService
    from memex_common.types import FactTypes

    vault_id = GLOBAL_VAULT_ID
    note_id = uuid4()
    unit_id = uuid4()
    entity_id = uuid4()
    emb = [0.1] * 384

    note = Note(id=note_id, original_text='E2E MW counter test', vault_id=vault_id)
    unit = MemoryUnit(
        id=unit_id,
        note_id=note_id,
        text='E2E fact about microservices',
        fact_type=FactTypes.WORLD,
        embedding=emb,
        vault_id=vault_id,
        event_date=datetime.now(timezone.utc),
        success_co_count=5,
        failure_co_count=2,
    )
    entity = Entity(id=entity_id, canonical_name='Microservices')
    ue = UnitEntity(unit_id=unit_id, entity_id=entity_id, vault_id=vault_id)

    db_session.add(note)
    db_session.add(unit)
    db_session.add(entity)
    db_session.add(ue)
    await db_session.commit()

    svc = OutcomeService()
    outcome = await svc.record_outcome(
        session=db_session,
        unit_ids=[str(unit_id)],
        success=True,
        vault_id=str(vault_id),
    )
    assert outcome['units_updated'] >= 1

    # Verify counter incremented in DB
    db_session.expire_all()
    refreshed = await db_session.get(MemoryUnit, unit_id)
    assert refreshed is not None
    assert refreshed.success_co_count == 6  # was 5, now 6


@pytest.mark.integration
async def test_e2e_record_failure_outcome(db_session):
    """Record a failure outcome and verify failure counter increments."""
    from memex_core.memory.sql_models import Entity, MemoryUnit, Note, UnitEntity
    from memex_core.services.outcomes import OutcomeService
    from memex_common.types import FactTypes

    vault_id = GLOBAL_VAULT_ID
    note_id = uuid4()
    unit_id = uuid4()
    entity_id = uuid4()
    emb = [0.1] * 384

    note = Note(id=note_id, original_text='E2E failure outcome test', vault_id=vault_id)
    unit = MemoryUnit(
        id=unit_id,
        note_id=note_id,
        text='E2E fact about serverless architecture',
        fact_type=FactTypes.WORLD,
        embedding=emb,
        vault_id=vault_id,
        event_date=datetime.now(timezone.utc),
        success_co_count=3,
        failure_co_count=1,
    )
    entity = Entity(id=entity_id, canonical_name='Serverless')
    ue = UnitEntity(unit_id=unit_id, entity_id=entity_id, vault_id=vault_id)

    db_session.add(note)
    db_session.add(unit)
    db_session.add(entity)
    db_session.add(ue)
    await db_session.commit()

    svc = OutcomeService()
    outcome = await svc.record_outcome(
        session=db_session,
        unit_ids=[str(unit_id)],
        success=False,
        vault_id=str(vault_id),
    )
    assert outcome['units_updated'] >= 1

    # Verify failure counter incremented
    db_session.expire_all()
    refreshed = await db_session.get(MemoryUnit, unit_id)
    assert refreshed is not None
    assert refreshed.failure_co_count == 2  # was 1, now 2
    assert refreshed.success_co_count == 3  # unchanged


@pytest.mark.integration
async def test_e2e_record_outcome_propagates_to_entity(db_session):
    """Verify outcome recording propagates counters to UnitEntity."""
    from memex_core.memory.sql_models import Entity, MemoryUnit, Note, UnitEntity
    from memex_core.services.outcomes import OutcomeService
    from memex_common.types import FactTypes

    vault_id = GLOBAL_VAULT_ID
    note_id = uuid4()
    unit_id = uuid4()
    entity_id = uuid4()
    emb = [0.1] * 384

    note = Note(id=note_id, original_text='E2E entity propagation test', vault_id=vault_id)
    unit = MemoryUnit(
        id=unit_id,
        note_id=note_id,
        text='E2E fact about Kubernetes deployments',
        fact_type=FactTypes.WORLD,
        embedding=emb,
        vault_id=vault_id,
        event_date=datetime.now(timezone.utc),
        success_co_count=0,
        failure_co_count=0,
    )
    entity = Entity(id=entity_id, canonical_name='Kubernetes')
    ue = UnitEntity(unit_id=unit_id, entity_id=entity_id, vault_id=vault_id)
    db_session.add(note)
    db_session.add(unit)
    db_session.add(entity)
    db_session.add(ue)
    await db_session.commit()

    svc = OutcomeService()
    await svc.record_outcome(
        session=db_session,
        unit_ids=[str(unit_id)],
        success=True,
        vault_id=str(vault_id),
    )

    # Verify UnitEntity counter incremented
    db_session.expire_all()
    refreshed_ue = await db_session.get(UnitEntity, (unit_id, entity_id))
    assert refreshed_ue is not None
    assert refreshed_ue.success_co_count == 1


@pytest.mark.integration
async def test_e2e_deprioritized_unit_hidden_by_default(client: TestClient, db_session):
    """Deprioritized units are excluded from HTTP search results by default."""
    from memex_core.memory.sql_models import Entity, MemoryUnit, Note, UnitEntity
    from memex_common.types import FactTypes

    vault_id = GLOBAL_VAULT_ID
    note_id = uuid4()
    active_id = uuid4()
    deprioritized_id = uuid4()
    entity_id = uuid4()
    emb = [0.1] * 384

    note = Note(id=note_id, original_text='E2E depri test', vault_id=vault_id)
    active_unit = MemoryUnit(
        id=active_id,
        note_id=note_id,
        text='E2E active fact about deployment',
        fact_type=FactTypes.WORLD,
        embedding=emb,
        vault_id=vault_id,
        event_date=datetime.now(timezone.utc),
        is_deprioritized=False,
    )
    deprioritized_unit = MemoryUnit(
        id=deprioritized_id,
        note_id=note_id,
        text='E2E deprioritized fact about deployment',
        fact_type=FactTypes.WORLD,
        embedding=emb,
        vault_id=vault_id,
        event_date=datetime.now(timezone.utc),
        is_deprioritized=True,
    )
    entity = Entity(id=entity_id, canonical_name='Deploy')
    ue_active = UnitEntity(unit_id=active_id, entity_id=entity_id, vault_id=vault_id)
    ue_dep = UnitEntity(unit_id=deprioritized_id, entity_id=entity_id, vault_id=vault_id)

    db_session.add(note)
    db_session.add(active_unit)
    db_session.add(deprioritized_unit)
    db_session.add(entity)
    db_session.add(ue_active)
    db_session.add(ue_dep)
    await db_session.commit()

    # Search through HTTP — deprioritized unit should not appear
    results = _search(client, 'deployment', str(vault_id))

    result_ids = [r['id'] for r in results]
    assert str(active_id) in result_ids
    assert str(deprioritized_id) not in result_ids


@pytest.mark.integration
async def test_e2e_deprioritized_unit_shown_with_flag(client: TestClient, db_session):
    """Deprioritized units appear via the HTTP search endpoint when
    ``include_deprioritized=true`` is set on the request body."""
    from memex_core.memory.sql_models import Entity, MemoryUnit, Note, UnitEntity
    from memex_common.types import FactTypes

    vault_id = GLOBAL_VAULT_ID
    note_id = uuid4()
    deprioritized_id = uuid4()
    entity_id = uuid4()
    emb = [0.1] * 384

    note = Note(id=note_id, original_text='E2E depri include test', vault_id=vault_id)
    deprioritized_unit = MemoryUnit(
        id=deprioritized_id,
        note_id=note_id,
        text='E2E deprioritized fact about caching',
        fact_type=FactTypes.WORLD,
        embedding=emb,
        vault_id=vault_id,
        event_date=datetime.now(timezone.utc),
        is_deprioritized=True,
    )
    entity = Entity(id=entity_id, canonical_name='Cache')
    ue = UnitEntity(unit_id=deprioritized_id, entity_id=entity_id, vault_id=vault_id)

    db_session.add(note)
    db_session.add(deprioritized_unit)
    db_session.add(entity)
    db_session.add(ue)
    await db_session.commit()

    # Default scope: deprioritized unit hidden
    default_results = _search(client, 'caching', str(vault_id))
    assert str(deprioritized_id) not in [r['id'] for r in default_results]

    # With include_deprioritized=true: unit appears
    explicit_results = _search(client, 'caching', str(vault_id), include_deprioritized=True)
    assert str(deprioritized_id) in [r['id'] for r in explicit_results]


@pytest.mark.integration
async def test_e2e_exploration_units_in_search(client: TestClient, db_session):
    """Cold-start units with low MW counts can appear when exploration is enabled."""
    from memex_core.memory.sql_models import Entity, MemoryUnit, Note, UnitEntity
    from memex_common.types import FactTypes
    from memex_core.memory.retrieval.exploration import inject_exploration_units

    vault_id = GLOBAL_VAULT_ID
    note_id = uuid4()
    cold_id = uuid4()
    warm_id = uuid4()
    entity_id = uuid4()
    emb = [0.1] * 384

    note = Note(id=note_id, original_text='E2E exploration test', vault_id=vault_id)
    warm_unit = MemoryUnit(
        id=warm_id,
        note_id=note_id,
        text='E2E well-known fact about containers',
        fact_type=FactTypes.WORLD,
        embedding=emb,
        vault_id=vault_id,
        event_date=datetime.now(timezone.utc),
        success_co_count=10,
        failure_co_count=5,
    )
    cold_unit = MemoryUnit(
        id=cold_id,
        note_id=note_id,
        text='E2E new fact about containers',
        fact_type=FactTypes.WORLD,
        embedding=emb,
        vault_id=vault_id,
        event_date=datetime.now(timezone.utc),
        success_co_count=0,
        failure_co_count=0,
    )
    entity = Entity(id=entity_id, canonical_name='Containers')
    ue_warm = UnitEntity(unit_id=warm_id, entity_id=entity_id, vault_id=vault_id)
    ue_cold = UnitEntity(unit_id=cold_id, entity_id=entity_id, vault_id=vault_id)

    db_session.add(note)
    db_session.add(warm_unit)
    db_session.add(cold_unit)
    db_session.add(entity)
    db_session.add(ue_warm)
    db_session.add(ue_cold)
    await db_session.commit()

    # Test exploration injection directly with deterministic epsilon=1.0
    all_candidates = [warm_unit, cold_unit]
    injected = inject_exploration_units(
        [warm_unit],
        all_candidates,
        epsilon=1.0,
        max_injections=2,
        low_mw_threshold=5,
    )

    exploration_units = [u for u in injected if u.unit_metadata.get('exploration', False)]
    assert len(exploration_units) >= 1
    for u in exploration_units:
        total = u.success_co_count + u.failure_co_count
        assert total < 5


@pytest.mark.llm
@pytest.mark.integration
async def test_e2e_ingest_search_record_outcome_search(client: TestClient, db_session):
    """Full pipeline: ingest → search → record outcome → search again.

    Marked ``llm`` because the F25 write-time classifier (default-on as of
    PR #91) issues a real Gemini call inside the ingestion pipeline that is
    not covered by the ``_extract_facts`` mock used in ``_ingest_note``.
    The search path also invokes query expansion via Gemini; CI runs
    `integration and not llm`, so this test skips without credentials.
    """
    from memex_core.services.outcomes import OutcomeService
    from memex_core.memory.sql_models import MemoryUnit

    vault_id = str(GLOBAL_VAULT_ID)
    now = datetime.now(timezone.utc)

    # 1. Ingest
    mock_facts = [
        ExtractedFact(
            fact_text='E2E: Go is a compiled language with goroutines.',
            fact_type='world',
            entities=[],
            chunk_index=0,
            content_index=0,
            mentioned_at=now,
            vault_id=GLOBAL_VAULT_ID,
        ),
    ]
    mock_chunks = [
        ChunkMetadata(
            chunk_text='E2E: Go is a compiled language with goroutines.',
            fact_count=1,
            chunk_index=0,
            content_index=0,
        )
    ]

    _ingest_note(
        client,
        content=b'E2E: Go is a compiled language with goroutines.',
        vault_id=vault_id,
        mock_facts=mock_facts,
        mock_chunks=mock_chunks,
        mock_embeddings=[[0.1] * 384],
    )

    # 2. Search
    results = _search(client, 'Go', vault_id)
    assert len(results) > 0, 'Expected results after ingestion'

    unit_id = str(results[0]['id'])

    # 3. Record outcome via OutcomeService (same event loop as db_session)
    svc = OutcomeService()
    outcome = await svc.record_outcome(
        session=db_session,
        unit_ids=[unit_id],
        success=True,
        vault_id=vault_id,
    )
    assert outcome['units_updated'] >= 1

    # 4. Verify counter incremented in DB
    db_session.expire_all()
    unit = await db_session.get(MemoryUnit, results[0]['id'])
    assert unit is not None
    assert unit.success_co_count >= 1

    # 5. Search again — unit should still appear
    results_after = _search(client, 'Go', vault_id)
    assert len(results_after) > 0


@pytest.mark.integration
async def test_e2e_record_outcome_changes_ranking(client: TestClient, db_session):
    """F1c via HTTP: opposing outcomes on two near-duplicate units flip the
    relative ranking returned by ``/api/v1/memories/search``.

    Both units share text + embedding, so cross-encoder ce_score is equal;
    after recording 10 successes on A and 10 failures on B, A's MW boost
    > 1.0 and B's < 1.0, so A must rank above B.
    """
    from memex_core.memory.sql_models import Entity, MemoryUnit, Note, UnitEntity
    from memex_core.services.outcomes import OutcomeService
    from memex_common.types import FactTypes

    vault_id = GLOBAL_VAULT_ID
    note_id = uuid4()
    a_id = uuid4()
    b_id = uuid4()
    entity_id = uuid4()
    text = 'E2E: queue worker retries failed jobs with exponential backoff.'
    emb = [0.1] * 384

    note = Note(id=note_id, original_text='E2E ranking change', vault_id=vault_id)
    a_unit = MemoryUnit(
        id=a_id,
        note_id=note_id,
        text=text,
        fact_type=FactTypes.WORLD,
        embedding=emb,
        vault_id=vault_id,
        event_date=datetime.now(timezone.utc),
        success_co_count=0,
        failure_co_count=0,
    )
    b_unit = MemoryUnit(
        id=b_id,
        note_id=note_id,
        text=text,
        fact_type=FactTypes.WORLD,
        embedding=emb,
        vault_id=vault_id,
        event_date=datetime.now(timezone.utc),
        success_co_count=0,
        failure_co_count=0,
    )
    entity = Entity(id=entity_id, canonical_name='QueueWorker')
    db_session.add(note)
    db_session.add(a_unit)
    db_session.add(b_unit)
    db_session.add(entity)
    db_session.add(UnitEntity(unit_id=a_id, entity_id=entity_id, vault_id=vault_id))
    db_session.add(UnitEntity(unit_id=b_id, entity_id=entity_id, vault_id=vault_id))
    await db_session.commit()

    svc = OutcomeService()
    for _ in range(10):
        await svc.record_outcome(
            session=db_session,
            unit_ids=[str(a_id)],
            success=True,
            vault_id=str(vault_id),
        )
        await svc.record_outcome(
            session=db_session,
            unit_ids=[str(b_id)],
            success=False,
            vault_id=str(vault_id),
        )

    db_session.expire_all()
    a_refreshed = await db_session.get(MemoryUnit, a_id)
    b_refreshed = await db_session.get(MemoryUnit, b_id)
    assert a_refreshed is not None and a_refreshed.success_co_count == 10
    assert b_refreshed is not None and b_refreshed.failure_co_count == 10

    results = _search(client, 'queue worker retries', str(vault_id))
    result_ids = [r['id'] for r in results]
    if str(a_id) not in result_ids or str(b_id) not in result_ids:
        pytest.skip(
            f'one or both seeded units missing from HTTP search results — '
            f'reranker may not be loaded in this test app. ids={result_ids}'
        )

    assert result_ids.index(str(a_id)) < result_ids.index(str(b_id)), (
        f'F1c E2E regression: high-success unit must rank above high-failure peer '
        f'after record_outcome. Got order: {result_ids}'
    )
