"""Tests for the memex_get_page_indices and memex_get_nodes MCP tools."""

import datetime as dt
from uuid import uuid4

import pytest
from conftest import parse_tool_result
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
# memex_get_page_indices
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memex_get_page_indices_returns_json(mock_api, mcp_client):
    """Tool returns a JSON-formatted page index when one exists."""
    doc_id = uuid4()
    page_index = {
        'metadata': {'title': 'Test Note', 'description': 'A test note'},
        'toc': [
            {'id': str(uuid4()), 'title': 'Chapter 1', 'level': 1, 'children': []},
            {'id': str(uuid4()), 'title': 'Chapter 2', 'level': 1, 'children': []},
        ],
    }
    mock_api.get_note_page_index.return_value = page_index

    result = await mcp_client.call_tool('memex_get_page_indices', {'note_ids': [str(doc_id)]})
    data = parse_tool_result(result)

    assert data[0]['toc'][0]['title'] == 'Chapter 1'
    assert data[0]['toc'][1]['title'] == 'Chapter 2'
    mock_api.get_note_page_index.assert_called_once_with(doc_id)


@pytest.mark.asyncio
async def test_memex_get_page_indices_no_index(mock_api, mcp_client):
    """Tool returns empty list when the document has no page index."""
    mock_api.get_note_page_index.return_value = None

    result = await mcp_client.call_tool('memex_get_page_indices', {'note_ids': [str(uuid4())]})
    data = parse_tool_result(result)

    assert data == []


@pytest.mark.asyncio
async def test_memex_get_page_indices_invalid_uuid(mock_api, mcp_client):
    """Tool silently skips malformed document UUIDs."""
    result = await mcp_client.call_tool(
        'memex_get_page_indices', {'note_ids': ['not-a-uuid']}, raise_on_error=False
    )
    data = parse_tool_result(result)
    assert data == []
    mock_api.get_note_page_index.assert_not_called()


@pytest.mark.asyncio
async def test_memex_get_page_indices_exception_handling(mock_api, mcp_client):
    """Tool silently skips notes that raise exceptions."""
    mock_api.get_note_page_index.side_effect = RuntimeError('DB offline')

    result = await mcp_client.call_tool('memex_get_page_indices', {'note_ids': [str(uuid4())]})
    data = parse_tool_result(result)
    assert data == []


# ---------------------------------------------------------------------------
# memex_get_nodes (batch)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memex_get_nodes_returns_formatted_content(mock_api, mcp_client):
    """Tool output includes title, node ID, document ID, and text."""
    node = _make_node(title='Background', text='Some background text.', level=2)
    mock_api.get_nodes.return_value = [node]

    result = await mcp_client.call_tool('memex_get_nodes', {'node_ids': [str(node.id)]})
    data = parse_tool_result(result)

    assert data[0]['title'] == 'Background'
    assert data[0]['id'] == str(node.id)
    assert data[0]['note_id'] == str(node.note_id)
    assert data[0]['text'] == 'Some background text.'
    mock_api.get_nodes.assert_called_once()


@pytest.mark.asyncio
async def test_memex_get_nodes_not_found(mock_api, mcp_client):
    """Tool silently drops nodes that do not exist."""
    node_id = uuid4()
    mock_api.get_nodes.return_value = []

    result = await mcp_client.call_tool('memex_get_nodes', {'node_ids': [str(node_id)]})
    data = parse_tool_result(result)

    assert data == []


@pytest.mark.asyncio
async def test_memex_get_nodes_invalid_uuid(mock_api, mcp_client):
    """Tool raises ToolError for a malformed node UUID."""
    with pytest.raises(ToolError, match='Invalid UUID'):
        await mcp_client.call_tool('memex_get_nodes', {'node_ids': ['bad-uuid']})

    mock_api.get_nodes.assert_not_called()


@pytest.mark.asyncio
async def test_memex_get_nodes_empty_text(mock_api, mcp_client):
    """Tool handles nodes with no text content gracefully."""
    node = _make_node(title='Empty Section', text='')
    mock_api.get_nodes.return_value = [node]

    result = await mcp_client.call_tool('memex_get_nodes', {'node_ids': [str(node.id)]})
    data = parse_tool_result(result)

    assert data[0]['title'] == 'Empty Section'
    assert not data[0]['text']


@pytest.mark.asyncio
async def test_memex_get_nodes_exception_handling(mock_api, mcp_client):
    """Tool returns empty list when batch lookup fails."""
    mock_api.get_nodes.side_effect = RuntimeError('connection reset')
    mock_api.get_node.side_effect = RuntimeError('connection reset')

    result = await mcp_client.call_tool('memex_get_nodes', {'node_ids': [str(uuid4())]})
    data = parse_tool_result(result)
    assert data == []


