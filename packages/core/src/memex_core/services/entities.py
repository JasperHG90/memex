"""Entity service — CRUD and query operations for entities."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncGenerator
from uuid import UUID

from sqlmodel import col

from memex_common.exceptions import EntityNotFoundError, ResourceNotFoundError

from memex_core.services.audit import audit_event
from memex_core.services.base import BaseService
from memex_core.services.vaults import VaultService

if TYPE_CHECKING:
    from memex_core.memory.sql_models import Entity

logger = logging.getLogger('memex.core.services.entities')


@dataclass
class EntityWithMetadata:
    """Wraps an ORM Entity with its vault-scoped MentalModel metadata."""

    entity: Any
    metadata: dict[str, Any] = field(default_factory=dict)
    observations: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class AggregatedCooccurrence:
    """A cooccurrence edge summed across vaults.

    Since ``052_entity_cooccurrence_vault_pk`` the cooccurrence grain is
    ``(entity_id_1, entity_id_2, vault_id)`` — one row per vault. Read paths
    that present "related entities" globally (CLI ``entity related``, the MCP /
    Hermes ``get_entity_cooccurrences`` tools) must collapse the per-vault rows
    for a counterpart into a single edge, or the same entity shows up once per
    vault with un-summed counts. This carries the summed count; ``vault_id`` is
    ``None`` because the edge spans vaults.

    ``entity_1`` / ``entity_2`` are populated by
    :meth:`EntityService.get_entity_cooccurrences` (the per-entity endpoint
    serialiser at ``server/entities.py:get_entity_cooccurrences`` reads
    ``.canonical_name`` / ``.entity_type`` off them) but are intentionally left
    ``None`` by :meth:`EntityService.get_bulk_cooccurrences`, whose only caller
    (the bulk endpoint at ``server/entities.py:get_bulk_cooccurrences``) emits
    just ids + counts and never dereferences them. Callers introducing new bulk
    consumers must either resolve the entities themselves or use the per-entity
    method.
    """

    entity_id_1: UUID
    entity_id_2: UUID
    entity_1: 'Entity | None'
    entity_2: 'Entity | None'
    cooccurrence_count: int
    vault_id: UUID | None = None


def _wrap_with_metadata(entity: Any, mental_model: Any | None) -> EntityWithMetadata:
    """Wrap an entity with mental model entity_metadata."""
    metadata = (mental_model.entity_metadata if mental_model else None) or {}
    observations = (mental_model.observations if mental_model else None) or []
    return EntityWithMetadata(entity=entity, metadata=metadata, observations=observations)


def _content_vault_id_subquery() -> Any:
    """A scalar subquery of content (non-system) vault ids.

    Used as the default scope for entity reads so system-vault edges/mentions
    don't surface or inflate counts unless a vault is named explicitly.

    Delegates to :meth:`VaultService.content_vault_ids_subquery` so all
    service files share one predicate. A future kind rename or filter
    change is one diff, not 11.
    """
    return VaultService.content_vault_ids_subquery()


def _membership_clause(vault_ids: list[UUID] | None, scope_ids: list[UUID]) -> Any:
    """WHERE predicate scoping entities to in-scope vaults.

    Explicit ``vault_ids`` → entity must be mentioned in one of them. Default
    scope → keep content + orphan (un-mentioned) entities, drop system-only ones.
    """
    from sqlalchemy import exists

    from memex_core.memory.sql_models import Entity, UnitEntity

    member_in_scope = exists().where(
        (col(UnitEntity.entity_id) == Entity.id) & col(UnitEntity.vault_id).in_(scope_ids)
    )
    if vault_ids:
        return member_in_scope
    has_any_membership = exists().where(col(UnitEntity.entity_id) == Entity.id)
    return member_in_scope | ~has_any_membership


def _model_observation_count(model: Any) -> int:
    meta = model.entity_metadata or {}
    count = meta.get('observation_count') if isinstance(meta, dict) else None
    if isinstance(count, int):
        return count
    return len(model.observations or [])


def _aggregate_mental_models(entity: Any, models: list[Any]) -> EntityWithMetadata:
    """Aggregate an entity's per-vault mental models into one profile.

    Entities are global; their descriptive content lives on vault-scoped mental
    models. Rather than guess a single vault, aggregate over the in-scope set:
    quantitative fields combine (``observation_count`` sums, ``vault_count``
    counts), qualitative fields take a representative (modal ``category``;
    ``description`` + ``observations`` from the most-evidenced model).
    """
    from collections import Counter

    if not models:
        return EntityWithMetadata(entity=entity, metadata={}, observations=[])

    representative = max(models, key=_model_observation_count)
    rep_meta = representative.entity_metadata or {}

    categories = [
        (m.entity_metadata or {}).get('category')
        for m in models
        if (m.entity_metadata or {}).get('category')
    ]
    metadata: dict[str, Any] = {}
    if categories:
        metadata['category'] = Counter(categories).most_common(1)[0][0]
    if rep_meta.get('description'):
        metadata['description'] = rep_meta['description']
    metadata['observation_count'] = sum(_model_observation_count(m) for m in models)
    metadata['vault_count'] = len(models)

    return EntityWithMetadata(
        entity=entity, metadata=metadata, observations=representative.observations or []
    )


def _merge_observations(
    winner_obs: list[dict[str, Any]],
    loser_obs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge two MentalModel.observations lists with deterministic dedup.

    Two observations collide iff their canonical JSON serialization matches.
    The winner's ordering is preserved for surviving rows; loser rows that
    do not already appear are appended in their iteration order.
    """
    import json as _json

    seen: set[str] = set()
    out: list[dict[str, Any]] = []

    def _key(o: dict[str, Any]) -> str:
        try:
            return _json.dumps(o, sort_keys=True, default=str)
        except Exception:
            return repr(o)

    for o in winner_obs or []:
        k = _key(o)
        if k in seen:
            continue
        seen.add(k)
        out.append(o)
    for o in loser_obs or []:
        k = _key(o)
        if k in seen:
            continue
        seen.add(k)
        out.append(o)
    return out


