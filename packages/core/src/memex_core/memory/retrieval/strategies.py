from __future__ import annotations

import logging
from typing import Protocol, Any, runtime_checkable
from uuid import UUID

from sqlalchemy import func, desc, literal, or_, text, cast, String, union_all, distinct
from sqlalchemy.sql import Select, CompoundSelect
from sqlalchemy.sql.expression import CTE
from sqlmodel import select, col

from memex_core.memory.sql_models import (
    MemoryUnit,
    MemoryLink,
    Entity,
    EntityAlias,
    UnitEntity,
    EntityCooccurrence,
    Chunk,
    Note,
    ContentStatus,
)
from memex_core.memory.sql_models import MentalModel

logger = logging.getLogger('memex.core.memory.retrieval.strategies')


def apply_date_filters(statement: Select, date_column: Any, **kwargs: Any) -> Select:
    """Applies start_date and end_date filters to a SQLAlchemy statement."""
    start_date = kwargs.get('start_date')
    end_date = kwargs.get('end_date')
    if start_date:
        statement = statement.where(col(date_column) >= start_date)
    if end_date:
        statement = statement.where(col(date_column) <= end_date)
    return statement


def apply_vault_filters(statement: Select, vault_id_col: Any, **kwargs: Any) -> Select:
    """
    Applies vault scoping rules suited for Project-based isolation:

    1. **Global Search (Superuser/Cross-Project):**
       If `vault_ids` is None or empty, no filter is applied.
       The search covers ALL vaults (Project A + Project B + ... + Global).

    2. **Scoped Search (Project Specific / Multi-Project):**
       If `vault_ids` is a list of UUIDs, the search is STRICTLY limited to those vaults.
       Example: `[ID_A, GLOBAL_ID]` searches Project A and Global, but not Project B.
    """
    vault_ids = kwargs.get('vault_ids')

    # If no vaults specified, we search everything (God Mode).
    if not vault_ids:
        return statement

    # Otherwise, STRICT scoping to the specified vaults.
    return statement.where(col(vault_id_col).in_(vault_ids))


def apply_generic_filters(statement: Select, **kwargs: Any) -> Select:
    """
    Applies generic filters for MemoryUnit columns (e.g., fact_type).
    """
    fact_type = kwargs.get('fact_type')
    if fact_type:
        statement = statement.where(col(MemoryUnit.fact_type) == fact_type)

    # Implicitly filter out stale units unless specifically requested (future proofing)
    # We default to ACTIVE to prevent "schizophrenic" retrieval, but allow overrides.
    include_stale = kwargs.get('include_stale', False)
    if not include_stale:
        statement = statement.where(col(MemoryUnit.status) == ContentStatus.ACTIVE)

    return statement


@runtime_checkable
class RetrievalStrategy(Protocol):
    """Protocol for memory retrieval strategies."""

    def get_statement(
        self, query: str, query_embedding: list[float] | None, limit: int = 10, **kwargs: Any
    ) -> Select | CompoundSelect: ...


class SemanticStrategy:
    """
    Retrieves memories based on vector similarity (Dense Retrieval).
    Score: Cosine Distance (Lower is better).
    """

    def get_statement(
        self, query: str, query_embedding: list[float] | None, limit: int = 60, **kwargs: Any
    ) -> Select:
        statement = select(MemoryUnit.id).select_from(MemoryUnit)

        # Apply Filters
        statement = apply_date_filters(statement, MemoryUnit.event_date, **kwargs)
        statement = apply_vault_filters(statement, MemoryUnit.vault_id, **kwargs)
        statement = apply_generic_filters(statement, **kwargs)

        if query_embedding is None:
            # If no embedding, this strategy is effectively disabled
            return statement.add_columns(literal(1.0).label('score')).where(literal(False))

        from typing import Any, cast

        distance_col = cast(Any, col(MemoryUnit.embedding)).cosine_distance(query_embedding)

        return (
            statement.add_columns(distance_col.label('score')).order_by(distance_col).limit(limit)
        )


class KeywordStrategy:
    """
    Retrieves memories based on lexical overlap.
    Score: ts_rank_cd (Higher is better).
    """

    def get_statement(
        self, query: str, query_embedding: list[float] | None, limit: int = 60, **kwargs: Any
    ) -> Select:
        # Hindsight Fix: plainto_tsquery uses strict AND which is too brittle for natural language.
        # We switch to a "Bag of Words" approach using OR (|) to simulate BM25's inclusive matching.
        # We take the output of plainto_tsquery (which is stemmed and cleaned) and replace '&' with '|'.
        ts_query_base = func.plainto_tsquery('english', query)
        permissive_query_str = func.regexp_replace(cast(ts_query_base, String), '&', '|', 'g')
        ts_query = func.to_tsquery('english', permissive_query_str)

        ts_vector = func.to_tsvector('english', col(MemoryUnit.text))
        rank = func.ts_rank_cd(ts_vector, ts_query)

        statement = select(MemoryUnit.id).select_from(MemoryUnit)

        # Apply Filters
        statement = apply_date_filters(statement, MemoryUnit.event_date, **kwargs)
        statement = apply_vault_filters(statement, MemoryUnit.vault_id, **kwargs)
        statement = apply_generic_filters(statement, **kwargs)

        return (
            statement.add_columns(rank.label('score'))
            .where(ts_vector.op('@@')(ts_query))
            .order_by(desc(rank))
            .limit(limit)
        )


from memex_core.memory.models.ner import FastNERModel
from memex_core.memory.utils import get_phonetic_code


# ---------------------------------------------------------------------------
# Semantic seed CTE builder (T3 — semantic seeding)
# ---------------------------------------------------------------------------