# ---------------------------------------------------------------------------
# memex_get_page_indices — total_tokens
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memex_get_page_indices_includes_total_tokens(mock_api, mcp_client):
    """Page index response includes total_tokens from metadata (small note, no guard)."""
    page_index = {
        'metadata': {'title': 'Test', 'total_tokens': 2000},
        'toc': [
            {
                'id': str(uuid4()),
                'title': 'Chapter 1',
                'level': 1,
                'token_estimate': 1200,
                'children': [],
            },
            {
                'id': str(uuid4()),
                'title': 'Chapter 2',
                'level': 1,
                'token_estimate': 800,
                'children': [],
            },
        ],
    }
    mock_api.get_note_page_index.return_value = page_index

    result = await mcp_client.call_tool('memex_get_page_indices', {'note_ids': [str(uuid4())]})
    data = parse_tool_result(result)

    assert data[0]['total_tokens'] == 2000
    assert data[0]['metadata']['total_tokens'] == 2000


@pytest.mark.asyncio
async def test_memex_get_page_indices_blocks_large_unfiltered(mock_api, mcp_client):
    """Unfiltered page index whose TOC exceeds 3000 tokens reports error in output."""
    large_toc = []
    for i in range(60):
        large_toc.append(
            {
                'id': str(uuid4()),
                'title': f'Section {i}: ' + 'A' * 200,
                'level': 1,
                'token_estimate': 10,
                'summary': {
                    'who': 'Person ' + 'B' * 40,
                    'what': 'Did something ' + 'C' * 40,
                },
                'children': [],
            }
        )
    page_index = {
        'metadata': {'title': 'Large TOC Note', 'total_tokens': 600},
        'toc': large_toc,
    }
    mock_api.get_note_page_index.return_value = page_index

    with pytest.raises(ToolError, match='Page index has'):
        await mcp_client.call_tool('memex_get_page_indices', {'note_ids': [str(uuid4())]})


@pytest.mark.asyncio
async def test_memex_get_page_indices_allows_large_content_small_toc(mock_api, mcp_client):
    """A note with high content tokens but a small TOC should NOT be blocked."""
    page_index = {
        'metadata': {'title': 'Big Content Note', 'total_tokens': 9999},
        'toc': [
            {
                'id': str(uuid4()),
                'title': 'Introduction',
                'level': 1,
                'token_estimate': 5000,
                'children': [],
            },
            {
                'id': str(uuid4()),
                'title': 'Conclusion',
                'level': 1,
                'token_estimate': 4999,
                'children': [],
            },
        ],
    }
    mock_api.get_note_page_index.return_value = page_index

    result = await mcp_client.call_tool('memex_get_page_indices', {'note_ids': [str(uuid4())]})
    data = parse_tool_result(result)

    # Should return successfully with the content total_tokens preserved
    assert data[0]['total_tokens'] == 9999
    assert len(data[0]['toc']) == 2


@pytest.mark.asyncio
async def test_memex_get_page_indices_toc_guard_includes_summaries(mock_api, mcp_client):
    """TOC guard accounts for summary fields, not just titles."""
    # Few nodes but with very large summaries that push TOC cost over 3000
    toc = []
    for i in range(10):
        toc.append(
            {
                'id': str(uuid4()),
                'title': f'Section {i}',
                'level': 1,
                'token_estimate': 50,
                'summary': {
                    'who': 'X' * 2000,
                    'what': 'Y' * 2000,
                    'how': 'Z' * 2000,
                },
                'children': [],
            }
        )
    page_index = {
        'metadata': {'title': 'Summary-heavy', 'total_tokens': 500},
        'toc': toc,
    }
    mock_api.get_note_page_index.return_value = page_index

    with pytest.raises(ToolError, match='Page index has'):
        await mcp_client.call_tool('memex_get_page_indices', {'note_ids': [str(uuid4())]})