class EntityService(BaseService):
    """Entity CRUD, search, and graph traversal operations."""

    async def _resolve_default_vault_id(self) -> UUID:
        """Resolve the active vault name from config to a UUID via direct DB lookup."""
        from memex_core.memory.sql_models import Vault
        from sqlmodel import select

        vault_name = self.config.server.default_active_vault
        # Try parsing as UUID first
        try:
            return UUID(vault_name)
        except ValueError:
            pass

        async with self.metastore.session() as session:
            stmt = select(Vault).where(Vault.name == vault_name)
            vault = (await session.exec(stmt)).first()
            if vault:
                return vault.id
            raise ResourceNotFoundError(f'Active vault "{vault_name}" not found.')

    async def list_entities_ranked(
        self,
        limit: int = 100,
        vault_ids: list[UUID] | None = None,
        entity_type: str | None = None,
        slim: bool = False,
        include_system_vaults: bool = False,
    ) -> AsyncGenerator[EntityWithMetadata, None]:
        """
        Stream entities ranked by hybrid score.
        Hybrid Score = 0.4 * mention_count + 0.4 * retrieval_count + 0.2 * centrality

        Scoping: an entity appears only if it is mentioned in an in-scope vault,
        and centrality sums cooccurrence only within scope — so system-vault
        entities and edges never surface or inflate ranking by default. Scope is
        the explicit ``vault_ids`` when given, else all content vaults (plus
        system vaults when ``include_system_vaults`` is set).

        When ``slim`` is True the per-vault profile is skipped — callers see an
        empty metadata dict. Otherwise the entity's mental models across the
        in-scope vaults are aggregated into a single profile (no single-vault
        guess); see :func:`_aggregate_mental_models`.

        .. note::
           Performance: this method now issues 2–3 queries (entities,
           cooccurrence centrality, mental-model aggregation) where the old
           single LEFT JOIN gave one. The split is a deliberate correctness
           fix for multi-vault scope (the old code joined on a single
           ``scope_vault_id`` UUID). For single-vault scope on large result
           sets, consider a single CTE that materialises the in-scope
           vault ids once and joins models/cooccurrences in one pass.
           TODO(perf): add a benchmark for ``limit=100, scope=1-vault`` to
           confirm the regression is bounded before any optimisation.
        """
        from collections import defaultdict

        from memex_core.memory.sql_models import (
            Entity,
            EntityCooccurrence,
            MentalModel,
            UnitEntity,
            Vault,
        )
        from sqlalchemy import exists
        from sqlmodel import select, func, desc, col

        async with self.metastore.session() as session:
            if vault_ids:
                scope_ids = [v if isinstance(v, UUID) else UUID(str(v)) for v in vault_ids]
            else:
                scope_q = (
                    VaultService.content_vault_ids_subquery()
                    if not include_system_vaults
                    else select(Vault.id)
                )
                scope_ids = list((await session.exec(scope_q)).all())

            if not scope_ids:
                return

            # Centrality: cooccurrence summed within the in-scope vaults only.
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
                    (
                        (EntityCooccurrence.entity_id_1 == Entity.id)
                        | (EntityCooccurrence.entity_id_2 == Entity.id)
                    )
                    & col(EntityCooccurrence.vault_id).in_(scope_ids),
                )
                .group_by(Entity.id)
            ).subquery()

            rank_score = (
                0.4 * Entity.mention_count
                + 0.4 * Entity.retrieval_count
                + 0.2 * centrality_stmt.c.centrality
            ).label('rank_score')

            stmt = select(Entity, rank_score).join(
                centrality_stmt, centrality_stmt.c.entity_id == Entity.id
            )
            member_in_scope = exists().where(
                (col(UnitEntity.entity_id) == Entity.id) & col(UnitEntity.vault_id).in_(scope_ids)
            )
            if vault_ids:
                # Explicit scope: entity must be mentioned in one of those vaults.
                stmt = stmt.where(member_in_scope)
            else:
                # Default: keep content + orphan entities; drop system-only ones
                # (mentioned, but in no content vault) so system entities stay silent.
                has_any_membership = exists().where(col(UnitEntity.entity_id) == Entity.id)
                stmt = stmt.where(member_in_scope | ~has_any_membership)
            if entity_type:
                stmt = stmt.where(Entity.entity_type == entity_type)
            stmt = stmt.order_by(desc(rank_score)).limit(limit)

            entities = [row[0] for row in (await session.exec(stmt)).all()]

            if slim:
                for entity in entities:
                    yield _wrap_with_metadata(entity, None)
                return

            # Rich: aggregate each entity's in-scope mental models into a profile.
            models_by_entity: dict[UUID, list[Any]] = defaultdict(list)
            page_ids = [e.id for e in entities]
            if page_ids:
                model_stmt = select(MentalModel).where(
                    col(MentalModel.entity_id).in_(page_ids),
                    col(MentalModel.vault_id).in_(scope_ids),
                    col(MentalModel.archived_at).is_(None),
                )
                for model in (await session.exec(model_stmt)).all():
                    models_by_entity[model.entity_id].append(model)

            for entity in entities:
                yield _aggregate_mental_models(entity, models_by_entity.get(entity.id, []))

    async def get_entity_cooccurrences(
        self,
        entity_id: UUID | str,
        vault_ids: list[UUID] | None = None,
        limit: int = 50,
    ) -> list[Any]:
        """Get co-occurrence edges for an entity, summed across vaults.

        The cooccurrence grain is per-vault (``052_entity_cooccurrence_vault_pk``),
        so a counterpart that co-occurs in N vaults has N rows. We GROUP BY the
        counterpart and SUM the counts so each related entity appears exactly
        once with its global strength; ``vault_ids`` narrows which vaults the
        sum spans. Returns :class:`AggregatedCooccurrence` rows (drop-in for the
        prior ORM rows: same ``entity_1``/``entity_2``/id/count attributes).
        """
        from sqlalchemy import case, func
        from sqlmodel import desc, or_, select

        from memex_core.memory.sql_models import Entity, EntityCooccurrence

        eid = UUID(str(entity_id))
        # The counterpart is whichever side of the edge is not the queried entity.
        counterpart = case(
            (EntityCooccurrence.entity_id_1 == eid, EntityCooccurrence.entity_id_2),
            else_=EntityCooccurrence.entity_id_1,
        ).label('counterpart_id')
        total = func.sum(EntityCooccurrence.cooccurrence_count).label('total')

        async with self.metastore.session() as session:
            stmt = select(counterpart, total).where(
                or_(
                    EntityCooccurrence.entity_id_1 == eid,
                    EntityCooccurrence.entity_id_2 == eid,
                )
            )
            if vault_ids:
                stmt = stmt.where(col(EntityCooccurrence.vault_id).in_(vault_ids))
            else:
                stmt = stmt.where(
                    col(EntityCooccurrence.vault_id).in_(_content_vault_id_subquery())
                )
            stmt = stmt.group_by(counterpart).order_by(desc(total)).limit(limit)
            rows = (await session.exec(stmt)).all()
            if not rows:
                return []

            # Resolve names/types for the queried entity + every counterpart in
            # one round-trip so the server serializer can read entity_*.name.
            counterpart_ids = [r.counterpart_id for r in rows]
            entities = {
                e.id: e
                for e in (
                    await session.exec(
                        select(Entity).where(col(Entity.id).in_([eid, *counterpart_ids]))
                    )
                ).all()
            }
            queried = entities.get(eid)
            return [
                AggregatedCooccurrence(
                    entity_id_1=eid,
                    entity_id_2=r.counterpart_id,
                    entity_1=queried,
                    entity_2=entities.get(r.counterpart_id),
                    cooccurrence_count=int(r.total),
                )
                for r in rows
            ]

    async def get_bulk_cooccurrences(
        self, entity_ids: list[UUID], vault_ids: list[UUID] | None = None
    ) -> list[Any]:
        """Get co-occurrences between a set of entities, summed across vaults.

        Like :meth:`get_entity_cooccurrences`, this collapses the per-vault grain
        (``052_entity_cooccurrence_vault_pk``): one edge per ``(entity_id_1,
        entity_id_2)`` pair with the count summed over the vaults in scope, so a
        pair that co-occurs in several vaults is not returned as duplicate rows.
        """
        from sqlalchemy import func
        from sqlmodel import col, select

        from memex_core.memory.sql_models import EntityCooccurrence

        async with self.metastore.session() as session:
            total = func.sum(EntityCooccurrence.cooccurrence_count).label('total')
            stmt = select(
                EntityCooccurrence.entity_id_1, EntityCooccurrence.entity_id_2, total
            ).where(
                (col(EntityCooccurrence.entity_id_1).in_(entity_ids))
                & (col(EntityCooccurrence.entity_id_2).in_(entity_ids))
            )
            if vault_ids:
                stmt = stmt.where(col(EntityCooccurrence.vault_id).in_(vault_ids))
            else:
                stmt = stmt.where(
                    col(EntityCooccurrence.vault_id).in_(_content_vault_id_subquery())
                )
            stmt = stmt.group_by(EntityCooccurrence.entity_id_1, EntityCooccurrence.entity_id_2)
            rows = (await session.exec(stmt)).all()
            return [
                AggregatedCooccurrence(
                    entity_id_1=r.entity_id_1,
                    entity_id_2=r.entity_id_2,
                    entity_1=None,
                    entity_2=None,
                    cooccurrence_count=int(r.total),
                )
                for r in rows
            ]

    async def get_entity_mentions(
        self,
        entity_id: UUID | str,
        limit: int = 20,
        vault_ids: list[UUID] | None = None,
        include_stale: bool = False,
        include_superseded: bool = False,
        include_deprioritized: bool = False,
    ) -> list[dict[str, Any]]:
        """Get memory units and source documents where this entity is mentioned.

        Filter defaults match ``memex_memory_search``: stale, superseded, and
        deprioritized units are excluded unless explicitly requested.
        """
        from memex_core.memory.confidence import extract_confidence_and_count
        from memex_core.memory.sql_models import ContentStatus, MemoryUnit, Note, UnitEntity
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
            else:
                stmt = stmt.where(col(MemoryUnit.vault_id).in_(_content_vault_id_subquery()))
            if not include_stale:
                stmt = stmt.where(col(MemoryUnit.status) == ContentStatus.ACTIVE)
            if not include_deprioritized:
                stmt = stmt.where(col(MemoryUnit.is_deprioritized) == False)  # noqa: E712
            # Over-fetch when the superseded post-filter will drop rows so the
            # caller's ``limit`` is honored against the post-filtered set.
            fetch_limit = limit * 3 if not include_superseded else limit
            stmt = stmt.order_by(desc(MemoryUnit.created_at)).limit(fetch_limit)
            results = list((await session.exec(stmt)).all())
            if not include_superseded:
                threshold = float(self.config.server.memory.retrieval.superseded_threshold)
                results = [
                    (unit, doc)
                    for unit, doc in results
                    if extract_confidence_and_count(unit)[0] >= threshold
                ]
            return [{'unit': unit, 'document': doc} for unit, doc in results[:limit]]

    async def get_entity(
        self, entity_id: UUID | str, vault_id: UUID | None = None
    ) -> EntityWithMetadata | None:
        """Get an entity by ID, with MentalModel metadata attached."""
        from memex_core.memory.sql_models import Entity, MentalModel
        from sqlmodel import select

        scope_vault_id = vault_id or await self._resolve_default_vault_id()
        eid = UUID(str(entity_id))
        async with self.metastore.session() as session:
            stmt = (
                select(Entity, MentalModel)
                .outerjoin(
                    MentalModel,
                    (MentalModel.entity_id == Entity.id) & (MentalModel.vault_id == scope_vault_id),
                )
                .where(Entity.id == eid)
            )
            result = (await session.exec(stmt)).first()
            if not result:
                return None
            return _wrap_with_metadata(result[0], result[1])

    async def get_entities(
        self, entity_ids: list[UUID], vault_id: UUID | None = None
    ) -> list[EntityWithMetadata]:
        """Get multiple entities by ID, with MentalModel metadata attached."""
        from memex_core.memory.sql_models import Entity, MentalModel
        from sqlmodel import select

        scope_vault_id = vault_id or await self._resolve_default_vault_id()
        async with self.metastore.session() as session:
            stmt = (
                select(Entity, MentalModel)
                .outerjoin(
                    MentalModel,
                    (MentalModel.entity_id == Entity.id) & (MentalModel.vault_id == scope_vault_id),
                )
                .where(col(Entity.id).in_(entity_ids))
            )
            results = (await session.exec(stmt)).all()
            return [_wrap_with_metadata(row[0], row[1]) for row in results]

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

        audit_event(self._audit_service, 'entity.deleted', 'entity', str(entity_id))
        return True

    async def collapse_cluster(
        self,
        *,
        winner_id: UUID,
        loser_ids: list[UUID],
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Merge a cluster of duplicate entities into one canonical winner.

        Resolves the six referential-integrity hazards in a single
        transaction:

        1. ``MemoryLink.entity_id`` repoint loser->winner (before the entity
           hard-delete; otherwise FK CASCADE would drop the rows).
        2. ``EntityCooccurrence`` re-ordered (smaller id as ``entity_id_1``)
           and summed with ON CONFLICT DO UPDATE.
        3. ``EntityAlias`` absorbed with ON CONFLICT (canonical_id, name)
           DO NOTHING; the loser's canonical_name is also recorded as an
           alias of the winner.
        4. ``UnitEntity`` re-inserted on the winner with summed counters,
           then loser rows dropped.
        5. ``MentalModel`` merged per-vault: loser's observations appended
           (dedup-aware), ``version = max(winner, loser) + 1``.
        6. ``Entity`` hard-delete losers (LAST).
        7. Append a single ``entity.collapse_cluster`` AuditLog row.

        Returns a summary dict suitable for logging / API response.
        """
        from datetime import datetime, timezone
        from uuid import UUID as PyUUID

        from sqlalchemy import case, func, null, or_, text
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlmodel import col, select, update

        from memex_core.memory.sql_models import (
            Entity,
            EntityAlias,
            EntityCooccurrence,
            MemoryLink,
            MentalModel,
            UnitEntity,
        )
        from memex_core.memory.utils import get_phonetic_code
        from memex_core import metrics
        import time as _time

        if not loser_ids:
            raise ValueError('collapse_cluster requires at least one loser_id')
        if winner_id in loser_ids:
            raise ValueError('winner_id must not appear in loser_ids')

        try:
            from opentelemetry import trace as _otel_trace

            _tracer = _otel_trace.get_tracer('memex.entity_maintenance')
        except Exception:  # pragma: no cover
            _tracer = None

        start = _time.perf_counter()
        span_cm = (
            _tracer.start_as_current_span(
                'memex.entity_maintenance.collapse_cluster',
                attributes={
                    'cluster_size': len(loser_ids) + 1,
                    'winner_id': str(winner_id),
                },
            )
            if _tracer
            else None
        )
        if span_cm is not None:
            span_cm.__enter__()

        outcome = 'failed'
        try:
            async with self.metastore.session() as session:
                winner = await session.get(Entity, winner_id)
                if winner is None:
                    raise EntityNotFoundError(f'Entity {winner_id} not found.')

                loser_uuids = [PyUUID(str(lid)) for lid in loser_ids]
                stmt = select(Entity).where(col(Entity.id).in_(loser_uuids))
                losers = list((await session.exec(stmt)).all())
                missing = set(str(u) for u in loser_uuids) - {str(e.id) for e in losers}
                if missing:
                    raise EntityNotFoundError(f'Entities not found: {sorted(missing)}')

                vaults_affected: set[str] = set()

                # 1) MemoryLink: repoint entity_id loser -> winner BEFORE hard delete
                ml_stmt = (
                    update(MemoryLink)
                    .where(col(MemoryLink.entity_id).in_(loser_uuids))
                    .values(entity_id=winner_id)
                )
                ml_result = await session.exec(ml_stmt)
                links_repointed = int(getattr(ml_result, 'rowcount', 0) or 0)

                # 2) EntityCooccurrence: rewrite loser-side rows so they reference
                # the winner instead, with canonical (smaller, larger) ordering and
                # summed counts. Loser rows are deleted afterwards.
                co_stmt = select(EntityCooccurrence).where(
                    col(EntityCooccurrence.entity_id_1).in_(loser_uuids)
                    | col(EntityCooccurrence.entity_id_2).in_(loser_uuids)
                )
                co_rows = list((await session.exec(co_stmt)).all())
                cooccurrences_merged = 0
                for row in co_rows:
                    other = row.entity_id_2 if row.entity_id_1 in loser_uuids else row.entity_id_1
                    if other in loser_uuids or other == winner_id:
                        # Intentional drop: post-collapse, winner and loser are
                        # the same entity, so a W<->L (or L<->L) cooccurrence
                        # becomes a self-loop, which the table CHECK constraint
                        # forbids. The loser-row DELETE below consumes the row.
                        continue
                    e1, e2 = sorted([winner_id, other], key=str)
                    upsert = pg_insert(EntityCooccurrence).values(
                        entity_id_1=e1,
                        entity_id_2=e2,
                        vault_id=row.vault_id,
                        cooccurrence_count=row.cooccurrence_count,
                        last_cooccurred=row.last_cooccurred,
                        valid_from=row.valid_from,
                        valid_to=row.valid_to,
                    )
                    upsert = upsert.on_conflict_do_update(
                        index_elements=['entity_id_1', 'entity_id_2', 'vault_id'],
                        set_={
                            'cooccurrence_count': (
                                EntityCooccurrence.cooccurrence_count + row.cooccurrence_count
                            ),
                            'last_cooccurred': func.greatest(
                                EntityCooccurrence.last_cooccurred,
                                upsert.excluded.last_cooccurred,
                            ),
                            'valid_from': case(
                                (
                                    or_(
                                        EntityCooccurrence.valid_from.is_(None),
                                        upsert.excluded.valid_from.is_(None),
                                    ),
                                    null(),
                                ),
                                else_=func.least(
                                    EntityCooccurrence.valid_from,
                                    upsert.excluded.valid_from,
                                ),
                            ),
                            'valid_to': case(
                                (
                                    or_(
                                        EntityCooccurrence.valid_to.is_(None),
                                        upsert.excluded.valid_to.is_(None),
                                    ),
                                    null(),
                                ),
                                else_=func.greatest(
                                    EntityCooccurrence.valid_to,
                                    upsert.excluded.valid_to,
                                ),
                            ),
                        },
                    )
                    await session.exec(upsert)
                    cooccurrences_merged += 1

                # Drop the loser-side rows (any direction)
                await session.exec(
                    text(
                        'DELETE FROM entity_cooccurrences '
                        'WHERE entity_id_1 = ANY(CAST(:ids AS uuid[])) '
                        'OR entity_id_2 = ANY(CAST(:ids AS uuid[]))'
                    ).bindparams(ids=[str(u) for u in loser_uuids])
                )

                # 3) EntityAlias: absorb loser aliases onto winner, plus a fresh
                # alias for each loser's canonical_name so future lookups find
                # the winner via the loser's spelling.
                alias_stmt = select(EntityAlias).where(
                    col(EntityAlias.canonical_id).in_(loser_uuids)
                )
                alias_rows = list((await session.exec(alias_stmt)).all())
                alias_values = [
                    {
                        'canonical_id': winner_id,
                        'name': a.name,
                        'phonetic_code': a.phonetic_code,
                    }
                    for a in alias_rows
                ]
                for loser in losers:
                    alias_values.append(
                        {
                            'canonical_id': winner_id,
                            'name': loser.canonical_name,
                            'phonetic_code': loser.phonetic_code
                            or get_phonetic_code(loser.canonical_name),
                        }
                    )
                aliases_absorbed = 0
                if alias_values:
                    alias_upsert = (
                        pg_insert(EntityAlias)
                        .values(alias_values)
                        .on_conflict_do_nothing(index_elements=['canonical_id', 'name'])
                    )
                    await session.exec(alias_upsert)
                    aliases_absorbed = len(alias_values)

                # 4) UnitEntity: roll loser's per-unit rows onto the winner, with
                # summed counters; then delete loser rows. Per-(unit_id, winner_id)
                # collisions resolved via the UPDATE-after-aggregate idiom.
                ue_stmt = select(UnitEntity).where(col(UnitEntity.entity_id).in_(loser_uuids))
                ue_rows = list((await session.exec(ue_stmt)).all())
                unit_entities_merged = 0
                if ue_rows:
                    upsert_sql = text(
                        """
                        INSERT INTO unit_entities (
                            unit_id, entity_id, vault_id,
                            success_co_count, failure_co_count
                        )
                        VALUES (
                            CAST(:unit_id AS uuid),
                            CAST(:winner_id AS uuid),
                            CAST(:vault_id AS uuid),
                            :success_co_count,
                            :failure_co_count
                        )
                        ON CONFLICT (unit_id, entity_id) DO UPDATE SET
                            success_co_count = unit_entities.success_co_count
                                + EXCLUDED.success_co_count,
                            failure_co_count = unit_entities.failure_co_count
                                + EXCLUDED.failure_co_count
                        """
                    )
                    for row in ue_rows:
                        await session.exec(
                            upsert_sql.bindparams(
                                unit_id=str(row.unit_id),
                                winner_id=str(winner_id),
                                vault_id=str(row.vault_id),
                                success_co_count=row.success_co_count,
                                failure_co_count=row.failure_co_count,
                            )
                        )
                        unit_entities_merged += 1
                    await session.exec(
                        text(
                            'DELETE FROM unit_entities WHERE entity_id = ANY(CAST(:ids AS uuid[]))'
                        ).bindparams(ids=[str(u) for u in loser_uuids])
                    )

                # 5) MentalModel: per-vault merge. For each vault that the
                # losers have a model in, fold their observations into the
                # winner's model and bump the version.
                mm_stmt = select(MentalModel).where(
                    col(MentalModel.entity_id).in_(loser_uuids + [winner_id])
                )
                mm_rows = list((await session.exec(mm_stmt)).all())
                # Bucket by vault
                by_vault: dict[Any, dict[str, Any]] = {}
                for mm in mm_rows:
                    bucket = by_vault.setdefault(mm.vault_id, {'winner': None, 'losers': []})
                    if mm.entity_id == winner_id:
                        bucket['winner'] = mm
                    else:
                        bucket['losers'].append(mm)

                mental_models_merged = 0
                for vault_id, bucket in by_vault.items():
                    vaults_affected.add(str(vault_id))
                    winner_mm = bucket['winner']
                    loser_mms = bucket['losers']
                    if not loser_mms:
                        continue

                    if winner_mm is None:
                        # No winner MM yet — promote the highest-versioned loser
                        # to be the winner MM in place (update entity_id), then
                        # absorb the rest.
                        loser_mms_sorted = sorted(loser_mms, key=lambda m: m.version, reverse=True)
                        promoted = loser_mms_sorted[0]
                        remaining = loser_mms_sorted[1:]
                        merged_obs = _merge_observations(
                            promoted.observations or [],
                            [o for m in remaining for o in (m.observations or [])],
                        )
                        promoted.entity_id = winner_id
                        promoted.observations = merged_obs
                        promoted.version = (
                            max([promoted.version] + [m.version for m in remaining]) + 1
                        )
                        promoted.last_refreshed = datetime.now(timezone.utc)
                        session.add(promoted)
                        for m in remaining:
                            await session.delete(m)
                        mental_models_merged += 1 + len(remaining)
                    else:
                        merged_obs = _merge_observations(
                            winner_mm.observations or [],
                            [o for m in loser_mms for o in (m.observations or [])],
                        )
                        winner_mm.observations = merged_obs
                        winner_mm.version = (
                            max([winner_mm.version] + [m.version for m in loser_mms]) + 1
                        )
                        winner_mm.last_refreshed = datetime.now(timezone.utc)
                        session.add(winner_mm)
                        for m in loser_mms:
                            await session.delete(m)
                        mental_models_merged += 1 + len(loser_mms)

                # Materialize ORM-pending mental-model deletes before the raw
                # SQL DELETE on entities — without this, the entity hard-delete
                # could fire while stale MentalModel rows still hold FK references.
                await session.flush()

                # 6) Entity: hard-delete losers — LAST step before commit.
                # Use raw SQL to bypass ORM cascade replay on already-cleaned tables.
                await session.exec(
                    text('DELETE FROM entities WHERE id = ANY(CAST(:ids AS uuid[]))').bindparams(
                        ids=[str(u) for u in loser_uuids]
                    )
                )

                # Refresh the winner's bookkeeping
                merged_mention = winner.mention_count + sum(
                    int(getattr(loser, 'mention_count', 0) or 0) for loser in losers
                )
                await session.exec(
                    update(Entity)
                    .where(col(Entity.id) == winner_id)
                    .values(
                        mention_count=merged_mention,
                        last_seen=datetime.now(timezone.utc),
                    )
                )

                summary = {
                    'winner_id': str(winner_id),
                    'loser_ids': [str(u) for u in loser_uuids],
                    'links_repointed': links_repointed,
                    'cooccurrences_merged': cooccurrences_merged,
                    'aliases_absorbed': aliases_absorbed,
                    'unit_entities_merged': unit_entities_merged,
                    'mental_models_merged': mental_models_merged,
                    'vaults_affected': sorted(vaults_affected),
                }

                # 7) AuditLog: append (NOTE column is `details`, not `metadata`).
                from memex_core.memory.sql_models import AuditLog as _AuditLog

                session.add(
                    _AuditLog(
                        actor=actor,
                        action='entity.collapse_cluster',
                        resource_type='entity',
                        resource_id=str(winner_id),
                        details=summary,
                    )
                )

                await session.commit()

            outcome = 'success'
            metrics.ENTITY_COLLAPSE_APPLY_TOTAL.labels(outcome='success').inc()
            return summary
        except Exception:
            metrics.ENTITY_COLLAPSE_APPLY_TOTAL.labels(outcome='failed').inc()
            raise
        finally:
            metrics.ENTITY_COLLAPSE_APPLY_DURATION_SECONDS.observe(_time.perf_counter() - start)
            if span_cm is not None:
                span_cm.__exit__(None, None, None)
            logger.info(
                'entity.collapse_cluster outcome=%s winner_id=%s cluster_size=%d',
                outcome,
                winner_id,
                len(loser_ids) + 1,
            )

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

        audit_event(
            self._audit_service,
            'mental_model.deleted',
            'entity',
            str(entity_id),
            vault_id=str(vault_id),
        )
        return True

    async def collapse_into_new_entity(
        self,
        *,
        member_ids: list[UUID],
        new_canonical_name: str,
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Merge a duplicate cluster into a freshly created canonical entity.

        Creates a bare survivor row (``mention_count=0``, no links), then
        delegates to :meth:`collapse_cluster` so every member's counters,
        links, aliases, and per-vault mental models fold onto the new entity
        through the same audited six-step repoint. Forward-only — the member
        entities are hard-deleted.

        The create and the collapse run in separate transactions
        (``collapse_cluster`` owns its session); on collapse failure the bare
        survivor is best-effort deleted so no orphan entity is left behind.
        """
        from memex_core.memory.sql_models import Entity
        from memex_core.memory.utils import get_phonetic_code
        from sqlmodel import col, select

        name = new_canonical_name.strip()
        if not name:
            raise ValueError('new_canonical_name must be non-empty')
        member_uuids = list(dict.fromkeys(UUID(str(m)) for m in member_ids))
        if not member_uuids:
            raise ValueError('collapse_into_new_entity requires at least one member_id')

        async with self.metastore.session() as session:
            existing = (
                await session.exec(select(Entity).where(col(Entity.canonical_name) == name))
            ).first()
            if existing is not None and existing.mention_count > 0:
                raise ValueError(
                    f'an entity named {name!r} already exists ({existing.id}); '
                    'merge into that winner instead of creating a new entity.'
                )
            if existing is not None:
                # A zero-mention row with this name is an orphan bare survivor
                # from a crashed prior attempt — reuse it so the retry heals
                # the leak instead of tripping over the unique name index.
                survivor_id = existing.id
            else:
                survivor = Entity(
                    canonical_name=name,
                    phonetic_code=get_phonetic_code(name),
                    mention_count=0,
                )
                session.add(survivor)
                await session.commit()
                await session.refresh(survivor)
                survivor_id = survivor.id

        try:
            summary = await self.collapse_cluster(
                winner_id=survivor_id, loser_ids=member_uuids, actor=actor
            )
        except Exception:
            try:
                await self.delete_entity(survivor_id)
            except Exception:  # noqa: BLE001 - cleanup is best-effort
                logger.warning(
                    'collapse_into_new_entity: failed to clean up bare survivor %s',
                    survivor_id,
                    exc_info=True,
                )
            raise
        summary['created_entity_id'] = str(survivor_id)
        summary['created_canonical_name'] = name
        return summary

    async def _scope_vault_ids(
        self, session: Any, vault_ids: list[UUID] | None, include_system_vaults: bool = False
    ) -> list[UUID]:
        """Resolve the in-scope vault id set for an entity query.

        Explicit ``vault_ids`` are honoured as given (may name a system vault);
        otherwise the scope is all content vaults (plus system when opted in).
        """
        from memex_core.memory.sql_models import Vault
        from sqlmodel import select

        if vault_ids:
            return [v if isinstance(v, UUID) else UUID(str(v)) for v in vault_ids]
        scope_q = (
            VaultService.content_vault_ids_subquery()
            if not include_system_vaults
            else select(Vault.id)
        )
        return list((await session.exec(scope_q)).all())

    async def _aggregate_models_for(
        self, session: Any, entities: list[Any], scope_ids: list[UUID]
    ) -> list[EntityWithMetadata]:
        """Attach each entity's aggregated in-scope mental-model profile."""
        from collections import defaultdict

        from memex_core.memory.sql_models import MentalModel
        from sqlmodel import select

        models_by_entity: dict[UUID, list[Any]] = defaultdict(list)
        page_ids = [e.id for e in entities]
        if page_ids:
            model_stmt = select(MentalModel).where(
                col(MentalModel.entity_id).in_(page_ids),
                col(MentalModel.vault_id).in_(scope_ids),
                col(MentalModel.archived_at).is_(None),
            )
            for model in (await session.exec(model_stmt)).all():
                models_by_entity[model.entity_id].append(model)
        return [_aggregate_mental_models(e, models_by_entity.get(e.id, [])) for e in entities]

    async def get_top_entities(
        self,
        limit: int = 5,
        vault_ids: list[UUID] | None = None,
        entity_type: str | None = None,
    ) -> list[EntityWithMetadata]:
        """Get top entities by mention count, with aggregated MentalModel profile.

        Scopes to the explicit ``vault_ids`` when given, else all content vaults
        (system vaults are silent by default; name one to include it).
        """
        from memex_core.memory.sql_models import Entity
        from sqlmodel import select, desc

        async with self.metastore.session() as session:
            scope_ids = await self._scope_vault_ids(session, vault_ids)
            if not scope_ids:
                return []
            stmt = select(Entity).where(_membership_clause(vault_ids, scope_ids))
            if entity_type:
                stmt = stmt.where(Entity.entity_type == entity_type)
            stmt = stmt.order_by(desc(Entity.mention_count)).limit(limit)
            entities = list((await session.exec(stmt)).all())
            return await self._aggregate_models_for(session, entities, scope_ids)

    async def search_entities(
        self,
        query: str,
        limit: int = 10,
        vault_ids: list[UUID] | None = None,
        entity_type: str | None = None,
    ) -> list[EntityWithMetadata]:
        """Search for entities by canonical name using trigram similarity or ILIKE.

        Scopes to the explicit ``vault_ids`` when given, else all content vaults.
        """
        from memex_core.memory.sql_models import Entity
        from sqlmodel import select, col

        async with self.metastore.session() as session:
            scope_ids = await self._scope_vault_ids(session, vault_ids)
            if not scope_ids:
                return []
            stmt = (
                select(Entity)
                .where(_membership_clause(vault_ids, scope_ids))
                .where(col(Entity.canonical_name).ilike(f'%{query}%'))
            )
            if entity_type:
                stmt = stmt.where(Entity.entity_type == entity_type)
            stmt = stmt.order_by(col(Entity.mention_count).desc()).limit(limit)
            entities = list((await session.exec(stmt)).all())
            return await self._aggregate_models_for(session, entities, scope_ids)
