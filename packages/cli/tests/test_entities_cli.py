from uuid import uuid4
from memex_common.schemas import EntityDTO, MemoryUnitDTO, NoteDTO
from datetime import datetime, timezone


def test_entity_list(runner, mock_api, monkeypatch):
    # Proper Async Iterator Mock
    async def async_iter(limit=100, q=None):
        yield EntityDTO(id=uuid4(), name='Python', mention_count=10)


def test_entity_view_resolve_name(runner, mock_api, monkeypatch):
    e_id = uuid4()
    # Mock resolution first
    mock_api.get_entity.return_value = EntityDTO(id=e_id, name='Python', mention_count=42)


def test_entity_mentions(runner, mock_api, monkeypatch):
    e_id = uuid4()
    # Mock resolution first
    mock_api.get_entity.return_value = EntityDTO(id=e_id, name='Python', mention_count=42)

    # Mock mentions
    mock_api.get_entity_mentions.return_value = [
        {
            'unit': MemoryUnitDTO(id=uuid4(), text='I love Python', fact_type='observation'),
            'note': NoteDTO(
                id=uuid4(), name='Notes.md', created_at=datetime.now(timezone.utc), vault_id=uuid4()
            ),
        }
    ]