@pytest.mark.asyncio
async def test_memex_get_page_indices_toc_guard_counts_nested_children(mock_api, mcp_client):
    """TOC guard recurses into nested children when estimating cost."""

    # Build a deep tree where each level has a node with a long title
    def _nested(depth: int) -> list[dict]:
        if depth == 0:
            return []
        return [
            {
                'id': str(uuid4()),
                'title': 'Node ' + 'A' * 200,
                'level': depth,
                'token_estimate': 10,
                'children': _nested(depth - 1),
            }
        ]

    # 50 root nodes each with 1 child = 100 nodes total, each ~70 tokens overhead
    toc = []
    for _ in range(50):
        toc.append(
            {
                'id': str(uuid4()),
                'title': 'Root ' + 'B' * 200,
                'level': 1,
                'token_estimate': 10,
                'children': [
                    {
                        'id': str(uuid4()),
                        'title': 'Child ' + 'C' * 200,
                        'level': 2,
                        'token_estimate': 10,
                        'children': [],
                    }
                ],
            }
        )
    page_index = {
        'metadata': {'title': 'Deep Note', 'total_tokens': 1000},
        'toc': toc,
    }
    mock_api.get_note_page_index.return_value = page_index

    with pytest.raises(ToolError, match='Page index has'):
        await mcp_client.call_tool('memex_get_page_indices', {'note_ids': [str(uuid4())]})


@pytest.mark.asyncio
async def test_memex_get_page_indices_small_toc_no_summaries_passes(mock_api, mcp_client):
    """A small TOC with no summaries should always pass the guard."""
    toc = [
        {
            'id': str(uuid4()),
            'title': f'Chapter {i}',
            'level': 1,
            'token_estimate': 1000,
            'children': [],
        }
        for i in range(5)
    ]
    page_index = {
        'metadata': {'title': 'Normal Note', 'total_tokens': 5000},
        'toc': toc,
    }
    mock_api.get_note_page_index.return_value = page_index

    result = await mcp_client.call_tool('memex_get_page_indices', {'note_ids': [str(uuid4())]})
    data = parse_tool_result(result)

    assert data[0]['total_tokens'] == 5000
    assert len(data[0]['toc']) == 5


@pytest.mark.asyncio
async def test_memex_get_page_indices_allows_large_with_depth(mock_api, mcp_client):
    """Large note with depth=0 should NOT be blocked by the guard."""
    page_index = {
        'metadata': {'title': 'Test', 'total_tokens': 9999},
        'toc': [
            {
                'id': str(uuid4()),
                'title': 'Section',
                'level': 1,
                'token_estimate': 100,
                'children': [],
            },
        ],
    }
    mock_api.get_note_page_index.return_value = page_index

    result = await mcp_client.call_tool(
        'memex_get_page_indices', {'note_ids': [str(uuid4())], 'depth': 0}
    )
    data = parse_tool_result(result)

    # With depth=0, guard is not triggered — recursive sum used
    assert data[0]['total_tokens'] == 100


@pytest.mark.asyncio
async def test_memex_get_page_indices_falls_back_to_recursive_sum(mock_api, mcp_client):
    """When stored total_tokens is missing, fall back to recursive sum."""
    page_index = {
        'metadata': {'title': 'Old Note'},
        'toc': [
            {
                'id': str(uuid4()),
                'title': 'A',
                'level': 1,
                'token_estimate': 200,
                'children': [
                    {
                        'id': str(uuid4()),
                        'title': 'A.1',
                        'level': 2,
                        'token_estimate': 150,
                        'children': [],
                    },
                ],
            },
        ],
    }
    mock_api.get_note_page_index.return_value = page_index

    result = await mcp_client.call_tool('memex_get_page_indices', {'note_ids': [str(uuid4())]})
    data = parse_tool_result(result)

    assert data[0]['total_tokens'] == 350


@pytest.mark.asyncio
async def test_memex_get_notes_metadata_includes_total_tokens(mock_api, mcp_client):
    """memex_get_notes_metadata should return total_tokens when present."""
    nid = uuid4()
    mock_api.get_notes_metadata.return_value = [
        {
            'note_id': str(nid),
            'title': 'Big Note',
            'description': 'A large note',
            'total_tokens': 7500,
        },
    ]

    result = await mcp_client.call_tool('memex_get_notes_metadata', {'note_ids': [str(nid)]})
    data = parse_tool_result(result)

    assert data[0]['total_tokens'] == 7500
    assert data[0]['title'] == 'Big Note'


# ---------------------------------------------------------------------------
# memex_get_page_indices — depth / parent_node_id filtering
# ---------------------------------------------------------------------------


def _make_nested_page_index():
    """Build a page index with a 3-level TOC for filtering tests."""
    return {
        'metadata': {'title': 'Test Note', 'total_tokens': 1000},
        'toc': [
            {
                'id': str(uuid4()),
                'title': 'Root 1',
                'level': 1,
                'token_estimate': 300,
                'children': [
                    {
                        'id': str(uuid4()),
                        'title': 'Child 1.1',
                        'level': 2,
                        'token_estimate': 150,
                        'children': [
                            {
                                'id': str(uuid4()),
                                'title': 'Grandchild 1.1.1',
                                'level': 3,
                                'token_estimate': 50,
                                'children': [],
                            },
                        ],
                    },
                ],
            },
            {
                'id': str(uuid4()),
                'title': 'Root 2',
                'level': 1,
                'token_estimate': 500,
                'children': [],
            },
        ],
    }