def build_semantic_seed_cte(
    query_embedding: list[float],
    vault_id: UUID,
    top_k: int = 5,
    weight: float = 0.7,
) -> CTE:
    """Build a CTE of seed entities discovered via semantic similarity.

    Finds the top-K memory units closest to ``query_embedding``, then extracts
    their linked entities as seeds with the given ``weight``.

    Args:
        query_embedding: Dense vector for the user query.
        vault_id: Scope the lookup to a single vault.
        top_k: Number of nearest memory units to consider.
        weight: Score assigned to each semantic seed (should be < 1.0 so NER
            seeds dominate on overlap).

    Returns:
        A SQLAlchemy CTE with columns ``(id, weight)``.
    """
    from typing import Any as _Any
    from typing import cast as _cast

    distance_col = _cast(_Any, col(MemoryUnit.embedding)).cosine_distance(query_embedding)

    top_units = (
        select(col(MemoryUnit.id).label('unit_id'))
        .where(col(MemoryUnit.vault_id) == vault_id)
        .where(col(MemoryUnit.status) == ContentStatus.ACTIVE)
        .order_by(distance_col)
        .limit(top_k)
    ).subquery('semantic_top_units')

    return (
        select(
            col(UnitEntity.entity_id).label('id'),
            literal(weight).label('weight'),
        )
        .join(top_units, col(UnitEntity.unit_id) == top_units.c.unit_id)
        .group_by(col(UnitEntity.entity_id))
    ).cte('semantic_seeds')


# ---------------------------------------------------------------------------
# Shared helper: build seed entity CTE from a query string
# ---------------------------------------------------------------------------


def _build_ner_seeds(
    query: str,
    ner_model: FastNERModel | None,
    similarity_threshold: float,
    include_ilike: bool = False,
) -> Select | CompoundSelect:
    """Build a UNION query of seed entity IDs from NER or fallback similarity.

    When a ``ner_model`` is provided, entities are extracted from the query and
    matched against ``Entity.canonical_name`` and ``EntityAlias.name`` using exact,
    phonetic, and optionally ILIKE/trigram conditions. Without NER, falls back
    to ILIKE + pg_trgm similarity on the raw query string.

    Returns:
        A ``Select | CompoundSelect`` producing a single ``id`` column.
    """
    extracted_names: list[str] = []
    extracted_phonetics: list[str] = []

    if ner_model:
        try:
            extracted = ner_model.predict(query)
            extracted_names = [e['word'] for e in extracted]
            for name in extracted_names:
                p_code = get_phonetic_code(name)
                if p_code:
                    extracted_phonetics.append(p_code)
        except (ValueError, RuntimeError, OSError) as e:
            logger.warning(f'NER extraction failed: {e}. Falling back to naive search.')

    if extracted_names:
        logger.info(f'NER found entities: {extracted_names}')

        conds_canonical: list[Any] = [col(Entity.canonical_name).in_(extracted_names)]
        if extracted_phonetics:
            conds_canonical.append(col(Entity.phonetic_code).in_(extracted_phonetics))
        if include_ilike:
            for name in extracted_names:
                conds_canonical.append(col(Entity.canonical_name).ilike(f'%{name}%'))
                conds_canonical.append(
                    func.similarity(col(Entity.canonical_name), name) > similarity_threshold
                )

        seed_from_canonical = select(col(Entity.id).label('id')).where(or_(*conds_canonical))

        conds_alias: list[Any] = [col(EntityAlias.name).in_(extracted_names)]
        if extracted_phonetics:
            conds_alias.append(col(EntityAlias.phonetic_code).in_(extracted_phonetics))
        if include_ilike:
            for name in extracted_names:
                conds_alias.append(col(EntityAlias.name).ilike(f'%{name}%'))
                conds_alias.append(
                    func.similarity(col(EntityAlias.name), name) > similarity_threshold
                )

        seed_from_alias = select(col(EntityAlias.canonical_id).label('id')).where(or_(*conds_alias))
    else:
        logger.info('No entities found by NER. Using fallback similarity search.')
        seed_from_canonical = select(col(Entity.id).label('id')).where(
            or_(
                col(Entity.canonical_name).ilike(f'%{query}%'),
                func.similarity(col(Entity.canonical_name), query) > similarity_threshold,
            )
        )
        seed_from_alias = select(col(EntityAlias.canonical_id).label('id')).where(
            or_(
                col(EntityAlias.name).ilike(f'%{query}%'),
                func.similarity(col(EntityAlias.name), query) > similarity_threshold,
            )
        )

    return seed_from_canonical.union(seed_from_alias)


def build_seed_entity_cte(
    query: str,
    ner_model: FastNERModel | None = None,
    similarity_threshold: float = 0.3,
    include_ilike: bool = False,
    cte_name: str = 'seed_entities',
    query_embedding: list[float] | None = None,
    vault_id: UUID | None = None,
    semantic_seed_top_k: int = 5,
    semantic_seed_weight: float = 0.7,
    enable_semantic_seeding: bool = True,
) -> CTE:
    """Build a CTE of (id, weight) seed entities from NER + optional semantic seeds.

    When ``query_embedding`` is provided and ``enable_semantic_seeding`` is True,
    NER seeds (weight=1.0) are combined with semantic seeds via UNION ALL then
    grouped by entity_id taking MAX(weight) so NER wins on overlap.

    Args:
        query: The user search query.
        ner_model: Optional NER model for entity extraction.
        similarity_threshold: Minimum pg_trgm similarity score.
        include_ilike: When True **and** NER entities are found, add per-entity
            ``ILIKE`` and ``similarity()`` fuzzy conditions to the NER path.
        cte_name: Name for the resulting CTE (must be unique per query tree).
        query_embedding: Optional query embedding for semantic seeding.
        vault_id: Vault ID for semantic seeding scope.
        semantic_seed_top_k: Number of top-K memory units for semantic seeds.
        semantic_seed_weight: Weight for semantic seed entities.
        enable_semantic_seeding: Whether to enable semantic seeding.

    Returns:
        A SQLAlchemy CTE with columns ``(id, weight)``.
    """
    ner_seeds_query = _build_ner_seeds(
        query, ner_model, similarity_threshold, include_ilike=include_ilike
    )
    ner_subq = ner_seeds_query.subquery(f'{cte_name}_ner_t')

    ner_weighted = select(
        ner_subq.c.id.label('id'),
        literal(1.0).label('weight'),
    ).select_from(ner_subq)

    use_semantic = enable_semantic_seeding and query_embedding is not None and vault_id is not None

    if use_semantic:
        assert query_embedding is not None
        assert vault_id is not None
        semantic_cte = build_semantic_seed_cte(
            query_embedding=query_embedding,
            vault_id=vault_id,
            top_k=semantic_seed_top_k,
            weight=semantic_seed_weight,
        )

        semantic_select = select(
            semantic_cte.c.id.label('id'),
            semantic_cte.c.weight.label('weight'),
        )

        combined = ner_weighted.union_all(semantic_select).subquery(f'{cte_name}_combined')

        return (
            select(
                combined.c.id.label('id'),
                func.max(combined.c.weight).label('weight'),
            ).group_by(combined.c.id)
        ).cte(cte_name)
    else:
        return (
            select(
                ner_subq.c.id.label('id'),
                literal(1.0).label('weight'),
            ).select_from(ner_subq)
        ).cte(cte_name)


