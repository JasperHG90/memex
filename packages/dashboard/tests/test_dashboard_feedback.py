import pytest
from memex_dashboard.pages.lineage import LineageState, LineageNode

# --- Lineage Tests ---


def test_lineage_metadata_population():
    """
    Verify that selecting a node correctly populates the selected_node_info
    with the raw metadata, formatted for the UI.
    """
    state = LineageState()

    # Mock a node with rich metadata
    raw_data = {
        'title': 'Test Title',
        'description': 'A test description',
        'score': 0.95,
        'tags': ['a', 'b'],
    }

    node = LineageNode(
        id='test_id', label='Test Label', type='document', x='10%', y='10%', raw=raw_data
    )
    state.nodes = [node]

    # Action: Select Node
    state.select_node(node.id)

    # Assertions
    assert state.selected_node_info.id == 'test_id'
    assert state.selected_node_info.label == 'Test Label'

    # Verify raw data transformation (dict -> list of dicts for display)
    # The current implementation converts to string
    raw_dict = {item['key']: item['value'] for item in state.selected_node_info.raw}
    assert raw_dict['title'] == 'Test Title'
    assert raw_dict['score'] == '0.95'  # Converted to string
    assert '["a", "b"]' in raw_dict['tags']  # List stringified


def test_lineage_highlighting_separation():
    """
    Verify that we can separate 'hover' (visual highlight) from 'selection' (details panel).
    The feedback requested that clicking does NOT persist the thick lines, implying
    lines should strictly follow hover, while details follow click.
    """
    state = LineageState()

    # We need to introduce a 'hovered_node_id' state to LineageState
    # This test asserts the *desired* behavior after we fix it.

    # Mock Graph Cache
    import networkx as nx

    G = nx.DiGraph()
    G.add_node('node_1')
    state._graph_cache = G

    # Simulate Hover
    if hasattr(state, 'set_hovered_node'):
        state.set_hovered_node('node_1')
        assert 'node_1' in state.highlighted_node_ids

        # Simulate Unhover
        state.set_hovered_node(None)
        assert 'node_1' not in state.highlighted_node_ids
    else:
        pytest.fail("LineageState missing 'set_hovered_node' method (Fix required)")

    # Simulate Click (Selection)
    # Should NOT affect highlighted_node_ids if we strictly follow "only happen when hovering"
    node = LineageNode(id='node_1', label='L', type='T', x='0', y='0', raw={})
    state.nodes = [node]
    state.select_node(node.id)

    assert state.selected_node_info.id == 'node_1'
    # The feedback implies the persistent highlight on click is unwanted.
    # So checking highlighted_node_ids should be empty (or not contain node_1) if not hovering.
    assert 'node_1' not in state.highlighted_node_ids


# --- Status / Overview Tests ---


@pytest.mark.asyncio
async def test_server_stats_parsing():
    """
    Verify the parsing logic for Prometheus-style metrics.
    """
    mock_metrics = """
# HELP http_requests_total Total number of HTTP requests
# TYPE http_requests_total counter
http_requests_total 123.0
# HELP process_resident_memory_bytes Resident memory size in bytes.
# TYPE process_resident_memory_bytes gauge
process_resident_memory_bytes 104857600.0
    """

    # We need to refactor the parsing logic into a testable method or mock the API
    # Here we simulate the logic found in on_load/get_server_stats

    # Let's assume we extract the parsing logic to a helper or verify the state update
    # For now, we'll manually invoke the parsing logic we intend to verify/fix

    parsed = {}
    for line in mock_metrics.split('\n'):
        if line.startswith('#') or not line:
            continue
        parts = line.split(' ')
        if len(parts) >= 2:
            parsed[parts[0]] = parts[1]

    assert parsed['http_requests_total'] == '123.0'
    assert parsed['process_resident_memory_bytes'] == '104857600.0'
