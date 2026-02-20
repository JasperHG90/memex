from datetime import datetime, timezone
from uuid import uuid4


from memex_core.memory.sql_models import Entity, ReflectionQueue, ReflectionStatus


def test_entity_schema_resonance_fields():
    """Verify that Entity has the new resonance fields."""
    entity = Entity(
        id=uuid4(),
        canonical_name='Test Entity',
        retrieval_count=5,
        last_retrieved_at=datetime.now(timezone.utc),
    )

    assert entity.retrieval_count == 5
    assert entity.last_retrieved_at is not None
    assert entity.mention_count == 1  # Default


def test_entity_defaults():
    """Verify default values for new Entity fields."""
    entity = Entity(id=uuid4(), canonical_name='Default Entity')

    assert entity.retrieval_count == 0
    assert entity.last_retrieved_at is None


def test_reflection_queue_schema_priority_fields():
    """Verify that ReflectionQueue uses float priority and tracks evidence."""
    entity_id = uuid4()
    queue_item = ReflectionQueue(
        entity_id=entity_id,
        priority_score=15.5,
        accumulated_evidence=10,
        status=ReflectionStatus.PENDING,
    )

    assert isinstance(queue_item.priority_score, float)
    assert queue_item.priority_score == 15.5
    assert queue_item.accumulated_evidence == 10


def test_reflection_queue_defaults():
    """Verify default values for ReflectionQueue."""
    entity_id = uuid4()
    queue_item = ReflectionQueue(entity_id=entity_id)

    assert queue_item.priority_score == 1.0
    assert isinstance(queue_item.priority_score, float)
    assert queue_item.accumulated_evidence == 0
    assert queue_item.status == ReflectionStatus.PENDING