# ---------------------------------------------------------------------------
# Memory-level graph strategy (entity co-occurrence)
# ---------------------------------------------------------------------------


class EntityCooccurrenceGraphStrategy:
    """Graph retrieval via entity co-occurrence (1st + 2nd order BFS).

    1st order: NER seed entities -> ``UnitEntity`` -> ``MemoryUnit`` (scored
    by seed weight + temporal decay).
    2nd order: seed entities -> ``EntityCooccurrence`` neighbours ->
    ``MemoryUnit`` (scored by co-occurrence link strength).

    Supports semantic seeding (T3) to augment NER seeds with entities
    extracted from the top-K semantically similar memory units.

    Config dependencies:
        ``RetrievalConfig.similarity_threshold``,
        ``RetrievalConfig.temporal_decay_days``,
        ``RetrievalConfig.temporal_decay_base``,
        ``RetrievalConfig.graph_semantic_seeding``,
        ``RetrievalConfig.graph_semantic_seed_top_k``,
        ``RetrievalConfig.graph_semantic_seed_weight``.
    """

    def __init__(
        self,
        ner_model: FastNERModel | None = None,
        similarity_threshold: float = 0.3,
        temporal_decay_days: float = 30.0,
        temporal_decay_base: float = 2.0,
        enable_semantic_seeding: bool = True,
        semantic_seed_top_k: int = 5,
        semantic_seed_weight: float = 0.7,
    ):
        self.ner_model = ner_model
        self.similarity_threshold = similarity_threshold
        self.temporal_decay_days = temporal_decay_days
        self.temporal_decay_base = temporal_decay_base
        self.enable_semantic_seeding = enable_semantic_seeding
        self.semantic_seed_top_k = semantic_seed_top_k
        self.semantic_seed_weight = semantic_seed_weight

    def get_statement(
        self, query: str, query_embedding: list[float] | None, limit: int = 60, **kwargs: Any
    ) -> Select | CompoundSelect:
        vault_ids = kwargs.get('vault_ids')
        vault_id = vault_ids[0] if vault_ids else None

        # Build seed entity CTE via shared helper.
        # The original GraphStrategy included ilike in its NER path, so include_ilike=True.
        seed_entities = build_seed_entity_cte(
            query=query,
            ner_model=self.ner_model,
            similarity_threshold=self.similarity_threshold,
            include_ilike=True,
            cte_name='seed_entities',
            query_embedding=query_embedding,
            vault_id=vault_id,
            semantic_seed_top_k=self.semantic_seed_top_k,
            semantic_seed_weight=self.semantic_seed_weight,
            enable_semantic_seeding=self.enable_semantic_seeding,
        )

        # Base Selects
        select_first = select(MemoryUnit.id).select_from(MemoryUnit)
        select_second = select(MemoryUnit.id).select_from(MemoryUnit)

        # Apply Filters
        select_first = apply_date_filters(select_first, MemoryUnit.event_date, **kwargs)
        select_first = apply_vault_filters(select_first, MemoryUnit.vault_id, **kwargs)
        select_first = apply_generic_filters(select_first, **kwargs)

        select_second = apply_date_filters(select_second, MemoryUnit.event_date, **kwargs)
        select_second = apply_vault_filters(select_second, MemoryUnit.vault_id, **kwargs)
        select_second = apply_generic_filters(select_second, **kwargs)

        # 2. 1st Order Memories (Direct Link)
        # V2 Scoring: 1.0 + Temporal Decay
        # Temporal Score = 2.0 ^ (-days / 30.0)

        # Calculate days difference from NOW().
        # Note: We rely on DB time.
        days_diff = (
            func.extract('epoch', func.now()) - func.extract('epoch', col(MemoryUnit.event_date))
        ) / 86400.0

        # We clamp days_diff to be at least 0 to avoid boost from future dates?
        # Actually logic says if < 0 return 1.0 (max score).
        # In SQL, we can just use the raw value, exponential of negative is > 1.
        # But let's stick to simple decay for now.

        temporal_score = func.power(
            self.temporal_decay_base, -(days_diff / self.temporal_decay_days)
        )

        first_order = (
            select_first.add_columns((seed_entities.c.weight + temporal_score).label('score'))
            .join(UnitEntity, col(UnitEntity.unit_id) == col(MemoryUnit.id))
            .join(seed_entities, col(UnitEntity.entity_id) == seed_entities.c.id)
        )

        # 3. 2nd Order Entities (Co-occurrences)
        # V2 Scoring: Weighted Context
        # Score = log2(cooc_count + 1) / log2(neighbor_mention_count + 2)

        # We need to compute neighbor_id first to join with Entity
        neighbor_id_expr = func.coalesce(
            func.nullif(col(EntityCooccurrence.entity_id_2), seed_entities.c.id),
            col(EntityCooccurrence.entity_id_1),
        ).label('neighbor_id')

        co_occur_stmt = (
            select(
                neighbor_id_expr,
                (
                    func.ln(col(EntityCooccurrence.cooccurrence_count) + 1)
                    / func.ln(col(Entity.mention_count) + 2)
                ).label('link_strength'),
            )
            .join(
                seed_entities,
                or_(
                    col(EntityCooccurrence.entity_id_1) == seed_entities.c.id,
                    col(EntityCooccurrence.entity_id_2) == seed_entities.c.id,
                ),
            )
            .join(Entity, col(Entity.id) == neighbor_id_expr)
        )

        co_occur_stmt = apply_vault_filters(co_occur_stmt, EntityCooccurrence.vault_id, **kwargs)
        co_occurrences = co_occur_stmt.cte('related_entities')

        # 4. 2nd Order Memories (Indirect Link)
        second_order = (
            select_second.add_columns((co_occurrences.c.link_strength).label('score'))
            .join(UnitEntity, col(UnitEntity.unit_id) == col(MemoryUnit.id))
            .join(co_occurrences, col(UnitEntity.entity_id) == co_occurrences.c.neighbor_id)
        )

        # Combine 1st and 2nd order.
        return first_order.union_all(second_order).order_by(text('score DESC')).limit(limit)


