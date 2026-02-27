"""Tests for the memex_get_page_index and memex_get_node MCP tools."""

import datetime as dt
from uuid import uuid4

import pytest
from fastmcp.exceptions import ToolError
from memex_common.schemas import NodeDTO


def _make_node(
    title: str = 'Introduction',
    text: str = 'Section body text.',
    level: int = 1,
    seq: int = 0,
) -> NodeDTO:
    return NodeDTO(
        id=uuid4(),
        note_id=uuid4(),
        vault_id=uuid4(),
        title=title,
        text=text,
        level=level,
        seq=seq,
        status='active',
        created_at=dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
    )


# ---------------------------------------------------------------------------
# memex_get_page_index
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memex_get_page_index_returns_json(mock_api, mcp_client):
    """Tool returns a JSON-formatted page index when one exists."""
    doc_id = uuid4()
    page_index = [
        {'id': str(uuid4()), 'title': 'Chapter 1', 'level': 1, 'children': []},
        {'id': str(uuid4()), 'title': 'Chapter 2', 'level': 1, 'children': []},
    ]
    mock_api.get_note_page_index.return_value = page_index

    result = await mcp_client.call_tool('memex_get_page_index', {'note_id': str(doc_id)})
    text = result.content[0].text

    assert 'Chapter 1' in text
    assert 'Chapter 2' in text
    mock_api.get_note_page_index.assert_called_once_with(doc_id)


@pytest.mark.asyncio
async def test_memex_get_page_index_no_index(mock_api, mcp_client):
    """Tool returns a helpful message when the document has no page index."""
    mock_api.get_note_page_index.return_value = None

    result = await mcp_client.call_tool('memex_get_page_index', {'note_id': str(uuid4())})
    text = result.content[0].text

    assert 'No page index available' in text


@pytest.mark.asyncio
async def test_memex_get_page_index_invalid_uuid(mock_api, mcp_client):
    """Tool raises ToolError for a malformed document UUID."""
    with pytest.raises(ToolError, match='Invalid Note UUID'):
        await mcp_client.call_tool('memex_get_page_index', {'note_id': 'not-a-uuid'})

    mock_api.get_note_page_index.assert_not_called()


@pytest.mark.asyncio
async def test_memex_get_page_index_exception_handling(mock_api, mcp_client):
    """Tool raises ToolError on unexpected failure."""
    mock_api.get_note_page_index.side_effect = RuntimeError('DB offline')

    with pytest.raises(ToolError, match='DB offline'):
        await mcp_client.call_tool('memex_get_page_index', {'note_id': str(uuid4())})


# ---------------------------------------------------------------------------
# memex_get_node
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memex_get_node_returns_formatted_content(mock_api, mcp_client):
    """Tool output includes title, node ID, document ID, and text."""
    node = _make_node(title='Background', text='Some background text.', level=2)
    mock_api.get_node.return_value = node

    result = await mcp_client.call_tool('memex_get_node', {'node_id': str(node.id)})
    text = result.content[0].text

    assert 'Background' in text
    assert str(node.id) in text
    assert str(node.note_id) in text
    assert 'Some background text.' in text
    mock_api.get_node.assert_called_once_with(node.id)


@pytest.mark.asyncio
async def test_memex_get_node_not_found(mock_api, mcp_client):
    """Tool raises ToolError when the node does not exist."""
    node_id = uuid4()
    mock_api.get_node.return_value = None

    with pytest.raises(ToolError, match='not found'):
        await mcp_client.call_tool('memex_get_node', {'node_id': str(node_id)})


@pytest.mark.asyncio
async def test_memex_get_node_invalid_uuid(mock_api, mcp_client):
    """Tool raises ToolError for a malformed node UUID."""
    with pytest.raises(ToolError, match='Invalid Node UUID'):
        await mcp_client.call_tool('memex_get_node', {'node_id': 'bad-uuid'})

    mock_api.get_node.assert_not_called()


@pytest.mark.asyncio
async def test_memex_get_node_empty_text(mock_api, mcp_client):
    """Tool handles nodes with no text content gracefully."""
    node = _make_node(title='Empty Section', text='')
    mock_api.get_node.return_value = node

    result = await mcp_client.call_tool('memex_get_node', {'node_id': str(node.id)})
    text = result.content[0].text

    assert 'Empty Section' in text
    assert 'No text content' in text


@pytest.mark.asyncio
async def test_memex_get_node_exception_handling(mock_api, mcp_client):
    """Tool raises ToolError on unexpected failure."""
    mock_api.get_node.side_effect = RuntimeError('connection reset')

    with pytest.raises(ToolError, match='connection reset'):
        await mcp_client.call_tool('memex_get_node', {'node_id': str(uuid4())})
