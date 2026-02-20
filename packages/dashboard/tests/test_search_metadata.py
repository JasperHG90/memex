from memex_dashboard.pages.search import SearchState, SearchResult


def test_open_details_populates_metadata_correctly():
    state = SearchState()

    # Mock result
    result = SearchResult(
        id='test1',
        text='Content',
        fact_type='memory',
        score=1.0,
        metadata={'key1': 'value1', 'key2': 'value2'},
    )

    state.open_details(result)

    assert state.selected_result.id == 'test1'
    # We expect metadata_list to be list of dicts
    metadata = state.metadata_list
    assert {'key': 'key1', 'value': 'value1'} in metadata
    assert {'key': 'key2', 'value': 'value2'} in metadata
