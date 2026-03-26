import datetime as dt
import pytest
from uuid import uuid4
from unittest.mock import AsyncMock
from conftest import parse_tool_result
from memex_common.schemas import NoteDTO
import httpx


@pytest.mark.asyncio
async def test_mcp_list_assets(mock_api, mcp_client):
    """Test memex_list_assets returns file list."""
    doc_id = uuid4()

    mock_api.get_note.return_value = NoteDTO(
        id=doc_id,
        doc_metadata={'name': 'Architecture Diagram'},
        assets=['assets/docs/diagram.png', 'assets/docs/spec.pdf'],
        created_at=dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
        vault_id=uuid4(),
    )

    result = await mcp_client.call_tool(
        'memex_list_assets', {'note_id': str(doc_id), 'vault_id': 'test-vault'}
    )

    data = parse_tool_result(result)
    mock_api.get_note.assert_called_once_with(doc_id)

    assert len(data) == 2
    assert data[0]['filename'] == 'diagram.png'
    assert data[0]['path'] == 'assets/docs/diagram.png'
    assert data[1]['filename'] == 'spec.pdf'


@pytest.mark.asyncio
async def test_mcp_list_assets_not_found(mock_api, mcp_client):
    """Test memex_list_assets raises ToolError when note is not found (HTTP 404)."""
    from fastmcp.exceptions import ToolError

    doc_id = uuid4()

    response = httpx.Response(404, request=httpx.Request('GET', f'http://test/notes/{doc_id}'))
    mock_api.get_note.side_effect = httpx.HTTPStatusError(
        'Not Found', request=response.request, response=response
    )

    with pytest.raises(ToolError, match='not found'):
        await mcp_client.call_tool(
            'memex_list_assets', {'note_id': str(doc_id), 'vault_id': 'test-vault'}
        )


@pytest.mark.asyncio
async def test_mcp_list_assets_with_vault_id(mock_api, mcp_client):
    """Test memex_list_assets accepts vault_id parameter."""
    doc_id = uuid4()
    vault_id = uuid4()

    mock_api.resolve_vault_identifier = AsyncMock(return_value=vault_id)
    mock_api.get_note.return_value = NoteDTO(
        id=doc_id,
        doc_metadata={'name': 'Test Note'},
        assets=['assets/test.png'],
        created_at=dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
        vault_id=vault_id,
    )

    result = await mcp_client.call_tool(
        'memex_list_assets', {'note_id': str(doc_id), 'vault_id': str(vault_id)}
    )

    data = parse_tool_result(result)
    assert len(data) == 1
    assert data[0]['filename'] == 'test.png'
    mock_api.resolve_vault_identifier.assert_called_once_with(str(vault_id))


@pytest.mark.asyncio
async def test_mcp_list_assets_no_assets(mock_api, mcp_client):
    """Test memex_list_assets returns empty list when note has no assets."""
    doc_id = uuid4()

    mock_api.get_note.return_value = NoteDTO(
        id=doc_id,
        doc_metadata={'name': 'Empty Note'},
        assets=[],
        created_at=dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
        vault_id=uuid4(),
    )

    result = await mcp_client.call_tool(
        'memex_list_assets', {'note_id': str(doc_id), 'vault_id': 'test-vault'}
    )

    data = parse_tool_result(result)
    assert data == []


@pytest.mark.asyncio
async def test_mcp_list_assets_http_500_propagates(mock_api, mcp_client):
    """Test that non-404 HTTP errors are not swallowed."""
    from fastmcp.exceptions import ToolError

    doc_id = uuid4()

    response = httpx.Response(500, request=httpx.Request('GET', f'http://test/notes/{doc_id}'))
    mock_api.get_note.side_effect = httpx.HTTPStatusError(
        'Server Error', request=response.request, response=response
    )

    with pytest.raises(ToolError, match='List assets failed'):
        await mcp_client.call_tool(
            'memex_list_assets', {'note_id': str(doc_id), 'vault_id': 'test-vault'}
        )


@pytest.mark.asyncio
async def test_mcp_list_assets_invalid_uuid(mock_api, mcp_client):
    """Test memex_list_assets rejects invalid UUIDs."""
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError, match='Invalid Note UUID'):
        await mcp_client.call_tool(
            'memex_list_assets', {'note_id': 'not-a-uuid', 'vault_id': 'test-vault'}
        )


# ── memex_add_assets tests ──


@pytest.mark.asyncio
async def test_mcp_add_assets(mock_api, mcp_client, tmp_path):
    """Test memex_add_assets adds files to a note."""
    note_id = uuid4()

    # Create temp file
    asset_file = tmp_path / 'photo.png'
    asset_file.write_bytes(b'fake image data')

    mock_api.add_note_assets = AsyncMock(
        return_value={
            'note_id': str(note_id),
            'added_assets': [f'assets/vault/{note_id}/photo.png'],
            'skipped': [],
            'asset_count': 1,
        }
    )

    result = await mcp_client.call_tool(
        'memex_add_assets',
        {
            'note_id': str(note_id),
            'file_paths': [str(asset_file)],
            'vault_id': 'test-vault',
        },
    )

    data = parse_tool_result(result)
    mock_api.add_note_assets.assert_called_once()
    call_args = mock_api.add_note_assets.call_args
    assert call_args[0][0] == note_id
    assert 'photo.png' in call_args[0][1]

    assert data['asset_count'] == 1
    assert len(data['added_assets']) == 1
    assert data['added_assets'][0]['filename'] == 'photo.png'
    assert data['skipped'] == []