# Backward-compatible alias
GraphStrategy = EntityCooccurrenceGraphStrategy


class MentalModelStrategy:
    """
    Retrieves Mental Models directly using Vector Similarity on the model's summary embedding.
    Runs independently on the mental_models table.
    """

    def get_statement(
        self, query: str, query_embedding: list[float] | None, limit: int = 60, **kwargs: Any
    ) -> Select:
        statement = select(MentalModel.id).select_from(MentalModel)

        # Apply Filters (Mental Models track last_refreshed)
        statement = apply_date_filters(statement, MentalModel.last_refreshed, **kwargs)
        statement = apply_vault_filters(statement, MentalModel.vault_id, **kwargs)
        # Generic filters generally don't apply to MentalModels in the same way (no fact_type),
        # so we skip apply_generic_filters here or check column existence.
        # MentalModels don't have 'fact_type', so we skip it.

        if query_embedding is None:
            # Fallback to name match if no embedding
            return (
                statement.add_columns(literal(1.0).label('score'))
                .where(col(MentalModel.name).ilike(f'%{query}%'))
                .limit(limit)
            )

        from typing import Any, cast

        distance_col = cast(Any, col(MentalModel.embedding)).cosine_distance(query_embedding)

        return (
            statement.add_columns(distance_col.label('score')).order_by(distance_col).limit(limit)
        )


class TemporalStrategy:
    """
    Retrieves memories based on temporal relevance.
    Score: Event Date Timestamp (Higher/Newer is better).
    """

    def get_statement(
        self, query: str, query_embedding: list[float] | None, limit: int = 60, **kwargs: Any
    ) -> Select:
        score_col = func.extract('epoch', col(MemoryUnit.event_date))

        statement = (
            select(MemoryUnit.id).select_from(MemoryUnit).add_columns(score_col.label('score'))
        )

        statement = apply_date_filters(statement, MemoryUnit.event_date, **kwargs)
        statement = apply_vault_filters(statement, MemoryUnit.vault_id, **kwargs)
        statement = apply_generic_filters(statement, **kwargs)

        return statement.order_by(desc(col(MemoryUnit.event_date))).limit(limit)


# ---------------------------------------------------------------------------
# Note-level graph strategy (entity co-occurrence)
# ---------------------------------------------------------------------------


