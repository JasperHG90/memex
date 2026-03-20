"""Tests for build_entity_dto with EntityWithMetadata wrapper."""

from uuid import uuid4

from memex_core.memory.sql_models import Entity
from memex_core.server.common import build_entity_dto
from memex_core.services.entities import EntityWithMetadata


def test_build_entity_dto_with_entity_with_metadata():
    """build_entity_dto should extract metadata from EntityWithMetadata wrapper."""
    entity = Entity(id=uuid4(), canonical_name='Alice', mention_count=5, entity_type='person')
    metadata = {'description': 'A person named Alice', 'category': 'person', 'observation_count': 3}
    wrapped = EntityWithMetadata(entity=entity, metadata=metadata)

    dto = build_entity_dto(wrapped)

    assert dto.name == 'Alice'
    assert dto.mention_count == 5
    assert dto.entity_type == 'person'
    assert dto.metadata == metadata


def test_build_entity_dto_with_plain_entity():
    """build_entity_dto should return empty metadata for a plain ORM entity."""
    entity = Entity(id=uuid4(), canonical_name='Bob', mention_count=2)

    dto = build_entity_dto(entity)

    assert dto.name == 'Bob'
    assert dto.mention_count == 2
    assert dto.metadata == {}
