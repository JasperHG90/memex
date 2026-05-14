from memex_core.memory.sql_models import Entity


def test_entity_has_no_vault_id_column():
    """P7: Entity is the global noun-graph; vault scoping lives on joins, not on the row."""
    assert 'vault_id' not in {c.name for c in Entity.__table__.columns}
    assert 'vault_id' not in Entity.model_fields