@pytest.mark.asyncio
async def test_memex_get_page_indices_depth_filters_toc(mock_api, mcp_client):
    """depth=0 returns roots + direct children (H1+H2 overview), grandchildren emptied."""
    page_index = _make_nested_page_index()
    mock_api.get_note_page_index.return_value = page_index

    result = await mcp_client.call_tool(
        'memex_get_page_indices', {'note_ids': [str(uuid4())], 'depth': 0}
    )
    data = parse_tool_result(result)

    assert len(data[0]['toc']) == 2
    assert data[0]['toc'][0]['title'] == 'Root 1'
    # depth=0 now includes direct children (H2 level)
    assert len(data[0]['toc'][0]['children']) == 1
    assert data[0]['toc'][0]['children'][0]['title'] == 'Child 1.1'
    # But grandchildren are trimmed
    assert data[0]['toc'][0]['children'][0]['children'] == []
    assert data[0]['toc'][1]['title'] == 'Root 2'
    assert data[0]['toc'][1]['children'] == []


@pytest.mark.asyncio
async def test_memex_get_page_indices_parent_node_id_filters_subtree(mock_api, mcp_client):
    """parent_node_id returns the subtree rooted at that node."""
    page_index = _make_nested_page_index()
    root_id = page_index['toc'][0]['id']
    mock_api.get_note_page_index.return_value = page_index

    result = await mcp_client.call_tool(
        'memex_get_page_indices', {'note_ids': [str(uuid4())], 'parent_node_id': root_id}
    )
    data = parse_tool_result(result)

    assert len(data[0]['toc']) == 1
    assert data[0]['toc'][0]['title'] == 'Child 1.1'


@pytest.mark.asyncio
async def test_memex_get_page_indices_filtered_total_tokens_uses_recursive_sum(
    mock_api, mcp_client
):
    """When filtering is active, total_tokens is computed from the filtered tree, not stored."""
    page_index = _make_nested_page_index()
    mock_api.get_note_page_index.return_value = page_index

    result = await mcp_client.call_tool(
        'memex_get_page_indices', {'note_ids': [str(uuid4())], 'depth': 0}
    )
    data = parse_tool_result(result)

    # depth=0 includes roots + direct children (H1+H2), grandchildren trimmed
    # Root 1 (300) + Child 1.1 (150) + Root 2 (500) = 950
    expected = 300 + 150 + 500
    assert data[0]['total_tokens'] == expected


# ---------------------------------------------------------------------------
# memex_get_page_indices — depth semantics (H1+H2 overview, full tree)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memex_get_page_indices_depth_0_includes_h2_children(mock_api, mcp_client):
    """depth=0 includes H2-level children under each H1 root."""
    page_index = {
        'metadata': {'title': 'Test', 'total_tokens': 500},
        'toc': [
            {
                'id': str(uuid4()),
                'title': 'Chapter 1',
                'level': 1,
                'token_estimate': 100,
                'children': [
                    {
                        'id': str(uuid4()),
                        'title': 'Section 1.1',
                        'level': 2,
                        'token_estimate': 50,
                        'children': [],
                    },
                    {
                        'id': str(uuid4()),
                        'title': 'Section 1.2',
                        'level': 2,
                        'token_estimate': 50,
                        'children': [],
                    },
                ],
            },
        ],
    }
    mock_api.get_note_page_index.return_value = page_index

    result = await mcp_client.call_tool(
        'memex_get_page_indices', {'note_ids': [str(uuid4())], 'depth': 0}
    )
    data = parse_tool_result(result)

    assert len(data[0]['toc']) == 1
    assert len(data[0]['toc'][0]['children']) == 2
    assert data[0]['toc'][0]['children'][0]['title'] == 'Section 1.1'
    assert data[0]['toc'][0]['children'][1]['title'] == 'Section 1.2'