@pytest.mark.asyncio
async def test_mcp_add_assets_with_skipped_duplicates(mock_api, mcp_client, tmp_path):
    """Test memex_add_assets reports skipped duplicates."""
    note_id = uuid4()

    asset_file = tmp_path / 'existing.pdf'
    asset_file.write_bytes(b'pdf content')

    mock_api.add_note_assets = AsyncMock(
        return_value={
            'note_id': str(note_id),
            'added_assets': [],
            'skipped': ['existing.pdf'],
            'asset_count': 1,
        }
    )

    result = await mcp_client.call_tool(
        'memex_add_assets',
        {
            'note_id': str(note_id),
            'file_paths': [str(asset_file)],
            'vault_id': 'test-vault',
        },
    )

    data = parse_tool_result(result)
    assert data['skipped'] == ['existing.pdf']
    assert data['added_assets'] == []


@pytest.mark.asyncio
async def test_mcp_add_assets_not_found(mock_api, mcp_client, tmp_path):
    """Test memex_add_assets raises ToolError when note not found."""
    from fastmcp.exceptions import ToolError

    note_id = uuid4()

    asset_file = tmp_path / 'img.png'
    asset_file.write_bytes(b'data')

    response = httpx.Response(
        404, request=httpx.Request('POST', f'http://test/notes/{note_id}/assets')
    )
    mock_api.add_note_assets = AsyncMock(
        side_effect=httpx.HTTPStatusError('Not Found', request=response.request, response=response)
    )

    with pytest.raises(ToolError, match='not found'):
        await mcp_client.call_tool(
            'memex_add_assets',
            {
                'note_id': str(note_id),
                'file_paths': [str(asset_file)],
                'vault_id': 'test-vault',
            },
        )


@pytest.mark.asyncio
async def test_mcp_add_assets_invalid_uuid(mock_api, mcp_client):
    """Test memex_add_assets rejects invalid UUIDs."""
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError, match='Invalid Note UUID'):
        await mcp_client.call_tool(
            'memex_add_assets',
            {
                'note_id': 'not-a-uuid',
                'file_paths': ['/tmp/foo.png'],
                'vault_id': 'test-vault',
            },
        )


@pytest.mark.asyncio
async def test_mcp_add_assets_no_valid_files(mock_api, mcp_client):
    """Test memex_add_assets raises ToolError when no valid files exist."""
    from fastmcp.exceptions import ToolError

    note_id = uuid4()

    with pytest.raises(ToolError, match='No valid asset files'):
        await mcp_client.call_tool(
            'memex_add_assets',
            {
                'note_id': str(note_id),
                'file_paths': ['/nonexistent/file.png'],
                'vault_id': 'test-vault',
            },
        )


# ── memex_delete_assets tests ──


@pytest.mark.asyncio
async def test_mcp_delete_assets(mock_api, mcp_client):
    """Test memex_delete_assets deletes assets from a note."""
    note_id = uuid4()

    mock_api.delete_note_assets = AsyncMock(
        return_value={
            'note_id': str(note_id),
            'deleted_assets': ['assets/vault/diagram.png'],
            'not_found': [],
            'asset_count': 0,
        }
    )

    result = await mcp_client.call_tool(
        'memex_delete_assets',
        {
            'note_id': str(note_id),
            'asset_paths': ['assets/vault/diagram.png'],
            'vault_id': 'test-vault',
        },
    )

    data = parse_tool_result(result)
    mock_api.delete_note_assets.assert_called_once_with(note_id, ['assets/vault/diagram.png'])
    assert data['deleted'] == ['assets/vault/diagram.png']
    assert data['not_found'] == []
    assert data['asset_count'] == 0


@pytest.mark.asyncio
async def test_mcp_delete_assets_partial_not_found(mock_api, mcp_client):
    """Test memex_delete_assets reports paths not found."""
    note_id = uuid4()

    mock_api.delete_note_assets = AsyncMock(
        return_value={
            'note_id': str(note_id),
            'deleted_assets': ['assets/vault/real.png'],
            'not_found': ['assets/vault/nonexistent.png'],
            'asset_count': 1,
        }
    )

    result = await mcp_client.call_tool(
        'memex_delete_assets',
        {
            'note_id': str(note_id),
            'asset_paths': ['assets/vault/real.png', 'assets/vault/nonexistent.png'],
            'vault_id': 'test-vault',
        },
    )

    data = parse_tool_result(result)
    assert data['deleted'] == ['assets/vault/real.png']
    assert data['not_found'] == ['assets/vault/nonexistent.png']


@pytest.mark.asyncio
async def test_mcp_delete_assets_not_found(mock_api, mcp_client):
    """Test memex_delete_assets raises ToolError when note not found."""
    from fastmcp.exceptions import ToolError

    note_id = uuid4()

    response = httpx.Response(
        404, request=httpx.Request('DELETE', f'http://test/notes/{note_id}/assets')
    )
    mock_api.delete_note_assets = AsyncMock(
        side_effect=httpx.HTTPStatusError('Not Found', request=response.request, response=response)
    )

    with pytest.raises(ToolError, match='not found'):
        await mcp_client.call_tool(
            'memex_delete_assets',
            {
                'note_id': str(note_id),
                'asset_paths': ['assets/vault/foo.png'],
                'vault_id': 'test-vault',
            },
        )


@pytest.mark.asyncio
async def test_mcp_delete_assets_invalid_uuid(mock_api, mcp_client):
    """Test memex_delete_assets rejects invalid UUIDs."""
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError, match='Invalid Note UUID'):
        await mcp_client.call_tool(
            'memex_delete_assets',
            {
                'note_id': 'not-a-uuid',
                'asset_paths': ['assets/vault/foo.png'],
                'vault_id': 'test-vault',
            },
        )
