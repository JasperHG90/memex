"""
Entity extraction and resolution for memory system.
"""

from uuid import UUID as PyUUID
from datetime import datetime, timezone
from typing import Any
from collections import defaultdict
import logging
import math
import itertools

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import col, text, update, select, desc
from sqlmodel.ext.asyncio.session import AsyncSession
from pydantic import BaseModel

from memex_core.config import GLOBAL_VAULT_ID
from memex_core.memory.sql_models import Entity, EntityCooccurrence, UnitEntity, EntityAlias
from memex_core.memory.utils import normalize_name, calculate_temporal_score, get_phonetic_code


class EntityInput(BaseModel):
    """Represents a raw entity extracted from text."""

    index: int
    indices: list[int] = []  # All indices in the batch that match this entity
    text: str
    event_date: datetime
    nearby_entity_names: set[str]


class EntityCandidate(BaseModel):
    """Represents a potential database match for an input."""

    id: str  # UUID as string
    canonical_name: str
    last_seen: datetime | None = None
    name_similarity_score: float
    phonetic_match: bool = False


class ResolutionResult(BaseModel):
    """The decision made for a specific input."""

    input_indices: list[int]
    entity_id: str | None = None
    is_new: bool = False
    input_data: EntityInput


def calculate_match_score(
    candidate: EntityCandidate,
    input_date: datetime,
    input_nearby_names: set[str],
    known_neighbors: dict[str, int],
) -> float:
    """
    Calculate confidence score (0.0 - 1.0) for a candidate match.

    Formula:
    - 50% Name Similarity (Trigram + Phonetic Boost)
    - 30% Context/Co-occurrence (Frequency-Weighted)
    - 20% Temporal Proximity (Exponential Decay)
    """
    # 1. Name Score (0.5)
    name_score = candidate.name_similarity_score

    # If we have a phonetic match but low trigram similarity, give a floor boost
    if candidate.phonetic_match and name_score < 0.5:
        name_score = 0.5

    score_name = name_score * 0.5

    # 2. Co-occurrence Score (0.3)
    score_co = 0.0
    if input_nearby_names and known_neighbors:
        # Intersection of names appearing in this text vs names known to appear with candidate
        common = input_nearby_names & set(known_neighbors.keys())
        if common:
            # TF-IDF style weighting: Match on rare neighbors is worth more.
            # Weight = 1.0 / log2(2 + mention_count)
            matched_weight = 0.0
            for name in common:
                freq = known_neighbors[name]
                # Log scale ensures rare neighbors have much higher impact than common ones
                weight = 1.0 / math.log2(2 + freq)
                matched_weight += weight

            # Normalize by input size. Max score per match is 1.0 (if freq=0)
            score_co = (matched_weight / len(input_nearby_names)) * 0.3
            score_co = min(score_co, 0.3)

    # 3. Temporal Score (0.2)
    score_temp = 0.0
    if candidate.last_seen and input_date:
        # Use exponential half-life decay (30-day default)
        score_temp = calculate_temporal_score(candidate.last_seen, input_date) * 0.2

    return score_name + score_co + score_temp


