"""Integration test: contradiction detection fires post-commit with real Postgres.

Verifies that after ingest() commits, the contradiction_task coroutine
is awaited by IngestionService (centralized await pattern) and can see
the newly created memory units via a new DB session. This proves
READ COMMITTED visibility works correctly.
"""

from uuid import UUID, uuid4

import pytest
from sqlmodel import select, col
from unittest.mock import AsyncMock, MagicMock, patch

from memex_core.memory.sql_models import MemoryUnit
from memex_core.services.ingestion import IngestionService
from memex_core.services.vaults import VaultService


@pytest.fixture
def ingestion_service_with_contradiction(metastore, filestore, memex_config, fake_retain_factory):
    """Build an IngestionService whose memory.retain() persists real data
    and returns a contradiction_task coroutine that loads units via a
    new session (simulating ContradictionEngine behaviour)."""
    base_retain = fake_retain_factory

    # We'll capture the unit_ids and vault_id from retain() so we can
    # verify what the contradiction task loaded.
    captured: dict = {}

    async def retain_with_contradiction(session, contents, note_id, **kwargs):
        result = await base_retain(session, contents, note_id, **kwargs)
        unit_ids = [UUID(uid) for uid in result['unit_ids']]
        vault_id = contents[0].vault_id
        captured['unit_ids'] = unit_ids
        captured['vault_id'] = vault_id

        async def fake_contradiction_task():
            """Simulate what ContradictionEngine.detect_contradictions does:
            open a new session via session_factory and load units."""
            async with metastore.session() as new_session:
                stmt = select(MemoryUnit).where(col(MemoryUnit.id).in_(unit_ids))
                rows = await new_session.exec(stmt)
                loaded = list(rows.all())
                # Store what we loaded so the test can verify
                captured['loaded_units'] = loaded

        result['contradiction_task'] = fake_contradiction_task()
        return result

    memory = AsyncMock()
    memory.retain = AsyncMock(side_effect=retain_with_contradiction)

    lm = MagicMock()
    vaults = MagicMock(spec=VaultService)
    vaults.resolve_vault_identifier = AsyncMock()
    file_processor = MagicMock()

    svc = IngestionService(
        metastore=metastore,
        filestore=filestore,
        config=memex_config,
        lm=lm,
        memory=memory,
        file_processor=file_processor,
        vaults=vaults,
    )
    svc._audit_service = MagicMock()
    svc._audit_service.log = AsyncMock()
    return svc, captured


@pytest.mark.integration
@pytest.mark.asyncio
async def test_contradiction_loads_units_after_commit(
    ingestion_service_with_contradiction,
):
    """After ingest() returns (transaction committed), the contradiction_task
    coroutine has been awaited by IngestionService and loaded the newly
    created memory units from a separate DB session. This proves the
    centralized await pattern works with READ COMMITTED visibility."""
    from memex_common.config import GLOBAL_VAULT_ID

    svc, captured = ingestion_service_with_contradiction
    svc._vaults.resolve_vault_identifier.return_value = GLOBAL_VAULT_ID

    note_id = uuid4()
    note = MagicMock()
    note.idempotency_key = note_id
    note._metadata.name = f'Contradiction Test Note {note_id}'
    note._metadata.description = 'Testing post-commit visibility'
    note._metadata.author = None
    note._metadata.tags = []
    note._content = b'# The capital of France is Paris. ' + str(note_id).encode()
    note._files = {}
    note.source_uri = None
    note.content_fingerprint = 'fp_' + str(note_id)
    note.template = None

    with (
        patch(
            'memex_core.services.ingestion.resolve_document_title',
            new_callable=AsyncMock,
        ) as mock_title,
        patch('memex_core.services.ingestion.audit_event'),
    ):
        mock_title.return_value = 'Contradiction Test Note'

        svc._detect_overlapping_notes = AsyncMock(return_value=[])

        result = await svc.ingest(note, vault_id=GLOBAL_VAULT_ID)

    # 1. Verify contradiction_task was popped and awaited by IngestionService
    assert 'contradiction_task' not in result, (
        'contradiction_task key should be popped by IngestionService after awaiting'
    )

    # 2. Verify the contradiction coroutine actually ran and loaded units
    assert 'loaded_units' in captured, (
        'Contradiction coroutine was never awaited — captured has no loaded_units'
    )
    loaded_units = captured['loaded_units']

    # 3. Verify _load_units equivalent found the committed memory units
    assert len(loaded_units) > 0, (
        '_load_units() returned empty — new session cannot see committed units'
    )

    # 4. Verify the loaded units match what was created
    loaded_ids = {u.id for u in loaded_units}
    expected_ids = set(captured['unit_ids'])
    assert loaded_ids == expected_ids, (
        f'Loaded unit IDs {loaded_ids} do not match expected {expected_ids}'
    )
