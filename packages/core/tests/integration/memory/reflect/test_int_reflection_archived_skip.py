"""Reflection must skip — not fail — entities whose only mental model is archived.

When an operator archives an entity's mental model (``archive_mental_model``),
the reflection SELECT (``archived_at IS NULL``) finds nothing, and the full
``(entity_id, vault_id)`` unique index blocks creating a fresh active row. The
entity is therefore left absent from ``_batch_get_or_create_models``.

Before the fix, ``_process_entity_reflection`` did ``models_map[eid]`` → KeyError
→ caught → routed to ``failed`` → ``mark_failed`` → retries → DEAD_LETTER (observed
in production for entity "MCP servers"). The fix skips such entities cleanly: they
appear in NONE of ``(models, abandoned, failed)`` so the service resolves their
queue task instead of failing it. This test pins that contract.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from memex_core.config import MemexConfig
from memex_core.memory.reflect.models import ReflectionRequest
from memex_core.memory.reflect.reflection import ReflectionEngine
from memex_core.memory.sql_models import Entity, MentalModel, Vault


@pytest.mark.integration
@pytest.mark.asyncio
async def test_archived_only_entity_is_skipped_not_failed(
    metastore, memex_config: MemexConfig
) -> None:
    async with metastore.session() as s:
        vault = Vault(id=uuid4(), name=f'archived-skip-{uuid4().hex[:8]}')
        entity = Entity(id=uuid4(), canonical_name='MCP servers')
        s.add_all([vault, entity])
        await s.commit()
        # The entity's ONLY mental model is archived.
        s.add(
            MentalModel(
                id=uuid4(),
                entity_id=entity.id,
                vault_id=vault.id,
                name='MCP servers',
                observations=[],
                archived_at=datetime.now(timezone.utc),
            )
        )
        await s.commit()
        vault_id, entity_id = vault.id, entity.id

    async with metastore.session() as s:
        engine = ReflectionEngine(session=s, config=memex_config, embedder=MagicMock())

        # Must not raise (previously KeyError on models_map[eid]).
        models, abandoned, failed = await engine.reflect_batch(
            [ReflectionRequest(entity_id=entity_id, vault_id=vault_id)]
        )

    # Skipped == present in NONE of the three outcome lists. In particular NOT
    # in `failed` (which is what dead-letters the entity).
    assert entity_id not in {m.entity_id for m in models}
    assert entity_id not in set(abandoned)
    assert entity_id not in set(failed), 'archived-only entity must not be routed to failure'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_batch_get_or_create_leaves_archived_only_entity_absent(
    metastore, memex_config: MemexConfig
) -> None:
    async with metastore.session() as s:
        vault = Vault(id=uuid4(), name=f'archived-absent-{uuid4().hex[:8]}')
        entity = Entity(id=uuid4(), canonical_name='Rituals')
        s.add_all([vault, entity])
        await s.commit()
        s.add(
            MentalModel(
                id=uuid4(),
                entity_id=entity.id,
                vault_id=vault.id,
                name='Rituals',
                observations=[],
                archived_at=datetime.now(timezone.utc),
            )
        )
        await s.commit()
        vault_id, entity_id = vault.id, entity.id

    async with metastore.session() as s:
        engine = ReflectionEngine(session=s, config=memex_config, embedder=MagicMock())
        models_map = await engine._batch_get_or_create_models([entity_id], vault_id=vault_id)

    # No ACTIVE model exists and the unique index blocks creating one → absent.
    assert entity_id not in models_map
