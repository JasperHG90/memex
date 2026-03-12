"""Entity service — CRUD and query operations for entities."""

from __future__ import annotations

import logging
from typing import Any, AsyncGenerator
from uuid import UUID

from sqlmodel import col

from memex_common.exceptions import EntityNotFoundError, ResourceNotFoundError

from memex_core.services.base import BaseService

logger = logging.getLogger('memex.core.services.entities')


def _attach_metadata(entity: Any, mental_model: Any | None) -> Any:
    """Attach mental model entity_metadata to an entity as a transient attribute."""
    entity._mental_model_metadata = (mental_model.entity_metadata if mental_model else None) or {}
    return entity


class EntityService(BaseService):
    """Entity CRUD, search, and graph traversal operations."""

    async def list_entities_ranked(
        self,
        limit: int = 100,
        vault_ids: list[UUID] | None = None,
        entity_type: str | None = None,
    ) -> AsyncGenerator[Any, None]:
        """
        Stream entities ranked by hybrid score.
        Hybrid Score = 0.4 * mention_count + 0.4 * retrieval_count + 0.2 * centrality
        """
        from memex_core.memory.sql_models import Entity, EntityCooccurrence, MentalModel, UnitEntity
        from sqlmodel import select, func, desc, col

        # Subquery for centrality (sum of cooccurrence counts)
        centrality_stmt = (
            select(
                func.coalesce(func.sum(EntityCooccurrence.cooccurrence_count), 0).label(
                    'centrality'
                ),
                Entity.id.label('entity_id'),
            )
            .select_from(Entity)
            .outerjoin(
                EntityCooccurrence,
                (EntityCooccurrence.entity_id_1 == Entity.id)
                | (EntityCooccurrence.entity_id_2 == Entity.id),
            )
            .group_by(Entity.id)
        ).subquery()

        stmt = (
            select(Entity, MentalModel)
            .join(centrality_stmt, centrality_stmt.c.entity_id == Entity.id)
            .outerjoin(MentalModel, MentalModel.entity_id == Entity.id)
        )

        if vault_ids:
            stmt = (
                stmt.join(UnitEntity, col(UnitEntity.entity_id) == Entity.id)
                .where(col(UnitEntity.vault_id).in_(vault_ids))
                .distinct()
            )

        if entity_type:
            stmt = stmt.where(Entity.entity_type == entity_type)

        stmt = stmt.order_by(
            desc(
                0.4 * Entity.mention_count
                + 0.4 * Entity.retrieval_count
                + 0.2 * centrality_stmt.c.centrality
            )
        ).limit(limit)

        async with self.metastore.session() as session:
            stream = await session.stream(stmt)
            async for row in stream:
                yield _attach_metadata(row[0], row[1])

    async def get_entity_cooccurrences(
        self,
        entity_id: UUID | str,
        vault_ids: list[UUID] | None = None,
        limit: int = 50,
    ) -> list[Any]:
        """Get co-occurrence edges for an entity."""
        from sqlalchemy.orm import selectinload
        from sqlmodel import desc, or_, select

        from memex_core.memory.sql_models import EntityCooccurrence

        eid = UUID(str(entity_id))
        async with self.metastore.session() as session:
            stmt = (
                select(EntityCooccurrence)
                .options(
                    selectinload(EntityCooccurrence.entity_1),  # type: ignore[arg-type]
                    selectinload(EntityCooccurrence.entity_2),  # type: ignore[arg-type]
                )
                .where(
                    or_(
                        EntityCooccurrence.entity_id_1 == eid,
                        EntityCooccurrence.entity_id_2 == eid,
                    )
                )
            )
            if vault_ids:
                stmt = stmt.where(col(EntityCooccurrence.vault_id).in_(vault_ids))
            stmt = stmt.order_by(desc(EntityCooccurrence.cooccurrence_count)).limit(limit)
            return list((await session.exec(stmt)).all())

    async def get_bulk_cooccurrences(
        self, entity_ids: list[UUID], vault_ids: list[UUID] | None = None
    ) -> list[Any]:
        """Get co-occurrences between a set of entities."""
        from memex_core.memory.sql_models import EntityCooccurrence
        from sqlmodel import col, select

        async with self.metastore.session() as session:
            stmt = select(EntityCooccurrence).where(
                (col(EntityCooccurrence.entity_id_1).in_(entity_ids))
                & (col(EntityCooccurrence.entity_id_2).in_(entity_ids))
            )
            if vault_ids:
                stmt = stmt.where(col(EntityCooccurrence.vault_id).in_(vault_ids))
            return list((await session.exec(stmt)).all())

    async def get_entity_mentions(
        self, entity_id: UUID | str, limit: int = 20, vault_ids: list[UUID] | None = None
    ) -> list[dict[str, Any]]:
        """Get memory units and source documents where this entity is mentioned."""
        from memex_core.memory.sql_models import MemoryUnit, Note, UnitEntity
        from sqlmodel import desc, select

        eid = UUID(str(entity_id))
        async with self.metastore.session() as session:
            stmt = (
                select(MemoryUnit, Note)
                .join(UnitEntity, UnitEntity.unit_id == MemoryUnit.id)
                .join(Note, MemoryUnit.note_id == Note.id)
                .where(UnitEntity.entity_id == eid)
            )
            if vault_ids:
                stmt = stmt.where(col(MemoryUnit.vault_id).in_(vault_ids))
            stmt = stmt.order_by(desc(MemoryUnit.created_at)).limit(limit)
            results = (await session.exec(stmt)).all()
            return [{'unit': unit, 'document': doc} for unit, doc in results]

    async def get_entity(self, entity_id: UUID | str) -> Any | None:
        """Get an entity by ID, with MentalModel metadata attached."""
        from memex_core.memory.sql_models import Entity, MentalModel
        from sqlmodel import select

        eid = UUID(str(entity_id))
        async with self.metastore.session() as session:
            stmt = (
                select(Entity, MentalModel)
                .outerjoin(MentalModel, MentalModel.entity_id == Entity.id)
                .where(Entity.id == eid)
            )
            result = (await session.exec(stmt)).first()
            if not result:
                return None
            return _attach_metadata(result[0], result[1])

    async def get_entities(self, entity_ids: list[UUID]) -> list[Any]:
        """Get multiple entities by ID, with MentalModel metadata attached."""
        from memex_core.memory.sql_models import Entity, MentalModel
        from sqlmodel import select

        async with self.metastore.session() as session:
            stmt = (
                select(Entity, MentalModel)
                .outerjoin(MentalModel, MentalModel.entity_id == Entity.id)
                .where(col(Entity.id).in_(entity_ids))
            )
            results = (await session.exec(stmt)).all()
            return [_attach_metadata(row[0], row[1]) for row in results]

    async def delete_entity(self, entity_id: UUID) -> bool:
        """
        Delete an entity and all associated data.

        Explicit cleanup: MentalModel rows (no FK cascade exists).
        ORM cascades handle: unit_entities, aliases, memory_links, cooccurrences.
        DB FK cascade handles: reflection_queue.
        """
        from memex_core.memory.sql_models import Entity, MentalModel
        from sqlmodel import select, col

        async with self.metastore.session() as session:
            entity = await session.get(Entity, entity_id)
            if not entity:
                raise EntityNotFoundError(f'Entity {entity_id} not found.')

            # Delete MentalModel rows explicitly (no FK cascade exists)
            stmt = select(MentalModel).where(col(MentalModel.entity_id) == entity_id)
            models = (await session.exec(stmt)).all()
            for model in models:
                await session.delete(model)

            # ORM cascades handle unit_entities, aliases, memory_links, cooccurrences
            # DB FK cascade handles reflection_queue
            await session.delete(entity)
            await session.commit()

        return True

    async def delete_mental_model(self, entity_id: UUID, vault_id: UUID) -> bool:
        """
        Delete a mental model for a specific entity in a specific vault.

        Does NOT delete the parent entity.
        """
        from memex_core.memory.sql_models import MentalModel
        from sqlmodel import select, col

        async with self.metastore.session() as session:
            stmt = select(MentalModel).where(
                (col(MentalModel.entity_id) == entity_id) & (col(MentalModel.vault_id) == vault_id)
            )
            model = (await session.exec(stmt)).first()
            if not model:
                raise ResourceNotFoundError(
                    f'Mental model for entity {entity_id} in vault {vault_id} not found.'
                )

            await session.delete(model)
            await session.commit()

        return True

    async def get_top_entities(
        self,
        limit: int = 5,
        vault_ids: list[UUID] | None = None,
        entity_type: str | None = None,
    ) -> list[Any]:
        """Get top entities by mention count, with MentalModel metadata attached."""
        from memex_core.memory.sql_models import Entity, MentalModel, UnitEntity
        from sqlmodel import select, desc, col

        async with self.metastore.session() as session:
            stmt = select(Entity, MentalModel).outerjoin(
                MentalModel, MentalModel.entity_id == Entity.id
            )
            if vault_ids:
                stmt = (
                    stmt.join(UnitEntity, col(UnitEntity.entity_id) == Entity.id)
                    .where(col(UnitEntity.vault_id).in_(vault_ids))
                    .distinct()
                )
            if entity_type:
                stmt = stmt.where(Entity.entity_type == entity_type)
            stmt = stmt.order_by(desc(Entity.mention_count)).limit(limit)
            results = (await session.exec(stmt)).all()
            return [_attach_metadata(row[0], row[1]) for row in results]

    async def search_entities(
        self,
        query: str,
        limit: int = 10,
        vault_ids: list[UUID] | None = None,
        entity_type: str | None = None,
    ) -> list[Any]:
        """Search for entities by canonical name using trigram similarity or ILIKE."""
        from memex_core.memory.sql_models import Entity, MentalModel, UnitEntity
        from sqlmodel import select, col

        async with self.metastore.session() as session:
            stmt = (
                select(Entity, MentalModel)
                .outerjoin(MentalModel, MentalModel.entity_id == Entity.id)
                .where(col(Entity.canonical_name).ilike(f'%{query}%'))
            )
            if vault_ids:
                stmt = (
                    stmt.join(UnitEntity, col(UnitEntity.entity_id) == Entity.id)
                    .where(col(UnitEntity.vault_id).in_(vault_ids))
                    .distinct()
                )
            if entity_type:
                stmt = stmt.where(Entity.entity_type == entity_type)
            stmt = stmt.order_by(col(Entity.mention_count).desc()).limit(limit)
            results = (await session.exec(stmt)).all()
            return [_attach_metadata(row[0], row[1]) for row in results]