class EntityResolver:
    """Resolves entities to canonical IDs with disambiguation."""

    def __init__(self, resolution_threshold: float = 0.65):
        self.resolution_threshold = resolution_threshold
        self._logger = logging.getLogger('memex_core.memory.entity_resolver')

    async def resolve_entities_batch(
        self,
        session: AsyncSession,
        entities_data: list[dict[str, Any]],
        default_event_date: datetime,
    ) -> list[str]:
        """
        Main Entry Point: Resolve a batch of raw entities.
        Returns a list of UUID strings in the same order as input.
        """
        if not entities_data:
            return []

        inputs = self._prepare_inputs(entities_data, default_event_date)

        candidates_map = await self._fetch_candidates(session, inputs)

        all_candidate_ids = {c.id for c_list in candidates_map.values() for c in c_list}
        neighbor_map = await self._fetch_neighbor_map(session, list(all_candidate_ids))

        resolutions = self._decide_resolutions(inputs, candidates_map, neighbor_map)

        final_ids = await self._persist_resolutions(session, resolutions)

        return final_ids

    def _prepare_inputs(
        self, entities_data: list[dict[str, Any]], default_date: datetime
    ) -> list[EntityInput]:
        """Convert raw dicts to typed Input objects with intra-batch deduplication."""
        # Key: normalized_name
        grouped: dict[str, EntityInput] = {}

        for idx, data in enumerate(entities_data):
            raw_text = data.get('text', '')
            norm_name = normalize_name(raw_text)

            key = norm_name

            nearby = {
                normalize_name(ne['text'])
                for ne in data.get('nearby_entities', [])
                if normalize_name(ne['text']) != norm_name
            }

            evt_date = data.get('event_date', default_date)
            if evt_date.tzinfo is None:
                evt_date = evt_date.replace(tzinfo=timezone.utc)

            if key in grouped:
                existing = grouped[key]
                existing.indices.append(idx)
                existing.nearby_entity_names.update(nearby)
            else:
                grouped[key] = EntityInput(
                    index=len(grouped),  # Local index within the deduplicated list
                    indices=[idx],
                    text=raw_text,
                    event_date=evt_date,
                    nearby_entity_names=nearby,
                )

        return list(grouped.values())

    async def _fetch_candidates(
        self, session: AsyncSession, inputs: list[EntityInput]
    ) -> dict[int, list[EntityCandidate]]:
        """
        Find top candidates for each input text using Trigram similarity
        (Canonical + Aliases) and Phonetic matching.
        Returns: Dict[InputIndex, List[Candidate]]
        """
        if not inputs:
            return {}

        # Ensure trigram limit is reasonable
        await session.exec(text('SELECT set_limit(0.3);'))

        # Prepare input arrays for SQL
        indices = [i.index for i in inputs]
        texts = [i.text for i in inputs]
        phonetics = [get_phonetic_code(i.text) for i in inputs]

        # Use a CTE to join inputs with candidate matches across Entities and Aliases
        query = text("""
            WITH inputs AS (
                SELECT
                    unnest(:indices :: int[]) as idx,
                    unnest(:texts :: text[]) as raw_text,
                    unnest(:phonetics :: text[]) as input_phonetic
            )
            SELECT
                i.idx,
                e.id,
                e.canonical_name,
                e.last_seen,
                e.score as name_score,
                e.is_phonetic
            FROM inputs i
            CROSS JOIN LATERAL (
                -- 1. Trigram matches on Entities
                SELECT
                    id, canonical_name, last_seen,
                    similarity(lower(canonical_name), lower(i.raw_text)) as score,
                    false as is_phonetic
                FROM entities
                WHERE lower(canonical_name) % lower(i.raw_text)

                UNION ALL

                -- 2. Trigram matches on Aliases
                SELECT
                    ent.id, ent.canonical_name, ent.last_seen,
                    similarity(lower(als.name), lower(i.raw_text)) as score,
                    false as is_phonetic
                FROM entity_aliases als
                JOIN entities ent ON als.canonical_id = ent.id
                WHERE lower(als.name) % lower(i.raw_text)

                UNION ALL

                -- 3. Phonetic matches on Entities
                SELECT
                    id, canonical_name, last_seen,
                    0.45 as score, -- Base score for phonetic match
                    true as is_phonetic
                FROM entities
                WHERE phonetic_code IS NOT NULL
                  AND phonetic_code = i.input_phonetic

                ORDER BY score DESC
                LIMIT 5
            ) e
            """)

        result = await session.exec(
            query,
            params={'indices': indices, 'texts': texts, 'phonetics': phonetics},
        )

        candidates_map: dict[int, list[EntityCandidate]] = {i: [] for i in indices}
        for row in result:
            # row: (idx, id, canonical_name, last_seen, name_score, is_phonetic)
            cand = EntityCandidate(
                id=str(row[1]),
                canonical_name=row[2],
                last_seen=row[3],
                name_similarity_score=row[4],
                phonetic_match=row[5],
            )
            candidates_map[row[0]].append(cand)

        return candidates_map

    async def _fetch_neighbor_map(
        self, session: AsyncSession, candidate_ids: list[str], limit_per_entity: int = 50
    ) -> dict[str, dict[str, int]]:
        if not candidate_ids:
            return {}

        # Using a raw SQL query with ROW_NUMBER() to limit neighbors per candidate
        # while still allowing a single batch fetch for performance.
        query = text("""
            WITH combined AS (
                -- Neighbors where candidate is entity_1
                SELECT
                    entity_id_1 as source_id,
                    entity_id_2 as neighbor_id,
                    cooccurrence_count
                FROM entity_cooccurrences
                WHERE entity_id_1 = ANY(:candidate_ids :: uuid[])

                UNION ALL

                -- Neighbors where candidate is entity_2
                SELECT
                    entity_id_2 as source_id,
                    entity_id_1 as neighbor_id,
                    cooccurrence_count
                FROM entity_cooccurrences
                WHERE entity_id_2 = ANY(:candidate_ids :: uuid[])
            ),
            ranked AS (
                SELECT
                    source_id,
                    neighbor_id,
                    cooccurrence_count,
                    ROW_NUMBER() OVER(PARTITION BY source_id ORDER BY cooccurrence_count DESC) as rank
                FROM combined
            )
            SELECT
                r.source_id,
                e.canonical_name,
                e.mention_count
            FROM ranked r
            JOIN entities e ON r.neighbor_id = e.id
            WHERE r.rank <= :limit
        """)

        result = await session.exec(
            query, params={'candidate_ids': candidate_ids, 'limit': limit_per_entity}
        )

        # Map: candidate_id -> {neighbor_name: neighbor_mention_count}
        neighbor_map: dict[str, dict[str, int]] = defaultdict(dict)
        for row in result:
            # row: (source_id, neighbor_name, mention_count)
            neighbor_map[str(row[0])][row[1].lower()] = row[2]

        return neighbor_map

    def _decide_resolutions(
        self,
        inputs: list[EntityInput],
        candidates_map: dict[int, list[EntityCandidate]],
        neighbor_map: dict[str, dict[str, int]],
    ) -> list[ResolutionResult]:
        """Pure logic: iterate inputs and decide to Link or Create."""
        results = []

        for inp in inputs:
            candidates = candidates_map.get(inp.index, [])

            best_id = None
            best_score = 0.0

            for cand in candidates:
                known_neighbors: dict[str, int] = neighbor_map.get(cand.id, {})

                score = calculate_match_score(
                    cand, inp.event_date, inp.nearby_entity_names, known_neighbors
                )

                if score > best_score:
                    best_score = score
                    best_id = cand.id

            if best_id and best_score >= self.resolution_threshold:
                # Match Found
                results.append(
                    ResolutionResult(input_indices=inp.indices, entity_id=best_id, input_data=inp)
                )
            else:
                # No Match -> Create New
                results.append(
                    ResolutionResult(
                        input_indices=inp.indices,
                        input_data=inp,
                        is_new=True,
                    )
                )

        return results

    async def _persist_resolutions(
        self, session: AsyncSession, resolutions: list[ResolutionResult]
    ) -> list[str]:
        """
        Updates existing entities and creates new ones in batch.
        Also records aliases for matched entities.
        Returns the list of Entity IDs (strings) in the exact order of the input resolutions.
        """
        current_time = datetime.now(timezone.utc)
        final_ids_map: dict[int, str] = {}

        update_ids: set[str] = set()
        alias_values = []

        for res in resolutions:
            if not res.is_new and res.entity_id:
                # Map all original indices to this ID
                str_id = str(res.entity_id)
                for idx in res.input_indices:
                    final_ids_map[idx] = str_id

                update_ids.add(res.entity_id)

                # Prepare alias recording
                alias_values.append(
                    {
                        'canonical_id': res.entity_id,
                        'name': res.input_data.text,
                        'phonetic_code': get_phonetic_code(res.input_data.text),
                    }
                )

        if update_ids:
            stmt = (
                update(Entity)
                .where(col(Entity.id).in_(update_ids))
                .values(mention_count=Entity.mention_count + 1, last_seen=current_time)
            )
            await session.exec(stmt)

        if alias_values:
            # Record alias (if it doesn't already exist)
            alias_stmt = (
                pg_insert(EntityAlias)
                .values(alias_values)
                .on_conflict_do_nothing(index_elements=['canonical_id', 'name'])
            )
            await session.exec(alias_stmt)

        # Group creations by normalized name
        creates_groups: dict[str, dict[str, Any]] = defaultdict(lambda: {'indices': []})

        for res in resolutions:
            if res.is_new:
                # Normalize name for grouping
                key = normalize_name(res.input_data.text)
                group = creates_groups[key]

                # Store data from the first occurrence
                if not group.get('data'):
                    group['data'] = res.input_data

                group['indices'].extend(res.input_indices)

        if creates_groups:
            # 2. Prepare raw dictionaries for SQLAlchemy Insert
            insert_values = []
            for group in creates_groups.values():
                data = group['data']
                insert_values.append(
                    {
                        'canonical_name': data.text,
                        'phonetic_code': get_phonetic_code(data.text),
                        'first_seen': data.event_date or current_time,
                        'last_seen': data.event_date or current_time,
                        'mention_count': len(group['indices']),
                    }
                )

            insert_stmt = pg_insert(Entity).values(insert_values)

            # Handle Edge Case: "New" entity actually exists in DB
            upsert_stmt = insert_stmt.on_conflict_do_update(
                index_elements=['canonical_name'],
                set_={
                    'mention_count': Entity.mention_count + insert_stmt.excluded.mention_count,
                    'last_seen': insert_stmt.excluded.last_seen,
                },
            ).returning(Entity.id, Entity.canonical_name)

            # 4. Execute and Map back
            result = await session.exec(upsert_stmt)

            for row_id, row_name in result.all():
                # Map the returned DB ID back to all input indices that matched this name
                key = normalize_name(row_name)
                if key in creates_groups:
                    str_id = str(row_id)
                    for idx in creates_groups[key]['indices']:
                        final_ids_map[idx] = str_id

        # Reassemble in original order
        total_len = sum(len(res.input_indices) for res in resolutions)
        return [final_ids_map[i] for i in range(total_len)]

    async def link_units_to_entities_batch(
        self,
        session: AsyncSession,
        unit_entity_pairs: list[tuple[str, str]],
        vault_id: PyUUID = GLOBAL_VAULT_ID,
    ):
        """
        1. Create Unit-Entity links (Bulk Insert).
        2. Update Entity-Entity co-occurrences (Aggregated Upsert).
        """
        if not unit_entity_pairs:
            return

        # Bulk link Unit <-> Entity
        links_data = [
            {'unit_id': u, 'entity_id': e, 'vault_id': vault_id} for u, e in unit_entity_pairs
        ]

        link_stmt = pg_insert(UnitEntity).values(links_data).on_conflict_do_nothing()
        await session.exec(link_stmt)

        # Bulk co-occurrence Entity <-> Entity
        unit_to_entities = defaultdict(set)
        for uid, eid in unit_entity_pairs:
            unit_to_entities[uid].add(eid)

        # Aggregate counts in memory before hitting DB
        # Key: (entity_id_1, entity_id_2), Value: count increment
        pair_counts: dict[tuple[str, str], int] = defaultdict(int)

        for entities in unit_to_entities.values():
            if len(entities) < 2:
                continue

            # Sort to enforce canonical ordering (uuid_1 < uuid_2)
            sorted_entities = sorted(list(entities))

            for e1, e2 in itertools.combinations(sorted_entities, 2):
                pair_counts[(e1, e2)] += 1

        if not pair_counts:
            return

        now = datetime.now(timezone.utc)
        co_pairs_data = [
            {
                'entity_id_1': e1,
                'entity_id_2': e2,
                'vault_id': vault_id,
                'cooccurrence_count': count,  # Batch total
                'last_cooccurred': now,
            }
            for (e1, e2), count in pair_counts.items()
        ]

        # Upsert: Add the batch count to the existing DB count
        stmt = pg_insert(EntityCooccurrence).values(co_pairs_data)
        stmt = stmt.on_conflict_do_update(
            index_elements=['entity_id_1', 'entity_id_2'],
            set_={
                # Magic: DB Count + Batch Count
                'cooccurrence_count': EntityCooccurrence.cooccurrence_count
                + stmt.excluded.cooccurrence_count,
                'last_cooccurred': stmt.excluded.last_cooccurred,
            },
        )
        await session.exec(stmt)

    async def get_units_by_entity(
        self, session: AsyncSession, entity_id: str | PyUUID, limit: int = 100
    ) -> list[str]:
        """
        Get all units that mention an entity.

        Optimized to fetch only the UUIDs, avoiding full object overhead.
        """
        e_uuid = PyUUID(str(entity_id))

        stmt = select(UnitEntity.unit_id).where(UnitEntity.entity_id == e_uuid).limit(limit)
        result = await session.exec(stmt)
        return [str(uid) for uid in result.all()]

    async def get_entity_by_text(
        self,
        session: AsyncSession,
        entity_text: str,
    ) -> str | None:
        """
        Find an entity by text (case-insensitive).

        Returns the most frequently mentioned entity if there are duplicates/ambiguities.
        """
        stmt = (
            select(Entity.id)
            # Use col() to satisfy type checkers for the ilike operator
            .where(col(Entity.canonical_name).ilike(entity_text))
            .order_by(desc(Entity.mention_count))
            .limit(1)
        )

        result = await session.exec(stmt)
        entity_id = result.first()

        return str(entity_id) if entity_id else None