class EntityCooccurrenceNoteGraphStrategy:
    """Note/chunk variant of :class:`EntityCooccurrenceGraphStrategy`.

    Traversal: seed entities -> ``UnitEntity`` -> ``MemoryUnit`` -> ``Note``
    -> ``Chunk``. Includes 1st-order (direct entity match) and 2nd-order
    (co-occurrence) results with temporal decay scoring.

    Returns ``(Chunk.id, score)`` where higher score is better.

    Config dependencies: same as :class:`EntityCooccurrenceGraphStrategy`.
    """

    def __init__(
        self,
        ner_model: FastNERModel | None = None,
        similarity_threshold: float = 0.3,
        temporal_decay_days: float = 30.0,
        temporal_decay_base: float = 2.0,
        enable_semantic_seeding: bool = True,
        semantic_seed_top_k: int = 5,
        semantic_seed_weight: float = 0.7,
    ):
        self.ner_model = ner_model
        self.similarity_threshold = similarity_threshold
        self.temporal_decay_days = temporal_decay_days
        self.temporal_decay_base = temporal_decay_base
        self.enable_semantic_seeding = enable_semantic_seeding
        self.semantic_seed_top_k = semantic_seed_top_k
        self.semantic_seed_weight = semantic_seed_weight

    def get_statement(
        self, query: str, query_embedding: list[float] | None, limit: int = 60, **kwargs: Any
    ) -> Select | CompoundSelect:
        """Build a query returning (Chunk.id, score) via entity graph traversal."""
        vault_ids = kwargs.get('vault_ids')
        vault_id = vault_ids[0] if vault_ids else None

        # NoteGraphStrategy always included ilike in NER path
        seed_entities = build_seed_entity_cte(
            query=query,
            ner_model=self.ner_model,
            similarity_threshold=self.similarity_threshold,
            include_ilike=True,
            cte_name='doc_graph_seed_entities',
            query_embedding=query_embedding,
            vault_id=vault_id,
            semantic_seed_top_k=self.semantic_seed_top_k,
            semantic_seed_weight=self.semantic_seed_weight,
            enable_semantic_seeding=self.enable_semantic_seeding,
        )

        # 1st Order: Entity -> UnitEntity -> MemoryUnit -> Document -> Chunk
        days_diff = (
            func.extract('epoch', func.now()) - func.extract('epoch', col(MemoryUnit.event_date))
        ) / 86400.0
        temporal_score = func.power(
            self.temporal_decay_base, -(days_diff / self.temporal_decay_days)
        )

        include_stale = kwargs.get('include_stale', False)

        first_order = (
            select(Chunk.id)
            .select_from(Chunk)
            .add_columns((seed_entities.c.weight + temporal_score).label('score'))
            .join(Note, col(Note.id) == col(Chunk.note_id))
            .join(MemoryUnit, col(MemoryUnit.note_id) == col(Note.id))
            .join(UnitEntity, col(UnitEntity.unit_id) == col(MemoryUnit.id))
            .join(seed_entities, col(UnitEntity.entity_id) == seed_entities.c.id)
        )
        if not include_stale:
            first_order = first_order.where(col(Chunk.status) == ContentStatus.ACTIVE)

        first_order = apply_vault_filters(first_order, Chunk.vault_id, **kwargs)

        # 2nd Order: Co-occurrence expansion
        neighbor_id_expr = func.coalesce(
            func.nullif(col(EntityCooccurrence.entity_id_2), seed_entities.c.id),
            col(EntityCooccurrence.entity_id_1),
        ).label('neighbor_id')

        co_occur_stmt = (
            select(
                neighbor_id_expr,
                (
                    func.ln(col(EntityCooccurrence.cooccurrence_count) + 1)
                    / func.ln(col(Entity.mention_count) + 2)
                ).label('link_strength'),
            )
            .join(
                seed_entities,
                or_(
                    col(EntityCooccurrence.entity_id_1) == seed_entities.c.id,
                    col(EntityCooccurrence.entity_id_2) == seed_entities.c.id,
                ),
            )
            .join(Entity, col(Entity.id) == neighbor_id_expr)
        )
        co_occur_stmt = apply_vault_filters(co_occur_stmt, EntityCooccurrence.vault_id, **kwargs)
        co_occurrences = co_occur_stmt.cte('doc_graph_related_entities')

        second_order = (
            select(Chunk.id)
            .select_from(Chunk)
            .add_columns(co_occurrences.c.link_strength.label('score'))
            .join(Note, col(Note.id) == col(Chunk.note_id))
            .join(MemoryUnit, col(MemoryUnit.note_id) == col(Note.id))
            .join(UnitEntity, col(UnitEntity.unit_id) == col(MemoryUnit.id))
            .join(co_occurrences, col(UnitEntity.entity_id) == co_occurrences.c.neighbor_id)
        )
        if not include_stale:
            second_order = second_order.where(col(Chunk.status) == ContentStatus.ACTIVE)
        second_order = apply_vault_filters(second_order, Chunk.vault_id, **kwargs)

        return first_order.union_all(second_order).order_by(text('score DESC')).limit(limit)


# Backward-compatible alias
NoteGraphStrategy = EntityCooccurrenceNoteGraphStrategy


# ---------------------------------------------------------------------------
# Causal graph expansion strategies (T2)
# ---------------------------------------------------------------------------

CAUSAL_LINK_TYPES = ('causes', 'caused_by', 'enables', 'prevents')


