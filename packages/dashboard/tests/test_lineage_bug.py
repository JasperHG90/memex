import pytest
from memex_dashboard.pages.lineage import LineageState


class MockEntity:
    def __init__(self, id, type, entity=None, derived_from=None):
        self.entity_type = type
        self.entity = entity or {'id': id, 'title': f'Title {id}'}
        self.derived_from = derived_from or []


@pytest.mark.asyncio
async def test_lineage_layer_key_error_repro():
    """
    Reproduces the KeyError: 'layer' bug when a Note has Assets.
    """
    state = LineageState()

    # Construct lineage: Model -> Observation -> Note -> Asset
    # The bug is suspected to be in how Note is handled when it has Assets.

    # Asset path
    asset_path = '/tmp/image.png'

    # Note with asset
    note_node = MockEntity(
        id='note1', type='note', entity={'id': 'note1', 'title': 'My Doc', 'assets': [asset_path]}
    )

    # Root node (Mental Model)
    root_node = MockEntity(id='model1', type='mental_model', derived_from=[note_node])

    # Attempt to generate layout
    try:
        state.generate_layout(root_node)
    except KeyError as e:
        pytest.fail(f'KeyError raised: {e}')
    except Exception as e:
        pytest.fail(f'Exception raised: {e}')

    # Check if nodes exist
    node_ids = [n.id for n in state.nodes]
    assert 'note1' in node_ids
    assert f'asset:{asset_path}' in node_ids

    # Check attributes
    # We expect note1 to have a label. If the bug exists, it might be missing label/type in G.nodes
    # But generate_layout reads G.nodes to create LineageNode.
    # If G.nodes['note1'] is missing 'label', it will raise KeyError: 'label' in the final loop.
