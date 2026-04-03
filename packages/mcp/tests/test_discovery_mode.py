"Tests for the DiscoveryMode progressive disclosure transform."

import pytest
from fastmcp import Client
from fastmcp.server.transforms.search.bm25 import BM25SearchTransform

from memex_mcp.server import mcp
from memex_mcp.transforms import DiscoveryMode


EXPECTED_TAGS = {'search', 'read', 'write', 'browse', 'entities', 'assets', 'storage'}

# Natural-language queries mapped to expected tool(s) — used for BM25 recall testing.
BM25_TEST_CASES: list[tuple[str, list[str]]] = [
    ('search for information about a topic', ['memex_memory_search', 'memex_note_search']),
    ('find notes about machine learning', ['memex_memory_search', 'memex_note_search']),
    (
        'what entities are related to this concept',
        ['memex_list_entities', 'memex_get_entity_cooccurrences'],
    ),
    ('read the content of a specific note', ['memex_read_note', 'memex_get_nodes']),
    ('get the table of contents of a note', ['memex_get_page_indices']),
    ('store a user preference', ['memex_kv_write']),
    ('look up a stored fact', ['memex_kv_get', 'memex_kv_search']),
    ('add a new document to memex', ['memex_add_note']),
    ('list all my vaults', ['memex_list_vaults']),
    ('find a note by its title', ['memex_find_note']),
    ('what notes were added recently', ['memex_recent_notes']),
    ('upload an image to a note', ['memex_add_assets']),
    ('view attachments on a note', ['memex_list_assets', 'memex_get_resources']),
    ('trace the provenance of a fact', ['memex_get_lineage']),
    ('get metadata for multiple notes', ['memex_get_notes_metadata']),
    ('which entities co-occur together', ['memex_get_entity_cooccurrences']),
    ('find facts that mention a person', ['memex_get_entity_mentions']),
    ('browse notes from last month', ['memex_list_notes', 'memex_recent_notes']),
    ('delete an attachment from a note', ['memex_delete_assets']),
    ('get a template for creating notes', ['memex_get_template', 'memex_list_templates']),
    ('what is the active vault', ['memex_active_vault', 'memex_list_vaults']),
    ('rename a note', ['memex_rename_note']),
    ('archive a note', ['memex_set_note_status']),
    ('semantic search over stored preferences', ['memex_kv_search']),
    ('list all key-value entries', ['memex_kv_list']),
    ('batch read memory units by ID', ['memex_get_memory_units']),
    ('explore the knowledge graph', ['memex_list_entities', 'memex_get_entity_cooccurrences']),
    ('how does this fact connect to that document', ['memex_get_lineage']),
    ('register a custom note template', ['memex_register_template']),
    ('supersede an outdated note', ['memex_set_note_status']),
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def discovery_mcp():
    """Return a copy of the MCP server with DiscoveryMode applied."""
    transform = DiscoveryMode()
    original_transforms = list(mcp._transforms) if hasattr(mcp, '_transforms') else []
    mcp.add_transform(transform)
    yield mcp
    # Restore original transforms
    if hasattr(mcp, '_transforms'):
        mcp._transforms = original_transforms


# ---------------------------------------------------------------------------
# Tag coverage
# ---------------------------------------------------------------------------


async def test_all_tools_have_tags():
    """Every registered tool must have at least one tag."""
    tools = await mcp._list_tools()
    untagged = [t.name for t in tools if not t.tags]
    assert not untagged, f'Tools without tags: {untagged}'


async def test_tag_taxonomy():
    """All tools must use only the expected tag set."""
    tools = await mcp._list_tools()
    all_tags: set[str] = set()
    for t in tools:
        all_tags |= t.tags or set()
    assert all_tags == EXPECTED_TAGS, f'Unexpected tags: {all_tags - EXPECTED_TAGS}'


# ---------------------------------------------------------------------------
# Discovery mode behaviour
# ---------------------------------------------------------------------------


async def test_list_tools_returns_only_meta_tools(discovery_mcp):
    """With DiscoveryMode active, tools/list should return only the 3 meta-tools."""
    async with Client(discovery_mcp) as client:
        tools = await client.list_tools()
    names = {t.name for t in tools}
    assert names == {'memex_tags', 'memex_search', 'memex_get_schema'}


async def test_tags_returns_categories(discovery_mcp):
    """memex_tags should return all 7 categories."""
    async with Client(discovery_mcp) as client:
        result = await client.call_tool('memex_tags', {'detail': 'brief'})
    text = result.content[0].text
    for tag in EXPECTED_TAGS:
        assert tag in text, f'Tag {tag!r} not found in memex_tags output'


async def test_search_finds_tools(discovery_mcp):
    """memex_search should return relevant tools for a keyword query."""
    async with Client(discovery_mcp) as client:
        result = await client.call_tool('memex_search', {'query': 'search notes'})
    text = result.content[0].text
    assert 'memex_note_search' in text or 'memex_memory_search' in text


async def test_search_with_tag_filter(discovery_mcp):
    """memex_search with tags filter should only return tools from that tag."""
    async with Client(discovery_mcp) as client:
        result = await client.call_tool('memex_search', {'query': 'list', 'tags': ['assets']})
    text = result.content[0].text
    assert 'memex_list_assets' in text
    # Should NOT contain tools from other tags
    assert 'memex_list_notes' not in text
    assert 'memex_list_vaults' not in text


async def test_get_schema_returns_params(discovery_mcp):
    """memex_get_schema should return parameter details for a named tool."""
    async with Client(discovery_mcp) as client:
        result = await client.call_tool('memex_get_schema', {'tools': ['memex_memory_search']})
    text = result.content[0].text
    assert 'memex_memory_search' in text
    assert 'query' in text  # main parameter


async def test_real_tools_still_callable(discovery_mcp, mock_api, mock_config):
    """Real tools should remain callable by name even with DiscoveryMode active."""
    mock_api.list_vaults.return_value = []
    async with Client(discovery_mcp) as client:
        result = await client.call_tool('memex_list_vaults', {})
    # Should not raise — the tool should be dispatched through get_tool passthrough
    assert result is not None


async def test_disabled_by_default():
    """Without DiscoveryMode, all 31+ tools should be visible."""
    async with Client(mcp) as client:
        tools = await client.list_tools()
    names = {t.name for t in tools}
    assert 'memex_memory_search' in names
    assert 'memex_list_vaults' in names
    assert len(names) >= 31


# ---------------------------------------------------------------------------
# BM25 recall
# ---------------------------------------------------------------------------


async def test_bm25_recall_at_3():
    """At least 90% of natural queries should hit the expected tool in the top 3."""
    tools = await mcp._list_tools()
    bm25 = BM25SearchTransform(max_results=5)
    hits = 0
    misses: list[tuple[str, list[str], list[str]]] = []
    for query, expected in BM25_TEST_CASES:
        results = await bm25._search(tools, query)
        top3 = {t.name for t in results[:3]}
        if set(expected) & top3:
            hits += 1
        else:
            misses.append((query, expected, [t.name for t in results[:5]]))
    rate = hits / len(BM25_TEST_CASES)
    assert rate >= 0.9, f'BM25 hit@3 = {rate:.0%} (expected >= 90%). Misses:\n' + '\n'.join(
        f'  {q!r} expected={e} got={g}' for q, e, g in misses
    )