class CausalGraphStrategy:
    """Graph retrieval expanding through causal edges in ``memory_links``.

    Traversal:
        seed_entities -> UnitEntity -> MemoryUnit  (1st order, score=1.0+decay)
        1st-order ids -> memory_links (causal)     -> MemoryUnit  (2nd order,
        score=weight*0.8)

    Combined via UNION ALL -> GROUP BY id, MAX(score). Only causal link types
    (``causes``, ``caused_by``, ``enables``, ``prevents``) with weight >=
    ``causal_weight_threshold`` are traversed.

    Config dependencies:
        ``RetrievalConfig.causal_weight_threshold``,
        ``RetrievalConfig.similarity_threshold``,
        ``RetrievalConfig.temporal_decay_days``,
        ``RetrievalConfig.temporal_decay_base``.
    """

    def __init__(
        self,
        ner_model: FastNERModel | None = None,
        similarity_threshold: float = 0.3,
        temporal_decay_days: float = 30.0,
        temporal_decay_base: float = 2.0,
        causal_weight_threshold: float = 0.3,
        enable_semantic_seeding: bool = True,
        semantic_seed_top_k: int = 5,
        semantic_seed_weight: float = 0.7,
    ):
        self.ner_model = ner_model
        self.similarity_threshold = similarity_threshold
        self.temporal_decay_days = temporal_decay_days
        self.temporal_decay_base = temporal_decay_base
        self.causal_weight_threshold = causal_weight_threshold
        self.enable_semantic_seeding = enable_semantic_seeding
        self.semantic_seed_top_k = semantic_seed_top_k
        self.semantic_seed_weight = semantic_seed_weight

    def get_statement(
        self,
        query: str,
        query_embedding: list[float] | None,
        limit: int = 60,
        **kwargs: Any,
    ) -> Select | CompoundSelect:
        vault_ids = kwargs.get('vault_ids')
        vault_id = vault_ids[0] if vault_ids else None

        seed_entities = build_seed_entity_cte(
            query=query,
            ner_model=self.ner_model,
            similarity_threshold=self.similarity_threshold,
            include_ilike=True,
            cte_name='causal_seed_entities',
            query_embedding=query_embedding,
            vault_id=vault_id,
            semantic_seed_top_k=self.semantic_seed_top_k,
            semantic_seed_weight=self.semantic_seed_weight,
            enable_semantic_seeding=self.enable_semantic_seeding,
        )

        # -- 1st order: seed -> UnitEntity -> MemoryUnit --------------------
        select_first = select(MemoryUnit.id).select_from(MemoryUnit)
        select_first = apply_date_filters(select_first, MemoryUnit.event_date, **kwargs)
        select_first = apply_vault_filters(select_first, MemoryUnit.vault_id, **kwargs)
        select_first = apply_generic_filters(select_first, **kwargs)

        days_diff = (
            func.extract('epoch', func.now()) - func.extract('epoch', col(MemoryUnit.event_date))
        ) / 86400.0
        temporal_score = func.power(
            self.temporal_decay_base,
            -(days_diff / self.temporal_decay_days),
        )

        first_order = (
            select_first.add_columns((literal(1.0) + temporal_score).label('score'))
            .join(UnitEntity, col(UnitEntity.unit_id) == col(MemoryUnit.id))
            .join(seed_entities, col(UnitEntity.entity_id) == seed_entities.c.id)
        )

        # -- causal expansion: 1st-order -> memory_links -> MemoryUnit ------
        first_order_cte = first_order.cte('causal_first_order')

        select_causal = select(MemoryUnit.id).select_from(MemoryUnit)
        select_causal = apply_date_filters(select_causal, MemoryUnit.event_date, **kwargs)
        select_causal = apply_vault_filters(select_causal, MemoryUnit.vault_id, **kwargs)
        select_causal = apply_generic_filters(select_causal, **kwargs)

        causal_expansion = (
            select_causal.add_columns((col(MemoryLink.weight) * literal(0.8)).label('score'))
            .join(MemoryLink, col(MemoryLink.to_unit_id) == col(MemoryUnit.id))
            .join(first_order_cte, col(MemoryLink.from_unit_id) == first_order_cte.c.id)
            .where(col(MemoryLink.link_type).in_(CAUSAL_LINK_TYPES))
            .where(col(MemoryLink.weight) >= self.causal_weight_threshold)
        )

        # -- combine & deduplicate: UNION ALL -> GROUP BY id, MAX(score) ----
        combined_cte = (
            select(first_order_cte.c.id, first_order_cte.c.score)
            .union_all(causal_expansion)
            .cte('causal_combined')
        )

        return (
            select(
                combined_cte.c.id,
                func.max(combined_cte.c.score).label('score'),
            )
            .group_by(combined_cte.c.id)
            .order_by(text('score DESC'))
            .limit(limit)
        )


class CausalNoteGraphStrategy:
    """Note/chunk variant of :class:`CausalGraphStrategy`.

    Returns ``(Chunk.id, score)`` by joining causal-expanded memory unit IDs
    back through ``MemoryUnit -> Note -> Chunk``.

    Config dependencies: same as :class:`CausalGraphStrategy`.
    """

    def __init__(
        self,
        ner_model: FastNERModel | None = None,
        similarity_threshold: float = 0.3,
        temporal_decay_days: float = 30.0,
        temporal_decay_base: float = 2.0,
        causal_weight_threshold: float = 0.3,
        enable_semantic_seeding: bool = True,
        semantic_seed_top_k: int = 5,
        semantic_seed_weight: float = 0.7,
    ):
        self.ner_model = ner_model
        self.similarity_threshold = similarity_threshold
        self.temporal_decay_days = temporal_decay_days
        self.temporal_decay_base = temporal_decay_base
        self.causal_weight_threshold = causal_weight_threshold
        self.enable_semantic_seeding = enable_semantic_seeding
        self.semantic_seed_top_k = semantic_seed_top_k
        self.semantic_seed_weight = semantic_seed_weight

    def get_statement(
        self,
        query: str,
        query_embedding: list[float] | None,
        limit: int = 60,
        **kwargs: Any,
    ) -> Select | CompoundSelect:
        vault_ids = kwargs.get('vault_ids')
        vault_id = vault_ids[0] if vault_ids else None

        seed_entities = build_seed_entity_cte(
            query=query,
            ner_model=self.ner_model,
            similarity_threshold=self.similarity_threshold,
            include_ilike=True,
            cte_name='causal_note_seed_entities',
            query_embedding=query_embedding,
            vault_id=vault_id,
            semantic_seed_top_k=self.semantic_seed_top_k,
            semantic_seed_weight=self.semantic_seed_weight,
            enable_semantic_seeding=self.enable_semantic_seeding,
        )

        include_stale = kwargs.get('include_stale', False)

        # -- 1st order: seed -> UnitEntity -> MU -> Note -> Chunk -----------
        days_diff = (
            func.extract('epoch', func.now()) - func.extract('epoch', col(MemoryUnit.event_date))
        ) / 86400.0
        temporal_score = func.power(
            self.temporal_decay_base,
            -(days_diff / self.temporal_decay_days),
        )

        first_order = (
            select(Chunk.id)
            .select_from(Chunk)
            .add_columns((literal(1.0) + temporal_score).label('score'))
            .join(Note, col(Note.id) == col(Chunk.note_id))
            .join(MemoryUnit, col(MemoryUnit.note_id) == col(Note.id))
            .join(UnitEntity, col(UnitEntity.unit_id) == col(MemoryUnit.id))
            .join(seed_entities, col(UnitEntity.entity_id) == seed_entities.c.id)
        )
        if not include_stale:
            first_order = first_order.where(col(Chunk.status) == ContentStatus.ACTIVE)
        first_order = apply_vault_filters(first_order, Chunk.vault_id, **kwargs)

        # Build CTE of 1st-order MemoryUnit ids for causal join
        mu_first = (
            select(MemoryUnit.id)
            .select_from(MemoryUnit)
            .join(UnitEntity, col(UnitEntity.unit_id) == col(MemoryUnit.id))
            .join(seed_entities, col(UnitEntity.entity_id) == seed_entities.c.id)
        ).cte('causal_note_first_mu')

        # -- causal expansion -> Chunk -------------------------------------
        causal_expansion = (
            select(Chunk.id)
            .select_from(Chunk)
            .add_columns((col(MemoryLink.weight) * literal(0.8)).label('score'))
            .join(Note, col(Note.id) == col(Chunk.note_id))
            .join(MemoryUnit, col(MemoryUnit.note_id) == col(Note.id))
            .join(MemoryLink, col(MemoryLink.to_unit_id) == col(MemoryUnit.id))
            .join(mu_first, col(MemoryLink.from_unit_id) == mu_first.c.id)
            .where(col(MemoryLink.link_type).in_(CAUSAL_LINK_TYPES))
            .where(col(MemoryLink.weight) >= self.causal_weight_threshold)
        )
        if not include_stale:
            causal_expansion = causal_expansion.where(col(Chunk.status) == ContentStatus.ACTIVE)
        causal_expansion = apply_vault_filters(causal_expansion, Chunk.vault_id, **kwargs)

        return first_order.union_all(causal_expansion).order_by(text('score DESC')).limit(limit)


