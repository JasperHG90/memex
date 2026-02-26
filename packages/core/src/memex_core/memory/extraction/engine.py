import math
import asyncio
import logging
from collections import defaultdict
from datetime import timedelta, datetime, timezone
from typing import Any
from uuid import UUID

import dspy
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert

import memex_core.config
from memex_core.config import (
    ExtractionConfig,
    ReflectionConfig,
    ConfidenceConfig,
    PageIndexTextSplitting,
    SimpleTextSplitting,
    GLOBAL_VAULT_ID,
)
from memex_common.config import CHARS_PER_TOKEN
from memex_core.memory.extraction.models import (
    RetainContent,
    ExtractedFact,
    ChunkMetadata,
    ProcessedFact,
    StableBlock,
    TOCNode,
    PageIndexOutput,
    content_hash_md5,
)
from memex_core.memory.extraction.core import (
    extract_facts_from_text,
    extract_facts_from_chunks,
    _convert_causal_relations,
    ExtractSemanticFacts,
    stable_chunk_text,
    content_hash,
    index_document,
)
from memex_core.memory.models.embedding import get_embedding_model
from memex_core.memory.extraction.utils import parse_datetime, normalize_timestamp
from memex_core.memory.extraction import storage, embedding_processor, deduplication
from memex_core.memory.sql_models import ContentStatus
from memex_core.memory.entity_resolver import EntityResolver
from memex_core.memory.confidence import ConfidenceEngine
from memex_core.memory.sql_models import MemoryLink, TokenUsage
from memex_core.memory.reflect.queue_service import ReflectionQueueService
from memex_core.context import get_session_id
from memex_core.processing.titles import resolve_title_from_page_index

logger = logging.getLogger('memex.core.memory.extraction.engine')


def _make_lm(model_cfg: 'memex_core.config.ModelConfig') -> dspy.LM:
    """Create a DSPy LM from a ModelConfig."""
    return dspy.LM(
        model=model_cfg.model,
        api_base=str(model_cfg.base_url) if model_cfg.base_url else None,
        api_key=model_cfg.api_key.get_secret_value() if model_cfg.api_key else None,
        reasoning_effort=model_cfg.reasoning_effort.value
        if model_cfg.reasoning_effort is not None
        else None,
    )


async def get_extraction_engine(
    config: ExtractionConfig,
    confidence_config: ConfidenceConfig,
    reflection_config: ReflectionConfig | None = None,
):
    """
    Factory method to create an ExtractionEngine with dependencies.
    """
    assert config.model is not None, 'extraction.model must be set (via default_model propagation)'
    lm = _make_lm(config.model)
    predictor = dspy.Predict(ExtractSemanticFacts)
    embedding_model = await get_embedding_model()
    entity_resolver = EntityResolver(resolution_threshold=0.65)

    # Create a separate LM for PageIndex if using a different model
    page_index_lm: dspy.LM | None = None
    if config.active_strategy == 'page_index':
        pi_cfg = config.text_splitting
        assert isinstance(pi_cfg, PageIndexTextSplitting)
        pi_model_cfg = pi_cfg.model
        if pi_model_cfg is None or pi_model_cfg.model == config.model.model:
            page_index_lm = lm
        else:
            page_index_lm = _make_lm(pi_model_cfg)

    return ExtractionEngine(
        config=config,
        confidence_config=confidence_config,
        lm=lm,
        predictor=predictor,
        embedding_model=embedding_model,
        entity_resolver=entity_resolver,
        reflection_config=reflection_config,
        page_index_lm=page_index_lm,
    )


