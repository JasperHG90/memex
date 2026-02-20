from uuid import uuid4
from memex_common.schemas import LineageDirection, LineageResponse


def test_lineage_direction_enum():
    assert LineageDirection.UPSTREAM == 'upstream'
    assert LineageDirection.DOWNSTREAM == 'downstream'
    assert LineageDirection.BOTH == 'both'


def test_lineage_response_structure():
    # Mock data
    entity_id = uuid4()
    child_id = uuid4()

    # Nested structure
    child_node = LineageResponse(
        entity_type='memory_unit',
        entity={'id': str(child_id), 'text': 'Something happened', 'fact_type': 'event'},
        derived_from=[],
    )

    root_node = LineageResponse(
        entity_type='observation',
        entity={'id': str(entity_id), 'content': 'An observation'},
        derived_from=[child_node],
    )

    assert root_node.entity_type == 'observation'
    assert len(root_node.derived_from) == 1
    assert root_node.derived_from[0].entity_type == 'memory_unit'
    assert root_node.derived_from[0].entity['id'] == str(child_id)


def test_lineage_response_serialization():
    entity_id = uuid4()
    response = LineageResponse(
        entity_type='document', entity={'id': str(entity_id), 'name': 'test.md'}, derived_from=[]
    )

    json_output = response.model_dump()
    assert json_output['entity_type'] == 'document'
    assert json_output['entity']['id'] == str(entity_id)
    assert json_output['derived_from'] == []