# ---------------------------------------------------------------------------
# Link-expansion graph strategies (T4)
# ---------------------------------------------------------------------------


class LinkExpansionGraphStrategy:
    """Graph retrieval expanding through 3 link signals with additive scoring.

    Signals (each contributes 0-1 to the final score, range 0-3):
    * **entity** -- co-occurring units via ``memory_links(type='entity')``,
      scored by ``tanh(distinct_entity_count * 0.5)``
    * **semantic** -- bidirectional kNN via ``memory_links(type='semantic')``,
      scored by max link weight
    * **causal** -- causal chain via ``memory_links(type IN causal_types)``,
      filtered by ``causal_threshold``

    Config dependencies:
        ``RetrievalConfig.link_expansion_causal_threshold``,
        ``RetrievalConfig.similarity_threshold``.
    """

    def __init__(
        self,
        ner_model: FastNERModel | None = None,
        similarity_threshold: float = 0.3,
        causal_threshold: float = 0.3,
        enable_semantic_seeding: bool = True,
        semantic_seed_top_k: int = 5,
        semantic_seed_weight: float = 0.7,
    ):
        self.ner_model = ner_model
        self.similarity_threshold = similarity_threshold
        self.causal_threshold = causal_threshold
        self.enable_semantic_seeding = enable_semantic_seeding
        self.semantic_seed_top_k = semantic_seed_top_k
        self.semantic_seed_weight = semantic_seed_weight

    def get_statement(
        self,
        query: str,
        query_embedding: list[float] | None,
        limit: int = 60,
        **kwargs: Any,
    ) -> Select | CompoundSelect:
        vault_ids = kwargs.get('vault_ids')
        vault_id = vault_ids[0] if vault_ids else None

        # Seed entities -------------------------------------------------
        seed_entities = build_seed_entity_cte(
            query=query,
            ner_model=self.ner_model,
            similarity_threshold=self.similarity_threshold,
            include_ilike=True,
            cte_name='le_seed_entities',
            query_embedding=query_embedding,
            vault_id=vault_id,
            semantic_seed_top_k=self.semantic_seed_top_k,
            semantic_seed_weight=self.semantic_seed_weight,
            enable_semantic_seeding=self.enable_semantic_seeding,
        )

        # First-order units (seed -> UnitEntity -> MemoryUnit) -----------
        fo_stmt = (
            select(col(MemoryUnit.id).label('unit_id'))
            .select_from(MemoryUnit)
            .join(UnitEntity, col(UnitEntity.unit_id) == col(MemoryUnit.id))
            .join(seed_entities, col(UnitEntity.entity_id) == seed_entities.c.id)
        )
        fo_stmt = apply_date_filters(fo_stmt, MemoryUnit.event_date, **kwargs)
        fo_stmt = apply_vault_filters(fo_stmt, MemoryUnit.vault_id, **kwargs)
        fo_stmt = apply_generic_filters(fo_stmt, **kwargs)
        first_order = fo_stmt.cte('le_first_order')

        # 1. Entity expansion -------------------------------------------
        entity_expanded = (
            select(
                col(MemoryLink.to_unit_id).label('id'),
                func.tanh(func.count(distinct(col(MemoryLink.entity_id))) * 0.5).label('score'),
            )
            .select_from(MemoryLink)
            .join(first_order, col(MemoryLink.from_unit_id) == first_order.c.unit_id)
            .where(col(MemoryLink.link_type) == 'entity')
            .where(col(MemoryLink.entity_id).is_not(None))
            .group_by(col(MemoryLink.to_unit_id))
        )

        # 2. Semantic expansion (bidirectional) -------------------------
        semantic_fwd = (
            select(
                col(MemoryLink.to_unit_id).label('id'),
                col(MemoryLink.weight).label('score'),
            )
            .select_from(MemoryLink)
            .join(first_order, col(MemoryLink.from_unit_id) == first_order.c.unit_id)
            .where(col(MemoryLink.link_type) == 'semantic')
        )
        semantic_bwd = (
            select(
                col(MemoryLink.from_unit_id).label('id'),
                col(MemoryLink.weight).label('score'),
            )
            .select_from(MemoryLink)
            .join(first_order, col(MemoryLink.to_unit_id) == first_order.c.unit_id)
            .where(col(MemoryLink.link_type) == 'semantic')
        )
        semantic_union = semantic_fwd.union_all(semantic_bwd).subquery('le_sem_raw')
        semantic_expanded = select(
            semantic_union.c.id.label('id'),
            func.max(semantic_union.c.score).label('score'),
        ).group_by(semantic_union.c.id)

        # 3. Causal expansion -------------------------------------------
        causal_expanded = (
            select(
                col(MemoryLink.to_unit_id).label('id'),
                col(MemoryLink.weight).label('score'),
            )
            .select_from(MemoryLink)
            .join(first_order, col(MemoryLink.from_unit_id) == first_order.c.unit_id)
            .where(col(MemoryLink.link_type).in_(CAUSAL_LINK_TYPES))
            .where(col(MemoryLink.weight) >= self.causal_threshold)
        )

        # Combined: UNION ALL -> GROUP BY -> SUM -------------------------
        all_signals = union_all(entity_expanded, semantic_expanded, causal_expanded).subquery(
            'le_all_signals'
        )

        combined = (
            select(
                all_signals.c.id.label('id'),
                func.sum(all_signals.c.score).label('score'),
            )
            .group_by(all_signals.c.id)
            .order_by(desc(func.sum(all_signals.c.score)))
            .limit(limit)
        )

        return combined


