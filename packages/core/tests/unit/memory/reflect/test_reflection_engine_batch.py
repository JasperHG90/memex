import pytest
from unittest.mock import MagicMock, AsyncMock
from uuid import uuid4
from sqlmodel.ext.asyncio.session import AsyncSession
from memex_core.memory.reflect.reflection import ReflectionEngine
from memex_core.memory.sql_models import MemoryUnit
from memex_core.config import MemexConfig


@pytest.fixture
def mock_session():
    session = AsyncMock(spec=AsyncSession)
    session.exec = AsyncMock()
    return session


@pytest.fixture
def engine(mock_session):
    mock_config = MagicMock(spec=MemexConfig)
    return ReflectionEngine(session=mock_session, config=mock_config, embedder=MagicMock())


@pytest.fixture
def engine_relaxed_config(mock_session):
    """Engine with a real default MemexConfig so deep attribute access works
    (e.g. ``config.server.memory.reflection.max_concurrency``). The defaults
    leave the reflection model as None, so the LM-init path is skipped.
    """
    return ReflectionEngine(session=mock_session, config=MemexConfig(), embedder=MagicMock())


@pytest.mark.asyncio
async def test_batch_fetch_recent_memories_sql_structure(engine_relaxed_config, mock_session):
    """
    Verify that _batch_fetch_recent_memories constructs a valid query
    and handles the result grouping correctly.

    Uses the relaxed_config fixture so the F22 variance-prioritisation
    flag check (``config.server.memory.reflection.variance_prioritisation_enabled``,
    Hermes round-10 HIGH) doesn't trip the MagicMock spec — the default
    is False so the sort branch stays inert.
    """
    engine = engine_relaxed_config
    entity_ids = [uuid4(), uuid4()]

    # Mock the DB response
    # Format: [(MemoryUnit, entity_id), ...]
    unit1 = MemoryUnit(text='U1')
    unit2 = MemoryUnit(text='U2')

    mock_result = MagicMock()
    mock_result.all.return_value = [
        (unit1, entity_ids[0]),
        (unit2, entity_ids[0]),
        (unit1, entity_ids[1]),  # Shared unit case
    ]
    mock_session.exec.return_value = mock_result

    # Execute
    result_map = await engine._batch_fetch_recent_memories(entity_ids, limit_per_entity=5)

    # Verify Grouping
    assert len(result_map[entity_ids[0]]) == 2
    assert len(result_map[entity_ids[1]]) == 1

    # Verify SQL execution happened
    mock_session.exec.assert_called_once()

    # Inspect the call args to sanity check logic (hard to verify exact SQL string with mocks,
    # but we check if it didn't crash during construction)


@pytest.mark.asyncio
async def test_batch_fetch_recent_memories_unbounded_caps_at_max_full_scope(
    engine_relaxed_config, mock_session
):
    engine = engine_relaxed_config
    """TC3 (CONCERN-2): limit_per_entity=None caps at MAX_FULL_SCOPE_UNITS in SQL.

    The outer query's `WHERE rn <= effective_limit` clause must use 1000
    when the caller requests 'no per-request cap' (F5 scope='full').
    """
    from memex_core.memory.reflect.reflection import MAX_FULL_SCOPE_UNITS

    mock_result = MagicMock()
    mock_result.all.return_value = []
    mock_session.exec.return_value = mock_result

    await engine._batch_fetch_recent_memories([uuid4()], limit_per_entity=None)

    mock_session.exec.assert_called_once()
    query = mock_session.exec.call_args.args[0]
    compiled = query.compile(compile_kwargs={'literal_binds': True})
    rendered = str(compiled).replace('\n', ' ')
    assert f'rn <= {MAX_FULL_SCOPE_UNITS}' in rendered, (
        f'expected SQL to bind rn <= {MAX_FULL_SCOPE_UNITS}; got: {rendered}'
    )