@pytest.mark.asyncio
async def test_memex_get_page_indices_depth_0_trims_grandchildren(mock_api, mcp_client):
    """depth=0 trims H3+ levels from the response."""
    page_index = {
        'metadata': {'title': 'Test', 'total_tokens': 500},
        'toc': [
            {
                'id': str(uuid4()),
                'title': 'Root',
                'level': 1,
                'token_estimate': 100,
                'children': [
                    {
                        'id': str(uuid4()),
                        'title': 'Child',
                        'level': 2,
                        'token_estimate': 100,
                        'children': [
                            {
                                'id': str(uuid4()),
                                'title': 'Grandchild',
                                'level': 3,
                                'token_estimate': 50,
                                'children': [],
                            },
                        ],
                    },
                ],
            },
        ],
    }
    mock_api.get_note_page_index.return_value = page_index

    result = await mcp_client.call_tool(
        'memex_get_page_indices', {'note_ids': [str(uuid4())], 'depth': 0}
    )
    data = parse_tool_result(result)

    child = data[0]['toc'][0]['children'][0]
    assert child['title'] == 'Child'
    assert child['children'] == []


@pytest.mark.asyncio
async def test_memex_get_page_indices_depth_1_returns_full_tree(mock_api, mcp_client):
    """depth=1 returns the complete tree with no trimming."""
    page_index = _make_nested_page_index()
    mock_api.get_note_page_index.return_value = page_index

    result = await mcp_client.call_tool(
        'memex_get_page_indices', {'note_ids': [str(uuid4())], 'depth': 1}
    )
    data = parse_tool_result(result)

    # Full tree: grandchild should be present
    assert len(data[0]['toc'][0]['children']) == 1
    child = data[0]['toc'][0]['children'][0]
    assert len(child['children']) == 1
    assert child['children'][0]['title'] == 'Grandchild 1.1.1'


@pytest.mark.asyncio
async def test_memex_get_page_indices_depth_1_total_tokens_includes_all(mock_api, mcp_client):
    """depth=1 total_tokens includes all nodes in the tree."""
    page_index = _make_nested_page_index()
    mock_api.get_note_page_index.return_value = page_index

    result = await mcp_client.call_tool(
        'memex_get_page_indices', {'note_ids': [str(uuid4())], 'depth': 1}
    )
    data = parse_tool_result(result)

    # All nodes: Root 1 (300) + Child 1.1 (150) + Grandchild 1.1.1 (50) + Root 2 (500)
    assert data[0]['total_tokens'] == 1000


@pytest.mark.asyncio
async def test_memex_get_page_indices_depth_0_flat_note(mock_api, mcp_client):
    """depth=0 on a note with only flat H1 sections returns them unchanged."""
    page_index = {
        'metadata': {'title': 'Flat', 'total_tokens': 300},
        'toc': [
            {
                'id': str(uuid4()),
                'title': 'A',
                'level': 1,
                'token_estimate': 100,
                'children': [],
            },
            {
                'id': str(uuid4()),
                'title': 'B',
                'level': 1,
                'token_estimate': 200,
                'children': [],
            },
        ],
    }
    mock_api.get_note_page_index.return_value = page_index

    result = await mcp_client.call_tool(
        'memex_get_page_indices', {'note_ids': [str(uuid4())], 'depth': 0}
    )
    data = parse_tool_result(result)

    assert len(data[0]['toc']) == 2
    assert data[0]['toc'][0]['children'] == []
    assert data[0]['toc'][1]['children'] == []
    assert data[0]['total_tokens'] == 300


@pytest.mark.asyncio
async def test_memex_get_page_indices_depth_0_with_parent_node(mock_api, mcp_client):
    """depth=0 + parent_node_id returns subtree children with one level of their children."""
    page_index = _make_nested_page_index()
    root_id = page_index['toc'][0]['id']
    mock_api.get_note_page_index.return_value = page_index

    result = await mcp_client.call_tool(
        'memex_get_page_indices',
        {'note_ids': [str(uuid4())], 'depth': 0, 'parent_node_id': root_id},
    )
    data = parse_tool_result(result)

    # Subtree of Root 1 is [Child 1.1]; depth=0 includes its children
    assert len(data[0]['toc']) == 1
    assert data[0]['toc'][0]['title'] == 'Child 1.1'
    assert len(data[0]['toc'][0]['children']) == 1
    assert data[0]['toc'][0]['children'][0]['title'] == 'Grandchild 1.1.1'


@pytest.mark.asyncio
async def test_memex_get_page_indices_depth_high_value_returns_full(mock_api, mcp_client):
    """Any depth >= 1 returns the full tree."""
    page_index = _make_nested_page_index()
    mock_api.get_note_page_index.return_value = page_index

    result = await mcp_client.call_tool(
        'memex_get_page_indices', {'note_ids': [str(uuid4())], 'depth': 99}
    )
    data = parse_tool_result(result)

    # Full tree preserved
    grandchild = data[0]['toc'][0]['children'][0]['children'][0]
    assert grandchild['title'] == 'Grandchild 1.1.1'