class LinkExpansionNoteGraphStrategy:
    """Note/chunk variant of :class:`LinkExpansionGraphStrategy`.

    Returns ``(Chunk.id, score)`` by joining expanded memory-unit IDs back
    through ``MemoryUnit -> Note -> Chunk``.

    Config dependencies: same as :class:`LinkExpansionGraphStrategy`.
    """

    def __init__(
        self,
        ner_model: FastNERModel | None = None,
        similarity_threshold: float = 0.3,
        causal_threshold: float = 0.3,
        enable_semantic_seeding: bool = True,
        semantic_seed_top_k: int = 5,
        semantic_seed_weight: float = 0.7,
    ):
        self.ner_model = ner_model
        self.similarity_threshold = similarity_threshold
        self.causal_threshold = causal_threshold
        self.enable_semantic_seeding = enable_semantic_seeding
        self.semantic_seed_top_k = semantic_seed_top_k
        self.semantic_seed_weight = semantic_seed_weight

    def get_statement(
        self,
        query: str,
        query_embedding: list[float] | None,
        limit: int = 60,
        **kwargs: Any,
    ) -> Select | CompoundSelect:
        # Re-use the unit-level strategy to get (id, score).
        unit_strategy = LinkExpansionGraphStrategy(
            ner_model=self.ner_model,
            similarity_threshold=self.similarity_threshold,
            causal_threshold=self.causal_threshold,
            enable_semantic_seeding=self.enable_semantic_seeding,
            semantic_seed_top_k=self.semantic_seed_top_k,
            semantic_seed_weight=self.semantic_seed_weight,
        )
        unit_result = unit_strategy.get_statement(
            query, query_embedding, limit=limit * 2, **kwargs
        ).subquery('le_unit_scores')

        include_stale = kwargs.get('include_stale', False)

        stmt = (
            select(col(Chunk.id).label('id'))
            .select_from(Chunk)
            .add_columns(unit_result.c.score.label('score'))
            .join(Note, col(Note.id) == col(Chunk.note_id))
            .join(MemoryUnit, col(MemoryUnit.note_id) == col(Note.id))
            .join(unit_result, col(MemoryUnit.id) == unit_result.c.id)
        )
        if not include_stale:
            stmt = stmt.where(col(Chunk.status) == ContentStatus.ACTIVE)
        stmt = apply_vault_filters(stmt, Chunk.vault_id, **kwargs)

        return stmt.order_by(desc(unit_result.c.score)).limit(limit)


# ---------------------------------------------------------------------------
# Factory functions
# ---------------------------------------------------------------------------

_GRAPH_STRATEGY_REGISTRY: dict[str, type] = {
    'entity_cooccurrence': EntityCooccurrenceGraphStrategy,
    'causal': CausalGraphStrategy,
    'link_expansion': LinkExpansionGraphStrategy,
}

_NOTE_GRAPH_STRATEGY_REGISTRY: dict[str, type] = {
    'entity_cooccurrence': EntityCooccurrenceNoteGraphStrategy,
    'causal': CausalNoteGraphStrategy,
    'link_expansion': LinkExpansionNoteGraphStrategy,
}


def get_graph_strategy(
    type: str = 'entity_cooccurrence',
    ner_model: FastNERModel | None = None,
    **kwargs: Any,
) -> RetrievalStrategy:
    """Create a memory-level graph retrieval strategy by name.

    Args:
        type: Strategy identifier (e.g. ``'entity_cooccurrence'``,
            ``'causal'``, ``'link_expansion'``).
        ner_model: Optional NER model for entity extraction.
        **kwargs: Forwarded to the strategy constructor.

    Returns:
        A ``RetrievalStrategy``-compatible instance.

    Raises:
        ValueError: If *type* is not registered.
    """
    cls = _GRAPH_STRATEGY_REGISTRY.get(type)
    if cls is None:
        raise ValueError(
            f'Unknown graph retriever type: {type!r}. Available: {sorted(_GRAPH_STRATEGY_REGISTRY)}'
        )
    return cls(ner_model=ner_model, **kwargs)  # type: ignore[return-value]


def get_note_graph_strategy(
    type: str = 'entity_cooccurrence',
    ner_model: FastNERModel | None = None,
    **kwargs: Any,
) -> RetrievalStrategy:
    """Create a note-level graph retrieval strategy by name.

    Args:
        type: Strategy identifier (e.g. ``'entity_cooccurrence'``,
            ``'causal'``, ``'link_expansion'``).
        ner_model: Optional NER model for entity extraction.
        **kwargs: Forwarded to the strategy constructor.

    Returns:
        A ``RetrievalStrategy``-compatible instance.

    Raises:
        ValueError: If *type* is not registered.
    """
    cls = _NOTE_GRAPH_STRATEGY_REGISTRY.get(type)
    if cls is None:
        raise ValueError(
            f'Unknown note graph retriever type: {type!r}. '
            f'Available: {sorted(_NOTE_GRAPH_STRATEGY_REGISTRY)}'
        )
    return cls(ner_model=ner_model, **kwargs)  # type: ignore[return-value]