@pytest.mark.asyncio
async def test_reflect_batch_derives_max_limit_for_mixed_batch(engine_relaxed_config, mock_session):
    engine = engine_relaxed_config
    """TC2.5 (CONCERN-1): mixed-limit batch picks the most-permissive limit.

    Two requests in the same batch, one with limit=20 and one with limit=None,
    must produce a single SQL fetch with the unbounded path
    (which is then capped at MAX_FULL_SCOPE_UNITS by the engine).
    """
    from memex_core.memory.reflect.models import ReflectionRequest
    from memex_core.memory.reflect.reflection import MAX_FULL_SCOPE_UNITS

    eid_a, eid_b = uuid4(), uuid4()
    requests = [
        ReflectionRequest(entity_id=eid_a, limit_recent_memories=20),
        ReflectionRequest(entity_id=eid_b, limit_recent_memories=None),
    ]

    captured_limits: list[int | None] = []

    async def _fake_fetch(entity_ids, vault_id=None, limit_per_entity=20):
        captured_limits.append(limit_per_entity)
        return {eid: [] for eid in entity_ids}

    engine._batch_fetch_recent_memories = _fake_fetch  # type: ignore[method-assign]
    engine._batch_get_or_create_models = AsyncMock(
        return_value={eid_a: MagicMock(), eid_b: MagicMock()}
    )
    engine._batch_get_entities = AsyncMock(return_value={})
    # Stub the per-entity processing path so the test focuses on the batch fetch.
    engine._process_entity_reflection = AsyncMock(return_value=None)

    await engine.reflect_batch(requests)

    assert captured_limits == [None], (
        f'Expected single fetch with limit=None (unbounded → capped at {MAX_FULL_SCOPE_UNITS}); '
        f'got {captured_limits}'
    )


@pytest.mark.asyncio
async def test_reflect_batch_max_when_all_bounded(engine_relaxed_config, mock_session):
    engine = engine_relaxed_config
    """When all requests carry int limits, the batch fetch uses max(limits)."""
    from memex_core.memory.reflect.models import ReflectionRequest

    eid_a, eid_b = uuid4(), uuid4()
    requests = [
        ReflectionRequest(entity_id=eid_a, limit_recent_memories=20),
        ReflectionRequest(entity_id=eid_b, limit_recent_memories=50),
    ]

    captured_limits: list[int | None] = []

    async def _fake_fetch(entity_ids, vault_id=None, limit_per_entity=20):
        captured_limits.append(limit_per_entity)
        return {eid: [] for eid in entity_ids}

    engine._batch_fetch_recent_memories = _fake_fetch  # type: ignore[method-assign]
    engine._batch_get_or_create_models = AsyncMock(
        return_value={eid_a: MagicMock(), eid_b: MagicMock()}
    )
    engine._batch_get_entities = AsyncMock(return_value={})
    engine._process_entity_reflection = AsyncMock(return_value=None)
    await engine.reflect_batch(requests)

    assert captured_limits == [50]


@pytest.mark.asyncio
async def test_process_entity_reflection_slices_per_request(engine):
    """TC2.5 slice-side: per-request limit is honoured even when the batch
    fetch returned more units than the smaller request's limit allows.
    """
    from memex_core.memory.reflect.models import ReflectionRequest

    eid = uuid4()
    units = [MagicMock(name=f'u{i}') for i in range(30)]
    memories_map = {eid: units}

    captured = {}

    async def _fake_internal(*, entity_id, mental_model, entity, recent_memories, vault_id):
        captured['recent_memories'] = recent_memories
        return MagicMock()

    engine._reflect_entity_internal = _fake_internal  # type: ignore[method-assign]

    import asyncio

    sem = asyncio.Semaphore(1)

    req = ReflectionRequest(entity_id=eid, limit_recent_memories=20)
    await engine._process_entity_reflection(
        req=req,
        models_map={eid: MagicMock()},
        entities_map={},
        memories_map=memories_map,
        sem=sem,
    )
    assert len(captured['recent_memories']) == 20

    # And with limit=None, the full pre-capped fetch is passed through.
    req_full = ReflectionRequest(entity_id=eid, limit_recent_memories=None)
    await engine._process_entity_reflection(
        req=req_full,
        models_map={eid: MagicMock()},
        entities_map={},
        memories_map=memories_map,
        sem=sem,
    )
    assert len(captured['recent_memories']) == 30


