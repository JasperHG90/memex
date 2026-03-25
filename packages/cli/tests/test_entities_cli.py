import json
from uuid import uuid4
from memex_common.schemas import EntityDTO, MemoryUnitDTO, NoteDTO
from memex_cli.entities import app as entity_app
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


# ---------------------------------------------------------------------------
# Batch: entity view (multi-ID)
# ---------------------------------------------------------------------------


def test_entity_view_multi(runner, mock_api, monkeypatch):
    e1 = EntityDTO(id=uuid4(), name='Python', mention_count=10)
    e2 = EntityDTO(id=uuid4(), name='Rust', mention_count=5)
    mock_api.get_entity.side_effect = [e1, e2]
    monkeypatch.setattr('memex_cli.entities.get_api_context', lambda config: mock_api)

    result = runner.invoke(entity_app, ['view', str(e1.id), str(e2.id)])
    assert result.exit_code == 0
    assert 'Python' in result.stdout
    assert 'Rust' in result.stdout


def test_entity_view_multi_json(runner, mock_api, monkeypatch):
    e1 = EntityDTO(id=uuid4(), name='Alpha', mention_count=1)
    e2 = EntityDTO(id=uuid4(), name='Beta', mention_count=2)
    mock_api.get_entity.side_effect = [e1, e2]
    monkeypatch.setattr('memex_cli.entities.get_api_context', lambda config: mock_api)

    result = runner.invoke(entity_app, ['view', str(e1.id), str(e2.id), '--json'])
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert isinstance(data, list)
    assert len(data) == 2


def test_entity_view_multi_partial_error(runner, mock_api, monkeypatch):
    e1 = EntityDTO(id=uuid4(), name='Good', mention_count=1)
    mock_api.get_entity.side_effect = [e1, RuntimeError('not found')]
    monkeypatch.setattr('memex_cli.entities.get_api_context', lambda config: mock_api)

    result = runner.invoke(entity_app, ['view', str(e1.id), str(uuid4())])
    assert result.exit_code == 0
    assert 'Good' in result.stdout
    assert 'Error' in result.stdout
