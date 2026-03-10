import datetime as dt
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from fastmcp import Client
from memex_mcp.server import mcp
from memex_common.schemas import MemoryUnitDTO, FactTypes, NoteDTO


@pytest.fixture
def mock_api():
    """Mock the RemoteMemexAPI."""
    mock = AsyncMock()
    mock.search = AsyncMock()
    mock.get_notes_metadata = AsyncMock(return_value=[])

    with patch('memex_mcp.server.get_api', return_value=mock):
        yield mock


@pytest.mark.asyncio
async def test_integration_search_assets_resource(mock_api):
    """Test flow: Search -> List Assets -> Get Resource."""
    mock_api.get_note = AsyncMock()
    mock_api.get_resource = AsyncMock()

    # 1. Setup Search Data
    unit_id = uuid4()
    doc_id = uuid4()
    mock_api.search.return_value = [
        MemoryUnitDTO(
            id=unit_id,
            note_id=doc_id,
            text='Architecture diagram shows the system layout.',
            fact_type=FactTypes.WORLD,
            score=0.95,
            vault_id=uuid4(),
            metadata={},
        )
    ]

    # 2. Setup Document Data (for List Assets)
    mock_api.get_note.return_value = NoteDTO(
        id=doc_id,
        doc_metadata={'name': 'System Arch'},
        assets=['assets/arch.png'],
        created_at=dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
        vault_id=uuid4(),
    )

    # 3. Setup Resource Data (for Get Resource)
    mock_api.get_resource_path = MagicMock(return_value='/data/assets/arch.png')
    mock_api.get_resource.return_value = b'fake_image_bytes'

    async with Client(mcp) as client:
        # Step 1: Search
        search_result = await client.call_tool('memex_memory_search', {'query': 'architecture'})
        search_text = search_result.content[0].text

        assert str(doc_id) in search_text

        # Step 2: List Assets using Document ID from search
        assets_result = await client.call_tool('memex_list_assets', {'note_id': str(doc_id)})
        assets_text = assets_result.content[0].text

        assert 'arch.png' in assets_text
        assert 'assets/arch.png' in assets_text

        # Step 3: Get Resource (batch)
        await client.call_tool('memex_get_resources', {'paths': ['assets/arch.png']})

        # Verify it returns file:// URI for local images
        mock_api.get_resource_path.assert_called_once_with('assets/arch.png')


@pytest.mark.asyncio
async def test_list_notes_includes_publish_date(mock_api):
    """memex_list_notes includes publish_date in its output."""
    pub = dt.datetime(2025, 3, 15, tzinfo=dt.timezone.utc)
    mock_api.list_notes = AsyncMock(
        return_value=[
            NoteDTO(
                id=uuid4(),
                title='Published Note',
                created_at=dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
                publish_date=pub,
                vault_id=uuid4(),
            )
        ]
    )

    async with Client(mcp) as client:
        result = await client.call_tool('memex_list_notes', {})
        text = result.content[0].text

        assert '2025-03-15' in text
        assert 'Published Note' in text


@pytest.mark.asyncio
async def test_recent_notes_includes_publish_date(mock_api):
    """memex_recent_notes includes publish_date in its output."""
    pub = dt.datetime(2025, 3, 15, tzinfo=dt.timezone.utc)
    mock_api.get_recent_notes = AsyncMock(
        return_value=[
            NoteDTO(
                id=uuid4(),
                title='Recent Published',
                created_at=dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
                publish_date=pub,
                vault_id=uuid4(),
            )
        ]
    )

    async with Client(mcp) as client:
        result = await client.call_tool('memex_recent_notes', {})
        text = result.content[0].text

        assert '2025-03-15' in text
        assert 'Recent Published' in text