@pytest.mark.asyncio
async def test_batch_get_or_create_models_logic(engine, mock_session):
    """Test mixed existing and new models."""
    existing_id = uuid4()
    missing_id = uuid4()

    # Mock existing finding
    mock_existing_model = MagicMock()
    mock_existing_model.entity_id = existing_id

    mock_session.exec.side_effect = [
        # 1. Query for models
        MagicMock(all=MagicMock(return_value=[mock_existing_model])),
        # 2. Query for missing entities (names)
        MagicMock(
            all=MagicMock(return_value=[MagicMock(id=missing_id, canonical_name='New Entity')])
        ),
    ]

    models_map = await engine._batch_get_or_create_models([existing_id, missing_id])

    assert len(models_map) == 2
    assert models_map[existing_id] == mock_existing_model
    assert models_map[missing_id].name == 'New Entity'

    # Verify new model was added
    mock_session.add.assert_called()


# ---------------------------------------------------------------------------
# F22 — variance prioritisation gate (Hermes round-10 HIGH).
#
# The variance sort over each entity bucket is gated on
# ``ReflectionConfig.variance_prioritisation_enabled``. Default False so the
# F22 migration ships INERTLY — the reflection cron's per-tick processing
# order MUST NOT change on deploy.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_variance_sort_off_by_default_preserves_sql_order(mock_session):
    """Default config (flag off) → bucket order matches SQL row order."""
    config = MemexConfig()
    assert config.server.memory.reflection.variance_prioritisation_enabled is False
    engine = ReflectionEngine(session=mock_session, config=config, embedder=MagicMock())

    eid = uuid4()
    # Three units: low/high/mid variance (descending count → ascending variance).
    high_var = MemoryUnit(text='cold', confidence=1.0, confidence_evidence_count=0)
    mid_var = MemoryUnit(text='mid', confidence=0.5, confidence_evidence_count=2)
    low_var = MemoryUnit(text='warm', confidence=0.5, confidence_evidence_count=20)

    mock_result = MagicMock()
    mock_result.all.return_value = [
        (low_var, eid),  # SQL emits in order: warm, mid, cold.
        (mid_var, eid),
        (high_var, eid),
    ]
    mock_session.exec.return_value = mock_result

    out = await engine._batch_fetch_recent_memories([eid], limit_per_entity=10)
    # Flag OFF → SQL order preserved (warm, mid, cold).
    assert [u.text for u in out[eid]] == ['warm', 'mid', 'cold']


@pytest.mark.asyncio
async def test_variance_sort_on_orders_by_descending_variance(mock_session):
    """Flag on → high-variance units land first within each bucket."""
    config = MemexConfig()
    config.server.memory.reflection.variance_prioritisation_enabled = True
    engine = ReflectionEngine(session=mock_session, config=config, embedder=MagicMock())

    eid = uuid4()
    high_var = MemoryUnit(text='cold', confidence=1.0, confidence_evidence_count=0)
    mid_var = MemoryUnit(text='mid', confidence=0.5, confidence_evidence_count=2)
    low_var = MemoryUnit(text='warm', confidence=0.5, confidence_evidence_count=20)

    mock_result = MagicMock()
    mock_result.all.return_value = [
        (low_var, eid),  # SQL order intentionally NOT variance-descending.
        (mid_var, eid),
        (high_var, eid),
    ]
    mock_session.exec.return_value = mock_result

    out = await engine._batch_fetch_recent_memories([eid], limit_per_entity=10)
    # Flag ON → reorder: cold (max variance) first, then mid, then warm.
    assert [u.text for u in out[eid]] == ['cold', 'mid', 'warm']
