import pytest
from uuid import uuid4
from datetime import datetime, timezone

from memex_core.memory.sql_models import Note, MemoryUnit, MentalModel, Entity, Vault
from memex_common.types import FactTypes
from memex_common.schemas import LineageDirection


@pytest.mark.asyncio
async def test_reproduce_observation_lineage_failure(api, metastore):
    """
    Reproduce the 500 error when fetching lineage for an observation.
    """
    # 1. Setup Data
    vault_id = uuid4()
    entity_id = uuid4()
    obs_id = uuid4()
    mem_unit_id = uuid4()
    doc_id = uuid4()

    async with metastore.session() as session:
        # Vault
        session.add(Vault(id=vault_id, name='Test Vault'))
        await session.commit()

        # Entity
        session.add(Entity(id=entity_id, canonical_name='Test Entity', vault_id=vault_id))
        # Document
        session.add(Note(id=doc_id, vault_id=vault_id, original_text='Test Doc'))
        # Memory Unit
        session.add(
            MemoryUnit(
                id=mem_unit_id,
                note_id=doc_id,
                text='Test Fact',
                fact_type=FactTypes.WORLD,
                vault_id=vault_id,
                embedding=[0.0] * 384,
                event_date=datetime.now(timezone.utc),
            )
        )

        # Mental Model with Observation
        # Observation contains evidence pointing to the Memory Unit
        obs_data = {
            'id': str(obs_id),
            'title': 'Test Observation',
            'content': 'Test Content',
            'trend': 'new',
            'evidence': [
                {
                    'memory_id': str(mem_unit_id),  # Valid UUID string
                    'quote': 'Test Quote',
                    'relevance': 1.0,
                    'explanation': 'Because',
                }
            ],
        }

        mm = MentalModel(
            entity_id=entity_id,
            vault_id=vault_id,
            name='Test Entity',
            observations=[obs_data],
            version=1,
        )
        session.add(mm)
        await session.commit()

        # 2. Call Lineage (Upstream)
        # This triggers _get_lineage_upstream for 'observation'
        try:
            lineage = await api.get_lineage(
                entity_type='observation', entity_id=obs_id, direction=LineageDirection.UPSTREAM
            )
        except Exception as e:
            pytest.fail(f'Lineage fetch failed with: {e}')

        # 3. Verify
        assert lineage.entity_type == 'observation'
        assert lineage.entity['id'] == str(obs_id)
        assert len(lineage.derived_from) == 1
        assert lineage.derived_from[0].entity_type == 'memory_unit'
        assert str(lineage.derived_from[0].entity['id']) == str(mem_unit_id)


@pytest.mark.asyncio
async def test_get_note_returns_404_not_500(api):
    """
    Verify that get_note raises ResourceNotFoundError for missing documents,
    which the server translates to 404.
    """
    from memex_common.exceptions import ResourceNotFoundError

    missing_id = uuid4()

    with pytest.raises(ResourceNotFoundError):
        await api.get_note(missing_id)
