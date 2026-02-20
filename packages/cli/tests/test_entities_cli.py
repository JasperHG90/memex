from uuid import uuid4
from memex_cli.entities import app as entity_app
from memex_common.schemas import EntityDTO, MemoryUnitDTO, DocumentDTO
from datetime import datetime, timezone


def test_entity_list(runner, mock_api, monkeypatch):
    # Proper Async Iterator Mock
    async def async_iter(limit=100, q=None):
        yield EntityDTO(id=uuid4(), name='Python', mention_count=10)
        yield EntityDTO(id=uuid4(), name='Rust', mention_count=5)

    # Replace method with plain function returning async iterator
    mock_api.list_entities_ranked = async_iter

    monkeypatch.setattr('memex_cli.entities.get_api_context', lambda config: mock_api)

    result = runner.invoke(entity_app, ['list'])
    assert result.exit_code == 0
    assert 'Python' in result.stdout
    assert 'Rust' in result.stdout


def test_entity_view_resolve_name(runner, mock_api, monkeypatch):
    e_id = uuid4()
    mock_api.search_entities.return_value = [EntityDTO(id=e_id, name='Python', mention_count=42)]
    monkeypatch.setattr('memex_cli.entities.get_api_context', lambda config: mock_api)

    result = runner.invoke(entity_app, ['view', 'Python'])
    assert result.exit_code == 0
    assert 'Entity: Python' in result.stdout
    assert str(e_id) in result.stdout


def test_entity_mentions(runner, mock_api, monkeypatch):
    e_id = uuid4()
    # Mock resolution first
    mock_api.get_entity.return_value = EntityDTO(id=e_id, name='Python', mention_count=42)

    # Mock mentions
    mock_api.get_entity_mentions.return_value = [
        {
            'unit': MemoryUnitDTO(id=uuid4(), text='I love Python', fact_type='observation'),
            'document': DocumentDTO(
                id=uuid4(), name='Notes.md', created_at=datetime.now(timezone.utc), vault_id=uuid4()
            ),
        }
    ]
    monkeypatch.setattr('memex_cli.entities.get_api_context', lambda config: mock_api)

    result = runner.invoke(entity_app, ['mentions', str(e_id)])
    assert result.exit_code == 0
    assert 'I love Python' in result.stdout
    assert 'Notes.md' in result.stdout