class ExtractionEngine:
    """
    Orchestrates the extraction, embedding, resolution, and persistence of memory.
    """

    SECONDS_PER_FACT = 10

    def __init__(
        self,
        config: ExtractionConfig,
        confidence_config: ConfidenceConfig,
        lm: dspy.LM,
        predictor: dspy.Predict,
        embedding_model: embedding_processor.EmbeddingsModel,
        entity_resolver: EntityResolver,
        reflection_config: ReflectionConfig | None = None,
        page_index_lm: dspy.LM | None = None,
    ):
        self.config = config
        self.lm = lm
        self.predictor = predictor
        self.embedding_model = embedding_model
        self.entity_resolver = entity_resolver
        self.confidence_engine = ConfidenceEngine(
            damping_factor=confidence_config.damping_factor,
            max_inherited_mass=confidence_config.max_inherited_mass,
            similarity_threshold=confidence_config.similarity_threshold,
        )
        self.queue_service = (
            ReflectionQueueService(config=reflection_config) if reflection_config else None
        )
        self.semaphore = asyncio.Semaphore(config.max_concurrency)
        self.page_index_lm = page_index_lm

    async def extract_and_persist(
        self,
        session: AsyncSession,
        contents: list[RetainContent],
        agent_name: str = 'memex_agent',
        note_id: str | None = None,
        is_first_batch: bool = True,
        extract_opinions: bool = False,
        content_fingerprint: str | None = None,
    ) -> tuple[list[str], TokenUsage, set[UUID]]:
        """
        Main entry point: Extract facts from content and persist them to memory.
        Returns (unit_ids, usage, touched_entity_ids).

        When ``note_id`` is provided and the document already has blocks in
        the DB, the incremental path is used: only changed blocks trigger LLM
        extraction.
        """
        if not contents:
            return [], TokenUsage(), set()

        # Determine vault_id (assuming uniform per batch)
        vault_id = contents[0].vault_id if contents else GLOBAL_VAULT_ID

        # Check if incremental path is viable
        if note_id and is_first_batch:
            existing_blocks = await storage.get_note_blocks(session, note_id)
            if existing_blocks:
                if self.config.active_strategy == 'page_index' and self.page_index_lm is not None:
                    return await self._extract_page_index_incremental(
                        session=session,
                        contents=contents,
                        agent_name=agent_name,
                        note_id=note_id,
                        existing_blocks=existing_blocks,
                        vault_id=vault_id,
                        extract_opinions=extract_opinions,
                        content_fingerprint=content_fingerprint,
                    )
                return await self._extract_incremental(
                    session=session,
                    contents=contents,
                    agent_name=agent_name,
                    note_id=note_id,
                    existing_blocks=existing_blocks,
                    vault_id=vault_id,
                    extract_opinions=extract_opinions,
                    content_fingerprint=content_fingerprint,
                )

        # --- Strategy dispatch ---
        if self.config.active_strategy == 'page_index' and self.page_index_lm is not None:
            return await self._extract_page_index(
                session=session,
                contents=contents,
                agent_name=agent_name,
                note_id=note_id,
                is_first_batch=is_first_batch,
                vault_id=vault_id,
                extract_opinions=extract_opinions,
                content_fingerprint=content_fingerprint,
            )

        # --- Full extraction path (simple strategy or no page_index LM) ---
        extracted_facts, chunks, usage = await self._extract_facts(
            contents, agent_name, extract_opinions
        )

        if chunks:
            chunk_texts = [c.chunk_text for c in chunks]
            chunk_embeddings = await embedding_processor.generate_embeddings_batch(
                self.embedding_model, chunk_texts
            )
            for chunk, emb in zip(chunks, chunk_embeddings):
                chunk.embedding = emb

        if not extracted_facts:
            if note_id:
                await self._track_document(
                    session, note_id, contents, is_first_batch, vault_id=vault_id
                )
            return [], usage, set()

        processed_facts = await self._process_embeddings(extracted_facts)

        # NB: Initialize informative priors for opinions
        for fact in processed_facts:
            if fact.fact_type == 'opinion':
                alpha, beta = await self.confidence_engine.calculate_informative_prior(
                    session, fact.embedding
                )
                fact.confidence_alpha = alpha
                fact.confidence_beta = beta

        # Deduplication
        is_duplicate = await deduplication.check_duplicates_batch(
            session, processed_facts, storage.check_duplicates_in_window, vault_id=vault_id
        )

        # Filter duplicates from both lists to maintain parallelism
        # We need to filter extracted_facts too because they are used for chunk linking logic below
        final_processed_facts = []
        final_extracted_facts = []

        for i, is_dup in enumerate(is_duplicate):
            if not is_dup:
                final_processed_facts.append(processed_facts[i])
                final_extracted_facts.append(extracted_facts[i])

        if not final_processed_facts:
            logger.info(f'All {len(processed_facts)} facts were duplicates.')
            if note_id:
                await self._track_document(
                    session, note_id, contents, is_first_batch, vault_id=vault_id
                )
            return [], usage, set()

        extracted_facts = final_extracted_facts
        processed_facts = final_processed_facts

        effective_doc_id = note_id
        if chunks and not effective_doc_id:
            logger.warning('Chunks present but no note_id provided. Chunk linking may be partial.')
            effective_doc_id = str(UUID(int=0))

        if effective_doc_id:
            await self._track_document(
                session, effective_doc_id, contents, is_first_batch, vault_id=vault_id
            )
            chunk_map = await self._store_chunks(
                session, effective_doc_id, chunks, vault_id=vault_id
            )

            # Link facts to chunks using the parallel lists
            for ef, pf in zip(extracted_facts, processed_facts):
                pf.note_id = effective_doc_id
                if ef.chunk_index is not None and ef.chunk_index in chunk_map:
                    pf.chunk_id = chunk_map[ef.chunk_index]

        unit_ids = await storage.insert_facts_batch(
            session, processed_facts, note_id=effective_doc_id
        )

        touched_entity_ids = await self._resolve_entities(
            session, unit_ids, processed_facts, vault_id=vault_id
        )
        await self._create_links(session, unit_ids, processed_facts, vault_id=vault_id)

        # Update Reflection Queue Priorities
        if self.queue_service:
            await self.queue_service.handle_extraction_event(
                session, touched_entity_ids, vault_id=vault_id
            )

        # Log aggregated token usage
        usage.session_id = get_session_id()
        usage.vault_id = vault_id
        usage.context_metadata = {
            'operation': 'extract',
            'mode': 'full',
            'note_id': note_id,
            'unit_count': len(unit_ids),
            'vault_id': str(vault_id),
        }
        session.add(usage)

        return unit_ids, usage, touched_entity_ids

    async def _extract_incremental(
        self,
        session: AsyncSession,
        contents: list[RetainContent],
        agent_name: str,
        note_id: str,
        existing_blocks: list[dict[str, object]],
        vault_id: UUID,
        extract_opinions: bool = False,
        content_fingerprint: str | None = None,
    ) -> tuple[list[str], TokenUsage, set[UUID]]:
        """Incremental extraction: diff blocks, extract only changed content.

        Args:
            session: Active DB session.
            contents: Content items to extract from.
            agent_name: Agent performing extraction.
            note_id: Stable document identifier.
            existing_blocks: Current blocks from DB (via get_note_blocks).
            vault_id: Vault scope.
            extract_opinions: Whether to extract opinions.
            content_fingerprint: Content fingerprint for document tracking.

        Returns:
            Tuple of (unit_ids, usage, touched_entity_ids).
        """
        combined_text = '\n'.join(c.content for c in contents)
        event_date = contents[0].event_date if contents else None

        # 1. Generate new blocks using stable chunking
        ts = self.config.text_splitting
        block_size = (
            ts.chunk_size_tokens * CHARS_PER_TOKEN if isinstance(ts, SimpleTextSplitting) else 4000
        )
        new_blocks = stable_chunk_text(combined_text, block_size=block_size)

        # 2. Build lookup of existing block hashes -> block metadata
        existing_hash_map: dict[str, dict[str, object]] = {}
        for existing_block in existing_blocks:
            h = str(existing_block['content_hash'])
            existing_hash_map[h] = existing_block

        new_hash_set = {b.content_hash for b in new_blocks}
        existing_hash_set = set(existing_hash_map.keys())

        # 3. Classify blocks
        retained_hashes = new_hash_set & existing_hash_set
        added_blocks = [b for b in new_blocks if b.content_hash not in existing_hash_set]
        removed_hashes = existing_hash_set - new_hash_set

        logger.info(
            f'Incremental diff for document {note_id}: '
            f'blocks_total={len(new_blocks)} retained={len(retained_hashes)} '
            f'added={len(added_blocks)} removed={len(removed_hashes)}'
        )

        # 4. Extract facts from ADDED blocks only
        usage = TokenUsage()
        unit_ids: list[str] = []
        touched_entity_ids: set[UUID] = set()

        if added_blocks:
            # Assemble ADDED blocks into LLM chunks with ±1 RETAINED neighbor context
            llm_chunks = self._assemble_llm_chunks(new_blocks, added_blocks, retained_hashes)

            # Extract facts from assembled chunks
            chunk_texts = [c['text'] for c in llm_chunks]
            contexts = [c.get('context', '') for c in llm_chunks]

            # Extract from each chunk with its context
            all_extracted_facts: list[ExtractedFact] = []
            all_chunk_metadata: list[ChunkMetadata] = []
            total_usage = TokenUsage()

            for chunk_idx, (chunk_text_val, ctx) in enumerate(zip(chunk_texts, contexts)):
                raw_facts, chunk_meta, chunk_usage = await extract_facts_from_chunks(
                    chunks=[chunk_text_val],
                    event_date=event_date or contents[0].event_date,
                    lm=self.lm,
                    predictor=self.predictor,
                    agent_name=agent_name,
                    context=ctx,
                    extract_opinions=extract_opinions,
                    semaphore=self.semaphore,
                    vault_id=vault_id,
                )
                total_usage += chunk_usage

                for fact_text, fact_count in chunk_meta:
                    all_chunk_metadata.append(
                        ChunkMetadata(
                            chunk_text=fact_text,
                            fact_count=fact_count,
                            content_index=0,
                            chunk_index=chunk_idx,
                            content_hash=llm_chunks[chunk_idx].get('content_hash', ''),
                        )
                    )

                fact_start_idx = len(all_extracted_facts)
                for raw_fact in raw_facts:
                    ef = ExtractedFact(
                        fact_text=raw_fact.formatted_text,
                        fact_type=raw_fact.fact_type,
                        entities=raw_fact.entities,
                        occurred_start=None,
                        occurred_end=None,
                        causal_relations=_convert_causal_relations(
                            relations_from_llm=raw_fact.causal_relations,
                            fact_start_idx=fact_start_idx,
                        ),
                        content_index=0,
                        chunk_index=chunk_idx,
                        context=ctx,
                        mentioned_at=event_date or contents[0].event_date,
                        payload=contents[0].payload or {},
                        where=raw_fact.where,
                        vault_id=vault_id,
                    )
                    all_extracted_facts.append(ef)

            usage = total_usage

            if all_extracted_facts:
                self._add_temporal_offsets(all_extracted_facts)
                processed_facts = await self._process_embeddings(all_extracted_facts)

                for fact in processed_facts:
                    if fact.fact_type == 'opinion':
                        alpha, beta = await self.confidence_engine.calculate_informative_prior(
                            session, fact.embedding
                        )
                        fact.confidence_alpha = alpha
                        fact.confidence_beta = beta

                # Deduplication
                is_duplicate = await deduplication.check_duplicates_batch(
                    session, processed_facts, storage.check_duplicates_in_window, vault_id=vault_id
                )

                final_processed = [
                    pf for pf, is_dup in zip(processed_facts, is_duplicate) if not is_dup
                ]
                final_extracted = [
                    ef for ef, is_dup in zip(all_extracted_facts, is_duplicate) if not is_dup
                ]

                if final_processed:
                    # Generate chunk embeddings for ADDED blocks
                    if all_chunk_metadata:
                        emb_texts = [c.chunk_text for c in all_chunk_metadata]
                        chunk_embeddings = await embedding_processor.generate_embeddings_batch(
                            self.embedding_model, emb_texts
                        )
                        for cm, emb in zip(all_chunk_metadata, chunk_embeddings):
                            cm.embedding = emb

                    # Store new chunks for ADDED blocks
                    chunk_map = await self._store_chunks(
                        session, note_id, all_chunk_metadata, vault_id=vault_id
                    )

                    for ef, pf in zip(final_extracted, final_processed):
                        pf.note_id = note_id
                        if ef.chunk_index is not None and ef.chunk_index in chunk_map:
                            pf.chunk_id = chunk_map[ef.chunk_index]

                    unit_ids = await storage.insert_facts_batch(
                        session, final_processed, note_id=note_id
                    )

                    touched_entity_ids = await self._resolve_entities(
                        session, unit_ids, final_processed, vault_id=vault_id
                    )
                    await self._create_links(session, unit_ids, final_processed, vault_id=vault_id)

        # 5. Reconciliation (single TX scope — caller manages commit)
        # RETAINED: update chunk_index
        retained_updates: list[tuple[UUID, int]] = []
        for block in new_blocks:
            if block.content_hash in retained_hashes:
                existing = existing_hash_map[block.content_hash]
                retained_updates.append(
                    (
                        existing['id'],  # type: ignore[arg-type]
                        block.block_index,
                    )
                )
        await storage.reindex_blocks(session, retained_updates)

        # REMOVED: mark stale
        removed_block_ids: list[UUID] = [
            UUID(str(existing_hash_map[h]['id'])) for h in removed_hashes
        ]
        await storage.mark_blocks_stale(session, removed_block_ids)
        await storage.mark_memory_units_stale(session, removed_block_ids)

        # Update document tracking
        await self._track_document(
            session, note_id, contents, is_first_batch=False, vault_id=vault_id
        )

        # Update Reflection Queue
        if self.queue_service and touched_entity_ids:
            await self.queue_service.handle_extraction_event(
                session, touched_entity_ids, vault_id=vault_id
            )

        # Log usage with incremental metadata
        usage.session_id = get_session_id()
        usage.vault_id = vault_id
        usage.context_metadata = {
            'operation': 'extract',
            'mode': 'incremental',
            'note_id': note_id,
            'blocks_total': len(new_blocks),
            'blocks_retained': len(retained_hashes),
            'blocks_added': len(added_blocks),
            'blocks_removed': len(removed_hashes),
            'unit_count': len(unit_ids),
            'vault_id': str(vault_id),
        }
        session.add(usage)

        return unit_ids, usage, touched_entity_ids

    async def _extract_page_index_incremental(
        self,
        session: AsyncSession,
        contents: list[RetainContent],
        agent_name: str,
        note_id: str,
        existing_blocks: list[dict[str, object]],
        vault_id: UUID,
        extract_opinions: bool = False,
        content_fingerprint: str | None = None,
    ) -> tuple[list[str], TokenUsage, set[UUID]]:
        """Incremental page-index extraction with node-level change detection.

        Diffs new PageIndex blocks against existing blocks:
        - RETAINED: hash match -> skip entirely (reindex only)
        - BOUNDARY_SHIFT: new hash but all constituent nodes existed before ->
          re-embed but skip HindSight (facts migrated from old chunks)
        - CONTENT_CHANGED: new hash with new/changed nodes -> full extraction
        - REMOVED: old hash absent from new tree -> stale

        Args:
            session: Active DB session.
            contents: Content items to extract from.
            agent_name: Agent performing extraction.
            note_id: Stable document identifier.
            existing_blocks: Current blocks from DB (via get_note_blocks).
            vault_id: Vault scope.
            extract_opinions: Whether to extract opinions.
            content_fingerprint: Content fingerprint for document tracking.

        Returns:
            Tuple of (unit_ids, usage, touched_entity_ids).
        """
        combined_text = '\n'.join(c.content for c in contents)
        event_date = contents[0].event_date if contents else None

        ts = self.config.text_splitting
        assert isinstance(ts, PageIndexTextSplitting)
        assert self.page_index_lm is not None

        # 1. Run PageIndex
        page_index_output, pi_usage = await index_document(
            full_text=combined_text,
            lm=self.page_index_lm,
            scan_chunk_size=ts.scan_chunk_size_tokens * CHARS_PER_TOKEN,
            max_node_length=ts.max_node_length_tokens * CHARS_PER_TOKEN,
            block_token_target=ts.block_token_target,
            short_doc_threshold=ts.short_doc_threshold_tokens * CHARS_PER_TOKEN,
        )

        logger.info(
            f'PageIndex incremental: path={page_index_output.path_used}, '
            f'blocks={len(page_index_output.blocks)}, '
            f'coverage={page_index_output.coverage_ratio:.1%}'
        )

        # 2. Block-level diff
        existing_hash_map: dict[str, dict[str, object]] = {
            str(b['content_hash']): b for b in existing_blocks
        }
        existing_hash_set = set(existing_hash_map.keys())
        new_hash_set = {b.id for b in page_index_output.blocks}

        retained_hashes = new_hash_set & existing_hash_set
        new_block_hashes = new_hash_set - existing_hash_set
        removed_hashes = existing_hash_set - new_hash_set

        # 3. Node-level change detection for new blocks
        prev_nodes = await storage.get_note_nodes(session, note_id)
        prev_node_hash_set = {str(n['node_hash']) for n in prev_nodes}

        # Build block_hash -> constituent node hashes from new PageIndexOutput
        block_node_hashes: dict[str, set[str]] = defaultdict(set)

        def _walk_nodes(nodes: list[TOCNode]) -> None:
            for n in nodes:
                if n.content_hash:
                    block_hash = page_index_output.node_to_block_map.get(n.id)
                    if block_hash:
                        block_node_hashes[block_hash].add(n.content_hash)
                _walk_nodes(n.children)

        _walk_nodes(page_index_output.toc)

        # Classify new blocks
        boundary_shift_hashes: set[str] = set()
        content_changed_hashes: set[str] = set()
        for block in page_index_output.blocks:
            if block.id not in new_block_hashes:
                continue  # retained
            node_hashes = block_node_hashes.get(block.id, set())
            if node_hashes and node_hashes.issubset(prev_node_hash_set):
                boundary_shift_hashes.add(block.id)
            else:
                content_changed_hashes.add(block.id)

        logger.info(
            f'PageIndex incremental diff for {note_id}: '
            f'retained={len(retained_hashes)} boundary_shift={len(boundary_shift_hashes)} '
            f'content_changed={len(content_changed_hashes)} removed={len(removed_hashes)}'
        )

        # 4. Persist nodes (all) + blocks (non-retained only)
        min_tokens = ts.min_node_tokens
        node_ids, block_chunk_map = await self._persist_page_index_nodes_and_blocks(
            session=session,
            page_index_output=page_index_output,
            note_id=note_id,
            vault_id=vault_id,
            min_node_tokens=min_tokens,
            skip_block_hashes=retained_hashes,
        )

        # 5. Fact migration for boundary-shift blocks
        # Get old block -> node_hashes mapping from DB
        old_node_map = await storage.get_node_hashes_by_block(session, note_id)

        chunk_migration: dict[UUID, UUID] = {}  # old_chunk_id -> new_chunk_id
        for removed_hash in removed_hashes:
            old_chunk_id = UUID(str(existing_hash_map[removed_hash]['id']))
            old_nodes = old_node_map.get(old_chunk_id, set())
            if not old_nodes:
                continue
            # Find the new boundary-shift block with the most node overlap
            best_new_chunk_id: UUID | None = None
            best_overlap = 0
            for bs_hash in boundary_shift_hashes:
                new_nodes = block_node_hashes.get(bs_hash, set())
                overlap = len(old_nodes & new_nodes)
                if overlap > best_overlap:
                    best_overlap = overlap
                    block_seq = next(b.seq for b in page_index_output.blocks if b.id == bs_hash)
                    chunk_uuid_str = block_chunk_map.get(block_seq)
                    if chunk_uuid_str:
                        best_new_chunk_id = UUID(chunk_uuid_str)
            if best_new_chunk_id:
                chunk_migration[old_chunk_id] = best_new_chunk_id

        await storage.migrate_facts_to_chunks(session, chunk_migration)

        # 6. Stale removed blocks
        removed_block_ids = [UUID(str(existing_hash_map[h]['id'])) for h in removed_hashes]
        await storage.mark_blocks_stale(session, removed_block_ids)
        # Only stale facts for blocks whose facts were NOT migrated
        unmigrated_ids = [bid for bid in removed_block_ids if bid not in chunk_migration]
        await storage.mark_memory_units_stale(session, unmigrated_ids)

        # 7. Mark truly-removed nodes stale
        new_all_node_hashes: set[str] = set()
        for hashes in block_node_hashes.values():
            new_all_node_hashes |= hashes
        # Also include retained blocks' nodes
        for block in page_index_output.blocks:
            if block.id in retained_hashes:
                node_hashes_for_block = block_node_hashes.get(block.id, set())
                new_all_node_hashes |= node_hashes_for_block

        stale_node_ids = [
            n['id'] for n in prev_nodes if str(n['node_hash']) not in new_all_node_hashes
        ]
        await storage.mark_nodes_stale(session, stale_node_ids)

        # 8. Reindex retained blocks
        retained_updates: list[tuple[UUID, int]] = []
        for block in page_index_output.blocks:
            if block.id in retained_hashes:
                existing = existing_hash_map[block.id]
                retained_updates.append(
                    (
                        existing['id'],  # type: ignore[arg-type]
                        block.seq,
                    )
                )
        await storage.reindex_blocks(session, retained_updates)

        # 9. Extract facts ONLY for CONTENT_CHANGED blocks
        usage = pi_usage
        unit_ids: list[str] = []
        touched_entity_ids: set[UUID] = set()

        changed_blocks = [b for b in page_index_output.blocks if b.id in content_changed_hashes]
        if changed_blocks:
            block_texts = [b.content for b in changed_blocks]
            raw_facts, chunk_meta, extract_usage = await extract_facts_from_chunks(
                chunks=block_texts,
                event_date=event_date or contents[0].event_date,
                lm=self.lm,
                predictor=self.predictor,
                agent_name=agent_name,
                context='',
                extract_opinions=extract_opinions,
                semaphore=self.semaphore,
                vault_id=vault_id,
            )
            usage += extract_usage

            if raw_facts:
                # Convert to ExtractedFacts
                extracted_facts: list[ExtractedFact] = []
                global_fact_idx = 0
                facts_start_idx = 0

                for chunk_idx, (chunk_text_val, fact_count) in enumerate(chunk_meta):
                    chunk_facts = raw_facts[facts_start_idx : facts_start_idx + fact_count]
                    facts_start_idx += fact_count
                    # Map chunk_idx back to the block's seq for chunk linking
                    block_seq = changed_blocks[chunk_idx].seq

                    for f in chunk_facts:
                        ef = ExtractedFact(
                            fact_text=f.formatted_text,
                            fact_type=f.fact_type,
                            entities=f.entities,
                            occurred_start=parse_datetime(f.occurred_start)
                            if f.occurred_start
                            else None,
                            occurred_end=parse_datetime(f.occurred_end) if f.occurred_end else None,
                            causal_relations=_convert_causal_relations(
                                relations_from_llm=f.causal_relations,
                                fact_start_idx=global_fact_idx,
                            ),
                            content_index=0,
                            chunk_index=block_seq,
                            context='',
                            mentioned_at=event_date or contents[0].event_date,
                            payload=contents[0].payload or {},
                            where=f.where,
                            vault_id=vault_id,
                        )
                        extracted_facts.append(ef)
                        global_fact_idx += 1

                if extracted_facts:
                    self._add_temporal_offsets(extracted_facts)
                    processed_facts = await self._process_embeddings(extracted_facts)

                    for fact in processed_facts:
                        if fact.fact_type == 'opinion':
                            alpha, beta = await self.confidence_engine.calculate_informative_prior(
                                session, fact.embedding
                            )
                            fact.confidence_alpha = alpha
                            fact.confidence_beta = beta

                    # Deduplication
                    is_duplicate = await deduplication.check_duplicates_batch(
                        session,
                        processed_facts,
                        storage.check_duplicates_in_window,
                        vault_id=vault_id,
                    )

                    final_processed = [
                        pf for pf, is_dup in zip(processed_facts, is_duplicate) if not is_dup
                    ]
                    final_extracted = [
                        ef for ef, is_dup in zip(extracted_facts, is_duplicate) if not is_dup
                    ]

                    if final_processed:
                        for ef, pf in zip(final_extracted, final_processed):
                            pf.note_id = note_id
                            if ef.chunk_index is not None and ef.chunk_index in block_chunk_map:
                                pf.chunk_id = block_chunk_map[ef.chunk_index]

                        unit_ids = await storage.insert_facts_batch(
                            session, final_processed, note_id=note_id
                        )

                        touched_entity_ids = await self._resolve_entities(
                            session, unit_ids, final_processed, vault_id=vault_id
                        )
                        await self._create_links(
                            session, unit_ids, final_processed, vault_id=vault_id
                        )

        # 10. Update thin tree + document tracking
        await self._track_document(
            session, note_id, contents, is_first_batch=False, vault_id=vault_id
        )

        toc_id_to_hash: dict[str, str] = {}

        def _collect_hashes(node: TOCNode) -> None:
            h = node.content_hash or content_hash_md5(node.content or node.title)
            toc_id_to_hash[node.id] = h
            for child in node.children:
                _collect_hashes(child)

        for toc_node in page_index_output.toc:
            _collect_hashes(toc_node)

        def _replace_ids(tree_dict: dict[str, Any]) -> dict[str, Any]:
            old_id = tree_dict.get('id', '')
            tree_dict['id'] = toc_id_to_hash.get(old_id, old_id)
            tree_dict['children'] = [_replace_ids(c) for c in tree_dict.get('children', [])]
            return tree_dict

        thin_tree = [
            _replace_ids(n.tree_without_text(min_node_tokens=min_tokens))
            for n in page_index_output.toc
            if min_tokens <= 0 or (n.token_estimate or 0) > min_tokens
        ]
        await storage.update_note_page_index(session, note_id, thin_tree)

        provided_name: str | None = contents[0].payload.get('note_name') if contents else None
        resolved_title = await resolve_title_from_page_index(
            page_index_toc=thin_tree,
            provided_name=provided_name,
            lm=self.page_index_lm,
            session=session,
            vault_id=vault_id,
        )
        await storage.update_note_title(session, note_id, resolved_title)

        # Update Reflection Queue
        if self.queue_service and touched_entity_ids:
            await self.queue_service.handle_extraction_event(
                session, touched_entity_ids, vault_id=vault_id
            )

        # Log usage with incremental page_index metadata
        usage.session_id = get_session_id()
        usage.vault_id = vault_id
        usage.context_metadata = {
            'operation': 'extract',
            'mode': 'page_index_incremental',
            'note_id': note_id,
            'blocks_total': len(page_index_output.blocks),
            'blocks_retained': len(retained_hashes),
            'blocks_boundary_shift': len(boundary_shift_hashes),
            'blocks_content_changed': len(content_changed_hashes),
            'blocks_removed': len(removed_hashes),
            'facts_migrated': len(chunk_migration),
            'unit_count': len(unit_ids),
            'vault_id': str(vault_id),
        }
        session.add(usage)

        return unit_ids, usage, touched_entity_ids

    async def _extract_page_index(
        self,
        session: AsyncSession,
        contents: list[RetainContent],
        agent_name: str,
        note_id: str | None,
        is_first_batch: bool,
        vault_id: UUID,
        extract_opinions: bool = False,
        content_fingerprint: str | None = None,
    ) -> tuple[list[str], TokenUsage, set[UUID]]:
        """Page-index extraction path: hierarchical TOC → nodes → blocks → facts.

        1. Run index_document() → PageIndexOutput
        2. Create nodes from TOC tree → insert via insert_nodes_batch()
        3. Create blocks from PageIndex output → compute embeddings → store blocks
        4. Store thin tree on document
        5. Extract facts from block texts → existing fact pipeline
        """
        combined_text = '\n'.join(c.content for c in contents)
        event_date = contents[0].event_date if contents else None

        ts = self.config.text_splitting
        assert isinstance(ts, PageIndexTextSplitting)
        assert self.page_index_lm is not None

        # 1. Run PageIndex
        page_index_output, pi_usage = await index_document(
            full_text=combined_text,
            lm=self.page_index_lm,
            scan_chunk_size=ts.scan_chunk_size_tokens * CHARS_PER_TOKEN,
            max_node_length=ts.max_node_length_tokens * CHARS_PER_TOKEN,
            block_token_target=ts.block_token_target,
            short_doc_threshold=ts.short_doc_threshold_tokens * CHARS_PER_TOKEN,
        )

        logger.info(
            f'PageIndex completed: path={page_index_output.path_used}, '
            f'blocks={len(page_index_output.blocks)}, '
            f'coverage={page_index_output.coverage_ratio:.1%}'
        )

        # Track document
        effective_doc_id = note_id
        if not effective_doc_id:
            effective_doc_id = str(UUID(int=0))

        await self._track_document(
            session, effective_doc_id, contents, is_first_batch, vault_id=vault_id
        )

        # 2. Flatten TOC tree into node rows and insert
        min_tokens = ts.min_node_tokens
        node_rows, block_chunk_map = await self._persist_page_index_nodes_and_blocks(
            session=session,
            page_index_output=page_index_output,
            note_id=effective_doc_id,
            vault_id=vault_id,
            min_node_tokens=min_tokens,
        )

        # 3. Build a mapping from TOCNode id → node_hash so the thin tree
        #    uses stable hashes as IDs (matching the DB `node_hash` column).
        #    This allows LLM-returned section IDs to be looked up directly.
        toc_id_to_hash: dict[str, str] = {}

        def _collect_hashes(node: TOCNode) -> None:
            h = node.content_hash or content_hash_md5(node.content or node.title)
            toc_id_to_hash[node.id] = h
            for child in node.children:
                _collect_hashes(child)

        for toc_node in page_index_output.toc:
            _collect_hashes(toc_node)

        def _replace_ids(tree_dict: dict[str, Any]) -> dict[str, Any]:
            old_id = tree_dict.get('id', '')
            tree_dict['id'] = toc_id_to_hash.get(old_id, old_id)
            tree_dict['children'] = [_replace_ids(c) for c in tree_dict.get('children', [])]
            return tree_dict

        thin_tree = [
            _replace_ids(n.tree_without_text(min_node_tokens=min_tokens))
            for n in page_index_output.toc
            if min_tokens <= 0 or (n.token_estimate or 0) > min_tokens
        ]
        await storage.update_note_page_index(session, effective_doc_id, thin_tree)

        # Resolve and store the document title from the TOC / block summaries.
        # This supersedes the rough title stored by _track_document above.
        provided_name: str | None = contents[0].payload.get('note_name') if contents else None
        resolved_title = await resolve_title_from_page_index(
            page_index_toc=thin_tree,
            provided_name=provided_name,
            lm=self.page_index_lm,
            session=session,
            vault_id=vault_id,
        )
        await storage.update_note_title(session, effective_doc_id, resolved_title)

        # 4. Extract facts from block texts
        block_texts = [block.content for block in page_index_output.blocks]
        if not block_texts:
            return [], pi_usage, set()

        raw_facts, chunk_meta, usage = await extract_facts_from_chunks(
            chunks=block_texts,
            event_date=event_date or contents[0].event_date,
            lm=self.lm,
            predictor=self.predictor,
            agent_name=agent_name,
            context='',
            extract_opinions=extract_opinions,
            semaphore=self.semaphore,
            vault_id=vault_id,
        )
        usage += pi_usage

        if not raw_facts:
            return [], usage, set()

        # Convert to ExtractedFacts
        extracted_facts: list[ExtractedFact] = []
        global_fact_idx = 0
        facts_start_idx = 0

        for chunk_idx, (chunk_text_val, fact_count) in enumerate(chunk_meta):
            chunk_facts = raw_facts[facts_start_idx : facts_start_idx + fact_count]
            facts_start_idx += fact_count

            for f in chunk_facts:
                ef = ExtractedFact(
                    fact_text=f.formatted_text,
                    fact_type=f.fact_type,
                    entities=f.entities,
                    occurred_start=parse_datetime(f.occurred_start) if f.occurred_start else None,
                    occurred_end=parse_datetime(f.occurred_end) if f.occurred_end else None,
                    causal_relations=_convert_causal_relations(
                        relations_from_llm=f.causal_relations,
                        fact_start_idx=global_fact_idx,
                    ),
                    content_index=0,
                    chunk_index=chunk_idx,
                    context='',
                    mentioned_at=event_date or contents[0].event_date,
                    payload=contents[0].payload or {},
                    where=f.where,
                    vault_id=vault_id,
                )
                extracted_facts.append(ef)
                global_fact_idx += 1

        if not extracted_facts:
            return [], usage, set()

        self._add_temporal_offsets(extracted_facts)
        processed_facts = await self._process_embeddings(extracted_facts)

        for fact in processed_facts:
            if fact.fact_type == 'opinion':
                alpha, beta = await self.confidence_engine.calculate_informative_prior(
                    session, fact.embedding
                )
                fact.confidence_alpha = alpha
                fact.confidence_beta = beta

        # Deduplication
        is_duplicate = await deduplication.check_duplicates_batch(
            session, processed_facts, storage.check_duplicates_in_window, vault_id=vault_id
        )

        final_processed = [pf for pf, is_dup in zip(processed_facts, is_duplicate) if not is_dup]
        final_extracted = [ef for ef, is_dup in zip(extracted_facts, is_duplicate) if not is_dup]

        if not final_processed:
            return [], usage, set()

        # Link facts to blocks
        for ef, pf in zip(final_extracted, final_processed):
            pf.note_id = effective_doc_id
            if ef.chunk_index is not None and ef.chunk_index in block_chunk_map:
                pf.chunk_id = block_chunk_map[ef.chunk_index]

        unit_ids = await storage.insert_facts_batch(
            session, final_processed, note_id=effective_doc_id
        )

        touched_entity_ids = await self._resolve_entities(
            session, unit_ids, final_processed, vault_id=vault_id
        )
        await self._create_links(session, unit_ids, final_processed, vault_id=vault_id)

        if self.queue_service:
            await self.queue_service.handle_extraction_event(
                session, touched_entity_ids, vault_id=vault_id
            )

        # Log usage
        usage.session_id = get_session_id()
        usage.vault_id = vault_id
        usage.context_metadata = {
            'operation': 'extract',
            'mode': 'page_index',
            'note_id': note_id,
            'unit_count': len(unit_ids),
            'blocks': len(page_index_output.blocks),
            'path_used': page_index_output.path_used,
            'vault_id': str(vault_id),
        }
        session.add(usage)

        return unit_ids, usage, touched_entity_ids

    async def _persist_page_index_nodes_and_blocks(
        self,
        session: AsyncSession,
        page_index_output: PageIndexOutput,
        note_id: str,
        vault_id: UUID,
        min_node_tokens: int = 0,
        skip_block_hashes: set[str] | None = None,
    ) -> tuple[list[str], dict[int, str]]:
        """Create DB nodes and blocks from PageIndex output.

        Args:
            session: Active DB session.
            page_index_output: PageIndex result with TOC and blocks.
            note_id: Stable document identifier.
            vault_id: Vault scope.
            min_node_tokens: Skip nodes with fewer tokens than this threshold.
            skip_block_hashes: Block content hashes to skip (retained blocks).
                When provided, blocks whose ``id`` is in this set are not
                embedded or stored as chunks.

        Returns:
            Tuple of (node_ids, block_chunk_map) where block_chunk_map maps
            block sequence index to chunk UUID string.
        """
        doc_uuid = UUID(note_id)

        # Flatten TOC tree into node rows
        node_rows: list[dict[str, object]] = []
        seq_counter = 0

        def flatten_nodes(nodes: list[TOCNode], parent_block_id: str | None = None) -> None:
            nonlocal seq_counter
            for node in nodes:
                # Skip trivially short nodes but still recurse into children
                if min_node_tokens > 0 and (node.token_estimate or 0) <= min_node_tokens:
                    if node.children:
                        flatten_nodes(node.children, parent_block_id)
                    continue
                block_id_str = page_index_output.node_to_block_map.get(node.id)
                node_hash = node.content_hash or content_hash_md5(node.content or node.title)

                summary_dict = None
                summary_fmt = None
                if node.summary:
                    summary_dict = node.summary.model_dump()
                    summary_fmt = node.summary.formatted

                node_rows.append(
                    {
                        'vault_id': vault_id,
                        'note_id': doc_uuid,
                        'node_hash': node_hash,
                        'title': node.title,
                        'text': node.content or '',
                        'summary': summary_dict,
                        'summary_formatted': summary_fmt,
                        'level': node.level,
                        'seq': seq_counter,
                        'token_estimate': node.token_estimate or 0,
                        'status': ContentStatus.ACTIVE,
                    }
                )
                seq_counter += 1

                if node.children:
                    flatten_nodes(node.children, block_id_str)

        flatten_nodes(page_index_output.toc)

        # Deduplicate by node_hash before the batch upsert. PostgreSQL raises
        # CardinalityViolationError if the same (note_id, node_hash) appears
        # twice in one INSERT ... ON CONFLICT DO UPDATE batch.
        seen_hashes: set[str] = set()
        deduped: list[dict[str, object]] = []
        for row in node_rows:
            h = str(row['node_hash'])
            if h not in seen_hashes:
                seen_hashes.add(h)
                deduped.append(row)
        node_rows = deduped

        # Insert nodes (block_id will be backfilled after blocks are created)
        node_ids = await storage.insert_nodes_batch(session, node_rows)

        # Create blocks: compute embeddings on block content, store as chunks.
        # When skip_block_hashes is given, only embed+store non-retained blocks.
        effective_skip = skip_block_hashes or set()
        block_chunk_metadata: list[ChunkMetadata] = []
        for block in page_index_output.blocks:
            if block.id in effective_skip:
                continue
            block_chunk_metadata.append(
                ChunkMetadata(
                    chunk_text=block.content,
                    fact_count=0,
                    content_index=0,
                    chunk_index=block.seq,
                    content_hash=block.id,
                )
            )

        if block_chunk_metadata:
            emb_texts = [c.chunk_text for c in block_chunk_metadata]
            chunk_embeddings = await embedding_processor.generate_embeddings_batch(
                self.embedding_model, emb_texts
            )
            for cm, emb in zip(block_chunk_metadata, chunk_embeddings):
                cm.embedding = emb

        block_chunk_map = await self._store_chunks(
            session, note_id, block_chunk_metadata, vault_id=vault_id
        )

        # Backfill Node.block_id using the node_to_block_map and block_chunk_map.
        # Build node_hash -> chunk UUID mapping by cross-referencing:
        #   node.id -> block_hash (node_to_block_map)
        #   block_hash -> block.seq (from blocks list)
        #   block.seq -> chunk UUID (block_chunk_map)
        block_hash_to_seq = {b.id: b.seq for b in page_index_output.blocks}
        node_hash_to_block_id: dict[str, UUID] = {}

        for node_id, block_hash in page_index_output.node_to_block_map.items():
            block_seq = block_hash_to_seq.get(block_hash)
            if block_seq is None:
                continue
            chunk_uuid_str = block_chunk_map.get(block_seq)
            if chunk_uuid_str is None:
                continue
            # Find the TOC node to get its content_hash (used as DB node_hash)
            node_hash = self._find_node_hash(page_index_output.toc, node_id)
            if node_hash:
                node_hash_to_block_id[node_hash] = UUID(chunk_uuid_str)

        await storage.backfill_node_block_ids(session, note_id, node_hash_to_block_id)

        return node_ids, block_chunk_map

    @staticmethod
    def _find_node_hash(toc: list[TOCNode], target_id: str) -> str | None:
        """Recursively find a TOC node by ID and return its content hash."""
        for node in toc:
            if node.id == target_id:
                return node.content_hash or content_hash_md5(node.content or node.title)
            result = ExtractionEngine._find_node_hash(node.children, target_id)
            if result is not None:
                return result
        return None

    def _assemble_llm_chunks(
        self,
        all_blocks: list[StableBlock],
        added_blocks: list[StableBlock],
        retained_hashes: set[str],
    ) -> list[dict[str, str]]:
        """Assemble ADDED blocks into LLM chunks with ±1 RETAINED neighbor context.

        Each ADDED block becomes one LLM chunk. The nearest RETAINED neighbor
        block(s) before/after are included as read-only context in the DSPy
        ``context`` field.

        Args:
            all_blocks: All blocks in document order.
            added_blocks: Blocks that need extraction.
            retained_hashes: Set of content hashes for retained blocks.

        Returns:
            List of dicts with ``text`` and ``context`` keys.
        """
        block_by_index = {b.block_index: b for b in all_blocks}
        result: list[dict[str, str]] = []

        for block in added_blocks:
            context_parts: list[str] = []

            # Look for ±1 RETAINED neighbor
            prev_idx = block.block_index - 1
            if prev_idx in block_by_index:
                prev_block = block_by_index[prev_idx]
                if prev_block.content_hash in retained_hashes:
                    context_parts.append(prev_block.text)

            next_idx = block.block_index + 1
            if next_idx in block_by_index:
                next_block = block_by_index[next_idx]
                if next_block.content_hash in retained_hashes:
                    context_parts.append(next_block.text)

            result.append(
                {
                    'text': block.text,
                    'context': '\n\n'.join(context_parts),
                    'content_hash': block.content_hash,
                }
            )

        return result

    async def adjust_belief(
        self,
        session: AsyncSession,
        unit_uuid: str,
        evidence_type_key: str,
        description: str | None = None,
    ) -> None:
        """
        Adjust the confidence of a memory unit based on new evidence.
        Delegates to the ConfidenceEngine.
        """
        await self.confidence_engine.adjust_belief(
            session, unit_uuid, evidence_type_key, description
        )
        await session.commit()

    async def _extract_facts(
        self,
        contents: list[RetainContent],
        agent_name: str,
        extract_opinions: bool,
    ) -> tuple[list[ExtractedFact], list[ChunkMetadata], TokenUsage]:
        """Run LLM extraction in parallel with semaphore."""

        ts = self.config.text_splitting
        chunk_max = (
            ts.chunk_size_tokens * CHARS_PER_TOKEN if isinstance(ts, SimpleTextSplitting) else 4000
        )
        chunk_overlap = (
            ts.chunk_overlap_tokens * CHARS_PER_TOKEN
            if isinstance(ts, SimpleTextSplitting)
            else 200
        )

        async def _sem_extract(content: RetainContent):
            return await extract_facts_from_text(
                text=content.content,
                event_date=content.event_date,
                lm=self.lm,
                predictor=self.predictor,
                agent_name=agent_name,
                chunk_max_chars=chunk_max,
                chunk_overlap=chunk_overlap,
                context=content.context or '',
                extract_opinions=extract_opinions,
                semaphore=self.semaphore,
                vault_id=content.vault_id,
            )

        tasks = [_sem_extract(c) for c in contents]
        results = await asyncio.gather(*tasks)

        extracted_facts: list[ExtractedFact] = []
        chunk_metadata: list[ChunkMetadata] = []
        total_usage = TokenUsage()

        global_chunk_idx = 0
        global_fact_idx = 0

        for content_idx, (content, (facts, chunks, usage)) in enumerate(zip(contents, results)):
            total_usage += usage
            facts_start_idx = 0

            for chunk_text, fact_count in chunks:
                chunk_metadata.append(
                    ChunkMetadata(
                        chunk_text=chunk_text,
                        fact_count=fact_count,
                        content_index=content_idx,
                        chunk_index=global_chunk_idx,
                        content_hash=content_hash(chunk_text),
                    )
                )

                chunk_facts = facts[facts_start_idx : facts_start_idx + fact_count]
                facts_start_idx += fact_count
                chunk_start_fact_idx = global_fact_idx

                for f in chunk_facts:
                    ef = ExtractedFact(
                        fact_text=f.formatted_text,
                        fact_type=f.fact_type,
                        entities=f.entities,
                        occurred_start=parse_datetime(f.occurred_start)
                        if f.occurred_start
                        else None,
                        occurred_end=parse_datetime(f.occurred_end) if f.occurred_end else None,
                        causal_relations=_convert_causal_relations(
                            relations_from_llm=f.causal_relations,
                            fact_start_idx=chunk_start_fact_idx,
                        ),
                        content_index=content_idx,
                        chunk_index=global_chunk_idx,
                        context=content.context,
                        mentioned_at=content.event_date,
                        payload=content.payload or {},  # Ensure payload is dict
                        where=f.where,
                        vault_id=content.vault_id,
                    )
                    extracted_facts.append(ef)
                    global_fact_idx += 1

                global_chunk_idx += 1

        self._add_temporal_offsets(extracted_facts)
        return extracted_facts, chunk_metadata, total_usage

    async def _process_embeddings(self, facts: list[ExtractedFact]) -> list[ProcessedFact]:
        """Augment text with dates and generate embeddings."""
        formatted_texts = embedding_processor.format_facts_for_embedding(facts)
        embeddings = await embedding_processor.generate_embeddings_batch(
            self.embedding_model, formatted_texts
        )

        processed = []
        for fact, emb in zip(facts, embeddings):
            pf = ProcessedFact.from_extracted_fact(fact, emb)
            pf.vault_id = fact.vault_id
            processed.append(pf)
        return processed

    async def _store_chunks(
        self,
        session: AsyncSession,
        note_id: str,
        chunks: list[ChunkMetadata],
        vault_id: UUID = GLOBAL_VAULT_ID,
    ) -> dict[int, str]:
        return await storage.store_chunks_batch(session, note_id, chunks, vault_id=vault_id)

    async def _track_document(
        self,
        session: AsyncSession,
        note_id: str,
        contents: list[RetainContent],
        is_first_batch: bool,
        vault_id: UUID = GLOBAL_VAULT_ID,
    ):
        combined_content = '\n'.join([c.content for c in contents])
        retain_params = {}
        tags = []
        assets: list[str] = []

        if contents:
            first = contents[0]
            retain_params.update(first.payload)
            if hasattr(first, 'tags') and first.tags:
                from typing import cast

                tags = cast(list[str], first.tags)

            # Extract assets list if present in payload
            if 'assets' in first.payload and isinstance(first.payload['assets'], list):
                assets = first.payload['assets']

        publish_date = contents[0].event_date if contents else None

        await storage.handle_document_tracking(
            session,
            note_id,
            combined_content,
            is_first_batch,
            retain_params,
            tags,
            vault_id=vault_id,
            assets=assets,
            content_fingerprint=retain_params.get('content_fingerprint'),
            publish_date=publish_date,
        )

    def _add_temporal_offsets(self, facts: list[ExtractedFact]) -> None:
        """Add slight time offsets to facts to preserve ordering."""
        current_content_idx = 0
        content_fact_start = 0

        for i, fact in enumerate(facts):
            if fact.content_index != current_content_idx:
                current_content_idx = fact.content_index
                content_fact_start = i

            fact_position = i - content_fact_start
            offset = timedelta(seconds=fact_position * self.SECONDS_PER_FACT)

            if fact.occurred_start:
                fact.occurred_start += offset
            if fact.occurred_end:
                fact.occurred_end += offset
            if fact.mentioned_at:
                fact.mentioned_at += offset

    async def _resolve_entities(
        self,
        session: AsyncSession,
        unit_ids: list[str],
        facts: list[ProcessedFact],
        vault_id: UUID = GLOBAL_VAULT_ID,
    ) -> set[UUID]:
        """Resolve entities and link them to units. Returns set of touched entity IDs."""
        entities_data = []

        for i, fact in enumerate(facts):
            unit_id = unit_ids[i]
            for ent in fact.entities:
                entities_data.append(
                    {
                        'text': ent.text,
                        'event_date': fact.occurred_start or fact.mentioned_at,
                        'nearby_entities': [{'text': e.text} for e in fact.entities],
                        'unit_id': unit_id,
                    }
                )

        if not entities_data:
            return set()

        default_date = facts[0].mentioned_at if facts else datetime.now(timezone.utc)
        resolved_ids = await self.entity_resolver.resolve_entities_batch(
            session, entities_data, default_date
        )

        unit_entity_pairs = []
        for data, entity_id in zip(entities_data, resolved_ids):
            unit_entity_pairs.append((data['unit_id'], entity_id))

        await self.entity_resolver.link_units_to_entities_batch(
            session, unit_entity_pairs, vault_id=vault_id
        )

        return {UUID(rid) for rid in resolved_ids}

    async def _create_links(
        self,
        session: AsyncSession,
        unit_ids: list[str],
        facts: list[ProcessedFact],
        vault_id: UUID = GLOBAL_VAULT_ID,
    ):
        """Create Temporal and Causal links."""
        links = []

        # 1. Causal Links
        for i, fact in enumerate(facts):
            uid_from = unit_ids[i]
            for rel in fact.causal_relations:
                target_idx = rel.target_fact_index
                if 0 <= target_idx < len(unit_ids) and target_idx != i:
                    uid_to = unit_ids[target_idx]
                    links.append(
                        {
                            'from_unit_id': uid_to
                            if rel.relationship_type == 'caused_by'
                            else uid_from,
                            'to_unit_id': uid_from
                            if rel.relationship_type == 'caused_by'
                            else uid_to,
                            'vault_id': vault_id,
                            'link_type': rel.relationship_type.value
                            if hasattr(rel.relationship_type, 'value')
                            else rel.relationship_type,
                            'weight': rel.strength,
                        }
                    )

        # 2. Temporal Links
        sorted_indices = sorted(
            range(len(facts)),
            key=lambda i: normalize_timestamp(facts[i].occurred_start or facts[i].mentioned_at),
        )
        for k in range(len(sorted_indices) - 1):
            idx_a = sorted_indices[k]
            idx_b = sorted_indices[k + 1]
            fact_a = facts[idx_a]
            fact_b = facts[idx_b]

            # Intra-document temporal links
            if fact_a.note_id and fact_a.note_id == fact_b.note_id:
                links.append(
                    {
                        'from_unit_id': unit_ids[idx_a],
                        'to_unit_id': unit_ids[idx_b],
                        'vault_id': vault_id,
                        'link_type': 'temporal',
                        'weight': 1.0,
                    }
                )

        # 3. Semantic Links
        # Find similar facts for each new fact
        # We must run these sequentially because we are sharing the same AsyncSession
        for i, fact in enumerate(facts):
            # Exclude the fact itself from search results
            exclude = [UUID(unit_ids[i])]
            similar_items = await storage.find_similar_facts(
                session,
                fact.embedding,
                limit=5,
                threshold=0.75,  # Configurable?
                exclude_ids=exclude,
                vault_ids=[vault_id] if vault_id else None,
            )

            from_id = unit_ids[i]
            for target_uuid, score in similar_items:
                if math.isnan(score):
                    continue

                links.append(
                    {
                        'from_unit_id': from_id,
                        'to_unit_id': str(target_uuid),
                        'vault_id': vault_id,
                        'link_type': 'semantic',
                        'weight': score,
                    }
                )

        if links:
            stmt = pg_insert(MemoryLink).values(links).on_conflict_do_nothing()
            await session.exec(stmt)

        # 4. Cross-Document Temporal Linking
        await self._create_cross_doc_links(session, unit_ids, facts, vault_id=vault_id)

    async def _create_cross_doc_links(
        self,
        session: AsyncSession,
        unit_ids: list[str],
        facts: list[ProcessedFact],
        vault_id: UUID = GLOBAL_VAULT_ID,
    ):
        """Link the new batch of facts to the existing timeline in the DB."""
        if not facts:
            return

        # Identify the temporal bounds of the new batch
        # We sort by event_date (occurred_start or mentioned_at)
        sorted_facts = sorted(
            zip(unit_ids, facts),
            key=lambda x: normalize_timestamp(x[1].occurred_start or x[1].mentioned_at),
        )

        earliest_id, earliest_fact = sorted_facts[0]
        latest_id, latest_fact = sorted_facts[-1]

        earliest_ts = earliest_fact.occurred_start or earliest_fact.mentioned_at
        latest_ts = latest_fact.occurred_start or latest_fact.mentioned_at

        # Exclude current batch IDs from search to avoid self-linking
        current_batch_uuids = [UUID(uid) for uid in unit_ids]

        # Find Predecessor (Fact < Earliest)
        predecessor_uuid = await storage.find_temporal_neighbor(
            session, earliest_ts, direction='before', exclude_ids=current_batch_uuids
        )

        # Find Successor (Fact > Latest)
        successor_uuid = await storage.find_temporal_neighbor(
            session, latest_ts, direction='after', exclude_ids=current_batch_uuids
        )

        cross_links = []
        if predecessor_uuid:
            cross_links.append(
                {
                    'from_unit_id': str(predecessor_uuid),
                    'to_unit_id': earliest_id,
                    'vault_id': vault_id,
                    'link_type': 'temporal',
                    'weight': 1.0,
                }
            )

        if successor_uuid:
            cross_links.append(
                {
                    'from_unit_id': latest_id,
                    'to_unit_id': str(successor_uuid),
                    'vault_id': vault_id,
                    'link_type': 'temporal',
                    'weight': 1.0,
                }
            )

        if cross_links:
            stmt = pg_insert(MemoryLink).values(cross_links).on_conflict_do_nothing()
            await session.exec(stmt)
