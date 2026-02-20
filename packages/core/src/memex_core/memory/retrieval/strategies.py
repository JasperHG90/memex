import logging
from typing import Protocol, Any, runtime_checkable

from sqlalchemy import func, desc, literal, or_, text, cast, String
from sqlalchemy.sql import Select, CompoundSelect
from sqlmodel import select, col

from memex_core.memory.sql_models import (
    MemoryUnit,
    Entity,
    EntityAlias,
    UnitEntity,
    EntityCooccurrence,
    Chunk,
    Document,
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


class GraphStrategy:
    """
    Retrieves memories linked to entities mentioned in the query (1st Order)
    AND entities related to those entities (2nd Order BFS).

    V2 Updates:
    - NER-Driven Query Expansion
    - Phonetic Search
    - Exponential Decay
    - Weighted Context
    """

    def __init__(self, ner_model: FastNERModel | None = None):
        self.ner_model = ner_model

    def get_statement(
        self, query: str, query_embedding: list[float] | None, limit: int = 60, **kwargs: Any
    ) -> Select | CompoundSelect:
        # 1. NER-Driven Extraction
        # Try to extract entities from the query
        extracted_names = []
        extracted_phonetics = []

        if self.ner_model:
            try:
                extracted = self.ner_model.predict(query)
                extracted_names = [e['word'] for e in extracted]

                # Compute phonetics for extracted entities
                for name in extracted_names:
                    p_code = get_phonetic_code(name)
                    if p_code:
                        extracted_phonetics.append(p_code)

            except Exception as e:
                logger.warning(f'NER Extraction failed: {e}. Falling back to naive search.')

        # 2. Build Seed Query
        if extracted_names:
            # --- NER PATH ---
            logger.info(f'NER found entities: {extracted_names}')

            # Canonical Name Matches (Exact, Phonetic, or Fuzzy)
            conds_canonical = []
            conds_canonical.append(col(Entity.canonical_name).in_(extracted_names))
            if extracted_phonetics:
                conds_canonical.append(col(Entity.phonetic_code).in_(extracted_phonetics))
            for name in extracted_names:
                conds_canonical.append(col(Entity.canonical_name).ilike(f'%{name}%'))
                conds_canonical.append(func.similarity(col(Entity.canonical_name), name) > 0.3)

            seed_from_canonical = select(col(Entity.id).label('id')).where(or_(*conds_canonical))

            # Alias Matches (Exact, Phonetic, or Fuzzy)
            conds_alias = []
            conds_alias.append(col(EntityAlias.name).in_(extracted_names))
            if extracted_phonetics:
                conds_alias.append(col(EntityAlias.phonetic_code).in_(extracted_phonetics))
            for name in extracted_names:
                conds_alias.append(col(EntityAlias.name).ilike(f'%{name}%'))
                conds_alias.append(func.similarity(col(EntityAlias.name), name) > 0.3)

            seed_from_alias = select(col(EntityAlias.canonical_id).label('id')).where(
                or_(*conds_alias)
            )

        else:
            # --- FALLBACK PATH (Legacy) ---
            logger.info('No entities found by NER. Using fallback similarity search.')
            seed_from_canonical = select(col(Entity.id).label('id')).where(
                or_(
                    col(Entity.canonical_name).ilike(f'%{query}%'),
                    func.similarity(col(Entity.canonical_name), query) > 0.3,
                )
            )

            seed_from_alias = select(col(EntityAlias.canonical_id).label('id')).where(
                or_(
                    col(EntityAlias.name).ilike(f'%{query}%'),
                    func.similarity(col(EntityAlias.name), query) > 0.3,
                )
            )

        combined_seeds = seed_from_canonical.union(seed_from_alias).subquery('t')

        seed_entities = (
            select(combined_seeds.c.id.label('id'), literal(1.0).label('weight')).select_from(
                combined_seeds
            )
        ).cte('seed_entities')

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

        temporal_score = func.power(2.0, -(days_diff / 30.0))

        first_order = (
            select_first.add_columns((literal(1.0) + temporal_score).label('score'))
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


class DocumentGraphStrategy:
    """
    Retrieves document chunks linked to entities mentioned in the query.

    Traversal path: Entity → UnitEntity → MemoryUnit → Document → Chunk.
    Includes both 1st-order (direct entity match) and 2nd-order (co-occurrence)
    results, with temporal decay scoring for recency.

    Returns Chunk.id + score (higher is better).
    """

    def __init__(self, ner_model: 'FastNERModel | None' = None):
        self.ner_model = ner_model

    def _build_seed_entities(self, query: str) -> Select | CompoundSelect:
        """Build the seed entity query using NER extraction or fallback similarity."""
        extracted_names: list[str] = []
        extracted_phonetics: list[str] = []

        if self.ner_model:
            try:
                extracted = self.ner_model.predict(query)
                extracted_names = [e['word'] for e in extracted]
                for name in extracted_names:
                    p_code = get_phonetic_code(name)
                    if p_code:
                        extracted_phonetics.append(p_code)
            except Exception as e:
                logger.warning(f'NER extraction failed for doc graph: {e}. Falling back.')

        if extracted_names:
            conds_canonical: list[Any] = [col(Entity.canonical_name).in_(extracted_names)]
            if extracted_phonetics:
                conds_canonical.append(col(Entity.phonetic_code).in_(extracted_phonetics))
            for name in extracted_names:
                conds_canonical.append(col(Entity.canonical_name).ilike(f'%{name}%'))
                conds_canonical.append(func.similarity(col(Entity.canonical_name), name) > 0.3)

            seed_from_canonical = select(col(Entity.id).label('id')).where(or_(*conds_canonical))

            conds_alias: list[Any] = [col(EntityAlias.name).in_(extracted_names)]
            if extracted_phonetics:
                conds_alias.append(col(EntityAlias.phonetic_code).in_(extracted_phonetics))
            for name in extracted_names:
                conds_alias.append(col(EntityAlias.name).ilike(f'%{name}%'))
                conds_alias.append(func.similarity(col(EntityAlias.name), name) > 0.3)

            seed_from_alias = select(col(EntityAlias.canonical_id).label('id')).where(
                or_(*conds_alias)
            )
        else:
            logger.info('No entities found by NER for doc graph. Using fallback similarity.')
            seed_from_canonical = select(col(Entity.id).label('id')).where(
                or_(
                    col(Entity.canonical_name).ilike(f'%{query}%'),
                    func.similarity(col(Entity.canonical_name), query) > 0.3,
                )
            )
            seed_from_alias = select(col(EntityAlias.canonical_id).label('id')).where(
                or_(
                    col(EntityAlias.name).ilike(f'%{query}%'),
                    func.similarity(col(EntityAlias.name), query) > 0.3,
                )
            )

        return seed_from_canonical.union(seed_from_alias)

    def get_statement(
        self, query: str, query_embedding: list[float] | None, limit: int = 60, **kwargs: Any
    ) -> Select | CompoundSelect:
        """Build a query returning (Chunk.id, score) via entity graph traversal."""
        combined_seeds = self._build_seed_entities(query).subquery('doc_graph_seeds_t')

        seed_entities = (
            select(combined_seeds.c.id.label('id'), literal(1.0).label('weight')).select_from(
                combined_seeds
            )
        ).cte('doc_graph_seed_entities')

        # 1st Order: Entity → UnitEntity → MemoryUnit → Document → Chunk
        days_diff = (
            func.extract('epoch', func.now()) - func.extract('epoch', col(MemoryUnit.event_date))
        ) / 86400.0
        temporal_score = func.power(2.0, -(days_diff / 30.0))

        include_stale = kwargs.get('include_stale', False)

        first_order = (
            select(Chunk.id)
            .select_from(Chunk)
            .add_columns((literal(1.0) + temporal_score).label('score'))
            .join(Document, col(Document.id) == col(Chunk.document_id))
            .join(MemoryUnit, col(MemoryUnit.document_id) == col(Document.id))
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
            .join(Document, col(Document.id) == col(Chunk.document_id))
            .join(MemoryUnit, col(MemoryUnit.document_id) == col(Document.id))
            .join(UnitEntity, col(UnitEntity.unit_id) == col(MemoryUnit.id))
            .join(co_occurrences, col(UnitEntity.entity_id) == co_occurrences.c.neighbor_id)
        )
        if not include_stale:
            second_order = second_order.where(col(Chunk.status) == ContentStatus.ACTIVE)
        second_order = apply_vault_filters(second_order, Chunk.vault_id, **kwargs)

        return first_order.union_all(second_order).order_by(text('score DESC')).limit(limit)
