"""Lineage service — provenance chain traversal for Memex entities."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import func, cast as sa_cast
from sqlalchemy.orm import defer
from sqlalchemy.types import UserDefinedType

from memex_common.exceptions import ResourceNotFoundError
from memex_common.schemas import LineageDirection, LineageResponse

from memex_core.services.base import BaseService

logger = logging.getLogger('memex.core.services.lineage')

# Fields to include in lineage response per entity type.
# Lineage returns only identification, metadata, and provenance-relevant fields.
# Full entity data is available via dedicated endpoints.
_NOTE_INCLUDE: set[str] = {
    'id',
    'vault_id',
    'title',
    'description',
    'status',
    'superseded_by',
    'appended_to',
    'publish_date',
    'created_at',
}
_UNIT_INCLUDE: set[str] = {
    'id',
    'vault_id',
    'text',
    'fact_type',
    'status',
    'confidence',
    'note_id',
    'event_date',
}
_MM_INCLUDE: set[str] = {
    'id',
    'vault_id',
    'entity_id',
    'name',
    'version',
    'last_refreshed',
}
_OBS_INCLUDE: set[str] = {'id', 'title', 'trend'}
_ENTITY_INCLUDE: set[str] = {'id', 'canonical_name', 'entity_type'}

# Max length for text fields in lineage responses (provenance, not full content).
_TEXT_TRUNCATE_LEN = 200


def _truncate_text_fields(entity_data: dict[str, Any]) -> dict[str, Any]:
    """Truncate long text fields in entity dicts to keep lineage responses compact."""
    text = entity_data.get('text')
    if isinstance(text, str) and len(text) > _TEXT_TRUNCATE_LEN:
        entity_data['text'] = text[:_TEXT_TRUNCATE_LEN] + '…'
    desc = entity_data.get('description')
    if isinstance(desc, str) and len(desc) > _TEXT_TRUNCATE_LEN:
        entity_data['description'] = desc[:_TEXT_TRUNCATE_LEN] + '…'
    return entity_data


class JSONPath(UserDefinedType):
    """SQLAlchemy custom type for Postgres jsonpath literals."""

    cache_ok = True

    def get_col_spec(self, **kw: Any) -> str:
        return 'jsonpath'


class LineageService(BaseService):
    """Traverses provenance chains across Memex entity types.

    Supports upstream (Mental Model -> Document) and downstream
    (Document -> Mental Model) lineage traversal with configurable depth.
    """

    async def get_lineage(
        self,
        entity_type: str,
        entity_id: UUID | str,
        direction: LineageDirection = LineageDirection.UPSTREAM,
        depth: int = 3,
        limit: int = 10,
    ) -> LineageResponse:
        """Retrieve the full lineage (dependency chain) of a specific entity.

        Args:
            entity_type: The type of the entity
                (mental_model, observation, memory_unit, note).
            entity_id: The UUID of the entity.
            direction: Direction of traversal (upstream, downstream, both).
            depth: Maximum recursion depth.
            limit: Maximum number of children per node.
        """
        if isinstance(entity_id, str):
            entity_id = UUID(entity_id)

        async with self.metastore.session() as session:
            if direction == LineageDirection.UPSTREAM:
                return await self._get_lineage_upstream(
                    session,
                    entity_type,
                    entity_id,
                    current_depth=0,
                    max_depth=depth,
                    limit=limit,
                )
            elif direction == LineageDirection.DOWNSTREAM:
                return await self._get_lineage_downstream(
                    session,
                    entity_type,
                    entity_id,
                    current_depth=0,
                    max_depth=depth,
                    limit=limit,
                )
            else:  # BOTH
                upstream = await self._get_lineage_upstream(
                    session,
                    entity_type,
                    entity_id,
                    current_depth=0,
                    max_depth=depth,
                    limit=limit,
                )
                downstream = await self._get_lineage_downstream(
                    session,
                    entity_type,
                    entity_id,
                    current_depth=0,
                    max_depth=depth,
                    limit=limit,
                )
                upstream.derived_from.extend(downstream.derived_from)
                return upstream

    async def _get_lineage_downstream(
        self,
        session: Any,
        entity_type: str,
        entity_id: UUID,
        current_depth: int,
        max_depth: int,
        limit: int,
    ) -> LineageResponse:
        """Recursive helper for downstream lineage (Document -> Mental Model)."""
        from memex_core.memory.sql_models import MentalModel, MemoryUnit, Note, Entity
        from sqlmodel import select, col

        entity_data: dict[str, Any] = {}
        children: list[LineageResponse] = []
        stop_recursion = current_depth >= max_depth

        if entity_type == 'note':
            stmt = (
                select(Note)
                .where(col(Note.id) == entity_id)
                .options(defer(Note.original_text), defer(Note.page_index))  # type: ignore
            )
            obj = (await session.exec(stmt)).first()
            if not obj:
                logger.warning('Lineage downstream: note %s not found', entity_id)
                raise ResourceNotFoundError(f'Note {entity_id} not found.')
            entity_data = _truncate_text_fields(obj.model_dump(include=_NOTE_INCLUDE))

            if not stop_recursion:
                stmt = (
                    select(MemoryUnit)
                    .where(col(MemoryUnit.note_id) == entity_id)
                    .options(  # type: ignore
                        defer(MemoryUnit.embedding),
                        defer(MemoryUnit.context),
                        defer(MemoryUnit.search_tsvector),
                    )
                    .limit(limit)
                )
                units = (await session.exec(stmt)).all()
                for unit in units:
                    child_node = await self._get_lineage_downstream(
                        session,
                        'memory_unit',
                        unit.id,
                        current_depth + 1,
                        max_depth,
                        limit,
                    )
                    children.append(child_node)

        elif entity_type == 'memory_unit':
            stmt = (
                select(MemoryUnit)
                .where(col(MemoryUnit.id) == entity_id)
                .options(  # type: ignore
                    defer(MemoryUnit.embedding),
                    defer(MemoryUnit.context),
                    defer(MemoryUnit.search_tsvector),
                )
            )
            obj = (await session.exec(stmt)).first()
            if not obj:
                logger.warning('Lineage downstream: memory_unit %s not found', entity_id)
                raise ResourceNotFoundError(f'Memory Unit {entity_id} not found.')
            entity_data = _truncate_text_fields(obj.model_dump(include=_UNIT_INCLUDE))

            if not stop_recursion:
                stmt = (
                    select(MentalModel)
                    .where(
                        func.jsonb_path_exists(
                            MentalModel.observations,
                            sa_cast(
                                f'$[*].evidence[*].memory_id ? (@ == "{entity_id}")',
                                JSONPath(),
                            ),
                        )
                    )
                    .options(defer(MentalModel.embedding))  # type: ignore
                )
                mms = (await session.exec(stmt)).all()

                for mm in mms:
                    for obs in mm.observations:
                        evidence = obs.get('evidence', [])
                        if any(e.get('memory_id') == str(entity_id) for e in evidence):
                            obs_id = obs.get('id')
                            if obs_id:
                                try:
                                    child_node = await self._get_lineage_downstream(
                                        session,
                                        'observation',
                                        UUID(str(obs_id)),
                                        current_depth + 1,
                                        max_depth,
                                        limit,
                                    )
                                    children.append(child_node)
                                except ValueError:
                                    pass

        elif entity_type == 'observation':
            stmt = (
                select(MentalModel)
                .where(
                    func.jsonb_path_exists(
                        MentalModel.observations,
                        sa_cast(f'$[*] ? (@.id == "{entity_id}")', JSONPath()),
                    )
                )
                .options(defer(MentalModel.embedding))  # type: ignore
            )
            parent_mm = (await session.exec(stmt)).first()

            if not parent_mm:
                raise ResourceNotFoundError(f'Observation {entity_id} not found.')

            target_obs = None
            for obs in parent_mm.observations:
                if str(obs.get('id')) == str(entity_id):
                    target_obs = obs
                    break

            if not target_obs:
                raise ResourceNotFoundError(f'Observation {entity_id} not found.')

            entity_data = {k: v for k, v in target_obs.items() if k in _OBS_INCLUDE}

            if not stop_recursion:
                child_node = await self._get_lineage_downstream(
                    session,
                    'mental_model',
                    parent_mm.entity_id,
                    current_depth + 1,
                    max_depth,
                    limit,
                )
                children.append(child_node)

        elif entity_type == 'mental_model':
            stmt = (
                select(MentalModel)
                .where(
                    (col(MentalModel.id) == entity_id) | (col(MentalModel.entity_id) == entity_id)
                )
                .options(defer(MentalModel.embedding))  # type: ignore
            )
            results = (await session.exec(stmt)).all()

            if not results:
                stmt_ent = select(Entity).where(col(Entity.id) == entity_id)
                ent = (await session.exec(stmt_ent)).first()
                if not ent:
                    raise ResourceNotFoundError(f'Entity {entity_id} not found.')
                results = [MentalModel(entity_id=entity_id, name=ent.canonical_name)]

            # Multiple mental models (multi-vault): wrap under an entity node
            if len(results) > 1:
                stmt_ent = select(Entity).where(col(Entity.id) == entity_id)
                ent = (await session.exec(stmt_ent)).first()
                if ent:
                    entity_data = ent.model_dump(include=_ENTITY_INCLUDE)
                else:
                    entity_data = {'id': str(entity_id), 'canonical_name': results[0].name}

                mm_children: list[LineageResponse] = []
                for mm in results:
                    mm_children.append(
                        LineageResponse(
                            entity_type='mental_model',
                            entity=_truncate_text_fields(mm.model_dump(include=_MM_INCLUDE)),
                            derived_from=[],
                        )
                    )

                return LineageResponse(
                    entity_type='entity',
                    entity=entity_data,
                    derived_from=mm_children,
                )

            # Single mental model: return directly
            obj = results[0]
            entity_data = _truncate_text_fields(obj.model_dump(include=_MM_INCLUDE))

        else:
            raise ValueError(f'Unknown entity type: {entity_type}')

        return LineageResponse(
            entity_type=entity_type,
            entity=entity_data,
            derived_from=children,
        )

    async def _get_lineage_upstream(
        self,
        session: Any,
        entity_type: str,
        entity_id: UUID,
        current_depth: int,
        max_depth: int,
        limit: int,
    ) -> LineageResponse:
        """Recursive helper for upstream lineage (Mental Model -> Document)."""
        from memex_core.memory.sql_models import MentalModel, MemoryUnit, Note, Entity
        from sqlmodel import select, col

        entity_data: dict[str, Any] = {}
        children: list[LineageResponse] = []
        stop_recursion = current_depth >= max_depth

        if entity_type == 'mental_model':
            stmt = (
                select(MentalModel)
                .where(
                    (col(MentalModel.id) == entity_id) | (col(MentalModel.entity_id) == entity_id)
                )
                .options(defer(MentalModel.embedding))  # type: ignore
            )
            results = (await session.exec(stmt)).all()

            if not results:
                stmt_ent = select(Entity).where(col(Entity.id) == entity_id)
                ent = (await session.exec(stmt_ent)).first()
                if not ent:
                    raise ResourceNotFoundError(f'Entity {entity_id} not found.')
                results = [MentalModel(entity_id=entity_id, name=ent.canonical_name)]

            # Multiple mental models (multi-vault): wrap under an entity node
            if len(results) > 1:
                stmt_ent = select(Entity).where(col(Entity.id) == entity_id)
                ent = (await session.exec(stmt_ent)).first()
                if ent:
                    entity_data = ent.model_dump(include=_ENTITY_INCLUDE)
                else:
                    entity_data = {'id': str(entity_id), 'canonical_name': results[0].name}

                mm_children: list[LineageResponse] = []
                for mm in results:
                    mm_child = await self._build_mental_model_upstream(
                        session,
                        mm,
                        current_depth + 1,
                        max_depth,
                        limit,
                    )
                    mm_children.append(mm_child)

                return LineageResponse(
                    entity_type='entity',
                    entity=entity_data,
                    derived_from=mm_children,
                )

            # Single mental model: return directly
            obj = results[0]
            entity_data = _truncate_text_fields(obj.model_dump(include=_MM_INCLUDE))

            if not stop_recursion:
                children = await self._build_mm_observations_upstream(
                    session,
                    obj,
                    current_depth,
                    max_depth,
                    limit,
                )

        elif entity_type == 'observation':
            stmt = (
                select(MentalModel)
                .where(
                    func.jsonb_path_exists(
                        MentalModel.observations,
                        sa_cast(f'$[*] ? (@.id == "{entity_id}")', JSONPath()),
                    )
                )
                .options(defer(MentalModel.embedding))  # type: ignore
            )
            parent_mm = (await session.exec(stmt)).first()

            if not parent_mm:
                raise ResourceNotFoundError(f'Observation {entity_id} not found.')

            target_obs = None
            for obs in parent_mm.observations:
                if str(obs.get('id')) == str(entity_id):
                    target_obs = obs
                    break

            if not target_obs:
                raise ResourceNotFoundError(f'Observation {entity_id} not found in parent model.')

            entity_data = {k: v for k, v in target_obs.items() if k in _OBS_INCLUDE}

            if not stop_recursion:
                evidence = target_obs.get('evidence', [])
                count = 0
                for item in evidence:
                    if count >= limit:
                        break
                    mem_id_str = item.get('memory_id')
                    if mem_id_str:
                        try:
                            mem_id = UUID(mem_id_str)
                            child_node = await self._get_lineage_upstream(
                                session,
                                'memory_unit',
                                mem_id,
                                current_depth + 1,
                                max_depth,
                                limit,
                            )
                            children.append(child_node)
                            count += 1
                        except (ValueError, ResourceNotFoundError):
                            pass

        elif entity_type == 'memory_unit':
            stmt = (
                select(MemoryUnit)
                .where(col(MemoryUnit.id) == entity_id)
                .options(  # type: ignore
                    defer(MemoryUnit.embedding),
                    defer(MemoryUnit.context),
                    defer(MemoryUnit.search_tsvector),
                )
            )
            obj = (await session.exec(stmt)).first()
            if not obj:
                logger.warning('Lineage lookup failed: memory_unit %s not found', entity_id)
                raise ResourceNotFoundError(f'Memory Unit {entity_id} not found.')

            entity_data = _truncate_text_fields(obj.model_dump(include=_UNIT_INCLUDE))

            if not stop_recursion:
                if obj.note_id:
                    try:
                        child_node = await self._get_lineage_upstream(
                            session,
                            'note',
                            obj.note_id,
                            current_depth + 1,
                            max_depth,
                            limit,
                        )
                        children.append(child_node)
                    except ResourceNotFoundError:
                        pass

        elif entity_type == 'note':
            stmt = (
                select(Note)
                .where(col(Note.id) == entity_id)
                .options(defer(Note.original_text), defer(Note.page_index))  # type: ignore
            )
            obj = (await session.exec(stmt)).first()
            if not obj:
                logger.warning('Lineage lookup failed: note %s not found', entity_id)
                raise ResourceNotFoundError(f'Note {entity_id} not found.')

            entity_data = _truncate_text_fields(obj.model_dump(include=_NOTE_INCLUDE))
            # Document is a leaf (upstream-wise)

        else:
            raise ValueError(f'Unknown entity type: {entity_type}')

        return LineageResponse(
            entity_type=entity_type,
            entity=entity_data,
            derived_from=children,
        )

    async def _build_mm_observations_upstream(
        self,
        session: Any,
        mm: Any,
        current_depth: int,
        max_depth: int,
        limit: int,
    ) -> list[LineageResponse]:
        """Build observation subtree for a single mental model (upstream)."""
        obs_nodes: list[LineageResponse] = []
        observations = mm.observations or []
        count = 0
        for obs in observations:
            if count >= limit:
                break
            obs_children: list[LineageResponse] = []

            if current_depth + 1 < max_depth:
                evidence = obs.get('evidence', [])
                ev_count = 0
                for item in evidence:
                    if ev_count >= limit:
                        break
                    mem_id_str = item.get('memory_id')
                    if mem_id_str:
                        try:
                            mem_id = UUID(str(mem_id_str))
                            ev_child = await self._get_lineage_upstream(
                                session,
                                'memory_unit',
                                mem_id,
                                current_depth + 2,
                                max_depth,
                                limit,
                            )
                            obs_children.append(ev_child)
                            ev_count += 1
                        except (ValueError, ResourceNotFoundError):
                            pass

            obs_node = LineageResponse(
                entity_type='observation',
                entity={k: v for k, v in obs.items() if k in _OBS_INCLUDE},
                derived_from=obs_children,
            )
            obs_nodes.append(obs_node)
            count += 1
        return obs_nodes

    async def _build_mental_model_upstream(
        self,
        session: Any,
        mm: Any,
        current_depth: int,
        max_depth: int,
        limit: int,
    ) -> LineageResponse:
        """Build a full mental_model LineageResponse node with its observation subtree."""
        mm_data = _truncate_text_fields(mm.model_dump(include=_MM_INCLUDE))
        obs_children: list[LineageResponse] = []
        if current_depth < max_depth:
            obs_children = await self._build_mm_observations_upstream(
                session,
                mm,
                current_depth,
                max_depth,
                limit,
            )
        return LineageResponse(
            entity_type='mental_model',
            entity=mm_data,
            derived_from=obs_children,
        )
