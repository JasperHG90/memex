from memex_dashboard.pages.lineage import LineageState, LineageNode


def test_select_node_populates_raw_correctly():
    state = LineageState()

    # Mock node
    node = LineageNode(
        id='test1',
        label='Test Node',
        full_label='Full Test Node',
        type='memory_unit',
        x='10%',
        y='10%',
        raw={'key1': 'value1', 'key2': 'value2'},
    )
    state.nodes = [node]

    state.select_node('test1')

    assert state.selected_node_info.id == 'test1'
    # We expect raw to be list of dicts (Proposed Fix)
    raw = state.selected_node_info.raw
    assert {'key': 'key1', 'value': 'value1'} in raw
    assert {'key': 'key2', 'value': 'value2'} in raw
