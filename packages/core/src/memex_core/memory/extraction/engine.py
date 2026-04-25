import asyncio
import logging
import re
from datetime import datetime, timezone
from uuid import UUID

import dspy
from sqlalchemy.exc import DBAPIError
from sqlmodel.ext.asyncio.session import AsyncSession

import memex_core.config
from memex_core.tracing import trace_span
from memex_core.config import (
    ExtractionConfig,
    ReflectionConfig,
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
    PageIndexOutput,
)
import yaml

from memex_core.memory.extraction.core import (
    extract_facts_from_text,
    extract_facts_from_chunks,
    extract_facts_from_frontmatter,
    _convert_causal_relations,
    _detect_frontmatter,
    ExtractSemanticFacts,
    FRONTMATTER_PATTERN,
    stable_chunk_text,
    content_hash,
    index_document,
)
from memex_core.memory.models.embedding import get_embedding_model
from memex_core.memory.extraction.utils import parse_iso_datetime
from memex_core.memory.extraction import storage, embedding_processor, deduplication
from memex_core.memory.extraction.pipeline.diffing import (
    assemble_llm_chunks,
    build_page_index_with_metadata,
    diff_blocks,
    diff_page_index_blocks,
    find_node_hash,
    flatten_toc_to_node_rows,
)
from memex_core.memory.extraction.pipeline.tracking import (
    track_document,
    enqueue_for_reflection,
)
from memex_core.memory.extraction.pipeline.linking import create_links
from memex_core.memory.extraction.pipeline.fact_processing import (
    add_temporal_offsets,
    process_embeddings,
)
from memex_core.memory.entity_resolver import EntityResolver
from memex_core.memory.reflect.queue_service import ReflectionQueueService
from memex_core.processing.titles import resolve_title_from_page_index

logger = logging.getLogger('memex.core.memory.extraction.engine')


def _is_statement_timeout(exc: DBAPIError) -> bool:
    """Check if a DBAPIError wraps an asyncpg QueryCanceledError (statement timeout)."""
    orig = getattr(exc, 'orig', None)
    return type(orig).__name__ == 'QueryCanceledError' if orig else False


_USER_NOTES_FIELD_RE = re.compile(r'^user_notes:.*\n(?:[ \t]+.*\n)*', re.MULTILINE)


def _separate_user_notes_from_frontmatter(
    fm_text: str,
) -> tuple[str | None, str | None]:
    """Strip ``user_notes`` from frontmatter for separate semantic extraction.

    Returns ``(cleaned_frontmatter_or_None, user_notes_text_or_None)``.
    *cleaned_frontmatter* is ``None`` when the frontmatter contained **only**
    the ``user_notes`` field.
    """
    match = FRONTMATTER_PATTERN.match(fm_text)
    if not match:
        return fm_text, None
    yaml_body = match.group('yaml')
    try:
        data = yaml.safe_load(yaml_body)
    except yaml.YAMLError:
        return fm_text, None
    if not isinstance(data, dict) or 'user_notes' not in data:
        return fm_text, None

    user_notes_text = str(data['user_notes'])
    cleaned = _USER_NOTES_FIELD_RE.sub('', yaml_body + '\n').strip()
    cleaned_fm = f'---\n{cleaned}\n---\n' if cleaned else None
    return cleaned_fm, user_notes_text


def _make_lm(model_cfg: 'memex_core.config.ModelConfig') -> dspy.LM:
    """Create a DSPy LM from a ModelConfig."""
    return dspy.LM(
        model=model_cfg.model,
        api_base=str(model_cfg.base_url) if model_cfg.base_url else None,
        api_key=model_cfg.api_key.get_secret_value() if model_cfg.api_key else None,
        reasoning_effort=model_cfg.reasoning_effort.value
        if model_cfg.reasoning_effort is not None
        else None,
        timeout=model_cfg.timeout,
        num_retries=model_cfg.num_retries,
    )


async def get_extraction_engine(
    config: ExtractionConfig,
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
        lm=lm,
        predictor=predictor,
        embedding_model=embedding_model,
        entity_resolver=entity_resolver,
        reflection_config=reflection_config,
        page_index_lm=page_index_lm,
    )


def _synthesize_tags(
    page_index_output: PageIndexOutput,
    user_tags: list[str],
) -> list[str]:
    """Merge user-provided tags with LLM-generated block tags."""
    tags: list[str] = list(user_tags) if user_tags else []
    seen: set[str] = {t.strip().lower() for t in tags}
    for block in page_index_output.blocks:
        if block.summary:
            for tag in block.summary.tags:
                normalized = tag.strip().lower()
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    tags.append(normalized)
    return tags[:15]  # Cap at 15 tags


def _synthesize_description(
    page_index_output: PageIndexOutput,
    fallback: str | None,
) -> str | None:
    """Build a note description from block summary topics or short-doc content."""
    topics = [b.summary.topic for b in page_index_output.blocks if b.summary and b.summary.topic]
    if topics:
        combined = '; '.join(topics)
        return combined[:200] if len(combined) > 200 else combined
    if page_index_output.path_used == 'short_doc_bypass':
        content = page_index_output.blocks[0].content if page_index_output.blocks else None
        if content:
            plain = re.sub(r'^#{1,6}\s+', '', content, flags=re.MULTILINE)
            plain = re.sub(r'\s+', ' ', plain).strip()
            return plain[:200] if len(plain) > 200 else plain
    return fallback


class ExtractionEngine:
    """
    Orchestrates the extraction, embedding, resolution, and persistence of memory.
    """

    SECONDS_PER_FACT = 10

    def __init__(
        self,
        config: ExtractionConfig,
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
        content_fingerprint: str | None = None,
    ) -> tuple[list[str], set[UUID]]:
        """
        Main entry point: Extract facts from content and persist them to memory.
        Returns (unit_ids, touched_entity_ids).

        When ``note_id`` is provided and the document already has blocks in
        the DB, the incremental path is used: only changed blocks trigger LLM
        extraction.
        """
        if not contents:
            return [], set()

        # Determine vault_id (assuming uniform per batch)
        vault_id = contents[0].vault_id if contents else GLOBAL_VAULT_ID

        with trace_span(
            'memex.extraction',
            'extraction',
            {
                'extraction.note_id': note_id or '',
                'extraction.vault_id': str(vault_id),
            },
        ):
            # Check if incremental path is viable
            if note_id and is_first_batch:
                existing_blocks = await storage.get_note_blocks(session, note_id)
                if existing_blocks:
                    if (
                        self.config.active_strategy == 'page_index'
                        and self.page_index_lm is not None
                    ):
                        return await self._extract_page_index_incremental(
                            session=session,
                            contents=contents,
                            agent_name=agent_name,
                            note_id=note_id,
                            existing_blocks=existing_blocks,
                            vault_id=vault_id,
                            content_fingerprint=content_fingerprint,
                        )
                    return await self._extract_incremental(
                        session=session,
                        contents=contents,
                        agent_name=agent_name,
                        note_id=note_id,
                        existing_blocks=existing_blocks,
                        vault_id=vault_id,
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
                    content_fingerprint=content_fingerprint,
                )

            # --- Full extraction path (simple strategy or no page_index LM) ---
            event_date = contents[0].event_date if contents else None
            extracted_facts, chunks = await self._extract_facts(contents, agent_name)

            if chunks:
                chunk_texts = [c.chunk_text for c in chunks]
                chunk_embeddings = await embedding_processor.generate_embeddings_batch(
                    self.embedding_model, chunk_texts
                )
                for chunk, emb in zip(chunks, chunk_embeddings):
                    chunk.embedding = emb

            if not extracted_facts:
                if note_id:
                    await track_document(
                        session, note_id, contents, is_first_batch, vault_id=vault_id
                    )
                return [], set()

            processed_facts = await process_embeddings(self.embedding_model, extracted_facts)

            # Deduplication
            is_duplicate = await deduplication.check_duplicates_batch(
                session,
                processed_facts,
                storage.check_duplicates_in_window,
                vault_id=vault_id,
                event_date=event_date,
            )

            # Filter duplicates from both lists to maintain parallelism
            # We need to filter extracted_facts too for chunk linking logic below
            final_processed_facts = []
            final_extracted_facts = []

            for i, is_dup in enumerate(is_duplicate):
                if not is_dup:
                    final_processed_facts.append(processed_facts[i])
                    final_extracted_facts.append(extracted_facts[i])

            if not final_processed_facts:
                logger.info(f'All {len(processed_facts)} facts were duplicates.')
                if note_id:
                    await track_document(
                        session, note_id, contents, is_first_batch, vault_id=vault_id
                    )
                return [], set()

            extracted_facts = final_extracted_facts
            processed_facts = final_processed_facts

            effective_doc_id = note_id
            if chunks and not effective_doc_id:
                logger.warning(
                    'Chunks present but no note_id provided. Chunk linking may be partial.'
                )
                effective_doc_id = str(UUID(int=0))

            if effective_doc_id:
                await track_document(
                    session, effective_doc_id, contents, is_first_batch, vault_id=vault_id
                )
                chunk_map = await storage.store_chunks_batch(
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
            await create_links(
                session, unit_ids, processed_facts, vault_id=vault_id, event_date=event_date
            )

            # Update Reflection Queue Priorities
            await enqueue_for_reflection(session, touched_entity_ids, vault_id, self.queue_service)

            return unit_ids, touched_entity_ids

    async def _extract_incremental(
        self,
        session: AsyncSession,
        contents: list[RetainContent],
        agent_name: str,
        note_id: str,
        existing_blocks: list[dict[str, object]],
        vault_id: UUID,
        content_fingerprint: str | None = None,
    ) -> tuple[list[str], set[UUID]]:
        """Incremental extraction: diff blocks, extract only changed content.

        Args:
            session: Active DB session.
            contents: Content items to extract from.
            agent_name: Agent performing extraction.
            note_id: Stable document identifier.
            existing_blocks: Current blocks from DB (via get_note_blocks).
            vault_id: Vault scope.
            content_fingerprint: Content fingerprint for document tracking.

        Returns:
            Tuple of (unit_ids, touched_entity_ids).
        """
        combined_text = '\n'.join(c.content for c in contents)
        event_date = contents[0].event_date if contents else None

        # 1. Generate new blocks using stable chunking
        ts = self.config.text_splitting
        block_size = (
            ts.chunk_size_tokens * CHARS_PER_TOKEN if isinstance(ts, SimpleTextSplitting) else 4000
        )
        # exempt: pure CPU sync chunker (incremental), no model load (AC-009 four-bucket audit)
        new_blocks = await asyncio.to_thread(
            stable_chunk_text, combined_text, block_size=block_size
        )

        # 2-3. Diff new blocks against existing blocks
        block_diff = diff_blocks(new_blocks, existing_blocks)
        retained_hashes = block_diff.retained_hashes
        added_blocks = block_diff.added_blocks
        removed_hashes = block_diff.removed_hashes

        # Build existing hash map for reconciliation later
        existing_hash_map: dict[str, dict[str, object]] = {
            str(b['content_hash']): b for b in existing_blocks
        }

        logger.info(
            f'Incremental diff for document {note_id}: '
            f'blocks_total={len(new_blocks)} retained={len(retained_hashes)} '
            f'added={len(added_blocks)} removed={len(removed_hashes)}'
        )

        # 4. Extract facts from ADDED blocks only
        unit_ids: list[str] = []
        touched_entity_ids: set[UUID] = set()

        if added_blocks:
            # Assemble ADDED blocks into LLM chunks with ±1 RETAINED neighbor context
            llm_chunks = assemble_llm_chunks(new_blocks, added_blocks, retained_hashes)

            # Extract facts from assembled chunks
            chunk_texts = [c['text'] for c in llm_chunks]
            contexts = [c.get('context', '') for c in llm_chunks]

            # Extract from each chunk with its context
            all_extracted_facts: list[ExtractedFact] = []
            all_chunk_metadata: list[ChunkMetadata] = []

            for chunk_idx, (chunk_text_val, ctx) in enumerate(zip(chunk_texts, contexts)):
                raw_facts, chunk_meta = await extract_facts_from_chunks(
                    chunks=[chunk_text_val],
                    event_date=event_date or contents[0].event_date,
                    lm=self.lm,
                    predictor=self.predictor,
                    agent_name=agent_name,
                    context=ctx,
                    semaphore=self.semaphore,
                )

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
                        occurred_start=parse_iso_datetime(raw_fact.occurred_start)
                        if raw_fact.occurred_start
                        else None,
                        occurred_end=parse_iso_datetime(raw_fact.occurred_end)
                        if raw_fact.occurred_end
                        else None,
                        causal_relations=_convert_causal_relations(
                            relations_from_llm=raw_fact.causal_relations,
                            fact_start_idx=fact_start_idx,
                        ),
                        content_index=0,
                        chunk_index=chunk_idx,
                        context=ctx,
                        mentioned_at=event_date or contents[0].event_date,
                        payload=contents[0].payload or {},
                        who=raw_fact.who,
                        where=raw_fact.where,
                        vault_id=vault_id,
                    )
                    all_extracted_facts.append(ef)

            # Extract facts from YAML frontmatter if present.
            # Frontmatter is at the start, so assign to chunk 0.
            fm_text, _ = _detect_frontmatter(combined_text)
            if fm_text:
                cleaned_fm, user_notes_text = _separate_user_notes_from_frontmatter(fm_text)
                if cleaned_fm:
                    fm_facts = await extract_facts_from_frontmatter(
                        frontmatter_text=cleaned_fm,
                        event_date=event_date or contents[0].event_date,
                        lm=self.lm,
                        semaphore=self.semaphore,
                    )
                    for f in fm_facts:
                        ef = ExtractedFact(
                            fact_text=f.formatted_text,
                            fact_type=f.fact_type,
                            entities=f.entities,
                            occurred_start=parse_iso_datetime(f.occurred_start)
                            if f.occurred_start
                            else None,
                            occurred_end=parse_iso_datetime(f.occurred_end)
                            if f.occurred_end
                            else None,
                            causal_relations=[],
                            content_index=0,
                            chunk_index=0,
                            context='frontmatter',
                            mentioned_at=event_date or contents[0].event_date,
                            payload=contents[0].payload or {},
                            who=f.who,
                            where=f.where,
                            vault_id=vault_id,
                        )
                        all_extracted_facts.append(ef)
                if user_notes_text:
                    un_facts, _ = await extract_facts_from_text(
                        text=user_notes_text,
                        event_date=event_date or contents[0].event_date,
                        lm=self.lm,
                        predictor=self.predictor,
                        agent_name=agent_name,
                        chunk_max_chars=4000,
                        chunk_overlap=200,
                        context='user_notes',
                        semaphore=self.semaphore,
                    )
                    for f in un_facts:
                        ef = ExtractedFact(
                            fact_text=f.formatted_text,
                            fact_type=f.fact_type,
                            entities=f.entities,
                            occurred_start=parse_iso_datetime(f.occurred_start)
                            if f.occurred_start
                            else None,
                            occurred_end=parse_iso_datetime(f.occurred_end)
                            if f.occurred_end
                            else None,
                            causal_relations=[],
                            content_index=0,
                            chunk_index=0,
                            context='user_notes',
                            mentioned_at=event_date or contents[0].event_date,
                            payload=contents[0].payload or {},
                            who=f.who,
                            where=f.where,
                            vault_id=vault_id,
                        )
                        all_extracted_facts.append(ef)

            if all_extracted_facts:
                add_temporal_offsets(all_extracted_facts, self.SECONDS_PER_FACT)
                processed_facts = await process_embeddings(
                    self.embedding_model, all_extracted_facts
                )

                # Deduplication
                is_duplicate = await deduplication.check_duplicates_batch(
                    session,
                    processed_facts,
                    storage.check_duplicates_in_window,
                    vault_id=vault_id,
                    event_date=event_date,
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
                    chunk_map = await storage.store_chunks_batch(
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
                    await create_links(
                        session, unit_ids, final_processed, vault_id=vault_id, event_date=event_date
                    )

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
        await track_document(session, note_id, contents, is_first_batch=False, vault_id=vault_id)

        # Update Reflection Queue
        await enqueue_for_reflection(session, touched_entity_ids, vault_id, self.queue_service)

        return unit_ids, touched_entity_ids

    async def _extract_page_index_incremental(
        self,
        session: AsyncSession,
        contents: list[RetainContent],
        agent_name: str,
        note_id: str,
        existing_blocks: list[dict[str, object]],
        vault_id: UUID,
        content_fingerprint: str | None = None,
    ) -> tuple[list[str], set[UUID]]:
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
            content_fingerprint: Content fingerprint for document tracking.

        Returns:
            Tuple of (unit_ids, touched_entity_ids).
        """
        combined_text = '\n'.join(c.content for c in contents)
        event_date = contents[0].event_date if contents else None

        ts = self.config.text_splitting
        assert isinstance(ts, PageIndexTextSplitting)
        assert self.page_index_lm is not None

        # 1. Run PageIndex
        page_index_output = await index_document(
            full_text=combined_text,
            lm=self.page_index_lm,
            max_scan_tokens=ts.scan_chunk_size_tokens,
            max_node_length=ts.max_node_length_tokens * CHARS_PER_TOKEN,
            block_token_target=ts.block_token_target,
            short_doc_threshold=ts.short_doc_threshold_tokens * CHARS_PER_TOKEN,
            scan_max_concurrency=ts.scan_max_concurrency,
            gap_rescan_threshold_tokens=ts.gap_rescan_threshold_tokens,
        )

        logger.info(
            f'PageIndex incremental: path={page_index_output.path_used}, '
            f'blocks={len(page_index_output.blocks)}, '
            f'coverage={page_index_output.coverage_ratio:.1%}'
        )

        # 2-3. Block-level diff with node-level change detection
        prev_nodes = await storage.get_note_nodes(session, note_id)
        prev_node_hash_set = {str(n['node_hash']) for n in prev_nodes}

        pi_diff = diff_page_index_blocks(page_index_output, existing_blocks, prev_node_hash_set)
        retained_hashes = pi_diff.retained_hashes
        boundary_shift_hashes = pi_diff.boundary_shift_hashes
        content_changed_hashes = pi_diff.content_changed_hashes
        removed_hashes = pi_diff.removed_hashes
        block_node_hashes = pi_diff.block_node_hashes

        # Build existing hash map for reconciliation later
        existing_hash_map: dict[str, dict[str, object]] = {
            str(b['content_hash']): b for b in existing_blocks
        }

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
        unit_ids: list[str] = []
        touched_entity_ids: set[UUID] = set()

        changed_blocks = [b for b in page_index_output.blocks if b.id in content_changed_hashes]
        if changed_blocks:
            block_texts = [b.content for b in changed_blocks]
            raw_facts, chunk_meta = await extract_facts_from_chunks(
                chunks=block_texts,
                event_date=event_date or contents[0].event_date,
                lm=self.lm,
                predictor=self.predictor,
                agent_name=agent_name,
                context='',
                semaphore=self.semaphore,
            )

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
                            occurred_start=parse_iso_datetime(f.occurred_start)
                            if f.occurred_start
                            else None,
                            occurred_end=parse_iso_datetime(f.occurred_end)
                            if f.occurred_end
                            else None,
                            causal_relations=_convert_causal_relations(
                                relations_from_llm=f.causal_relations,
                                fact_start_idx=global_fact_idx,
                            ),
                            content_index=0,
                            chunk_index=block_seq,
                            context='',
                            mentioned_at=event_date or contents[0].event_date,
                            payload=contents[0].payload or {},
                            who=f.who,
                            where=f.where,
                            vault_id=vault_id,
                        )
                        extracted_facts.append(ef)
                        global_fact_idx += 1

                if extracted_facts:
                    add_temporal_offsets(extracted_facts, self.SECONDS_PER_FACT)
                    processed_facts = await process_embeddings(
                        self.embedding_model, extracted_facts
                    )

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
                        await create_links(
                            session,
                            unit_ids,
                            final_processed,
                            vault_id=vault_id,
                            event_date=event_date,
                        )

        # 10. Update thin tree + document tracking
        await track_document(session, note_id, contents, is_first_batch=False, vault_id=vault_id)

        retain_params = contents[0].payload if contents else {}
        synthesized_desc = _synthesize_description(
            page_index_output, retain_params.get('note_description')
        )
        user_tags = retain_params.get('tags', [])
        synthesized_tags = _synthesize_tags(page_index_output, user_tags)
        page_index = build_page_index_with_metadata(
            page_index_output.toc,
            metadata={
                'title': retain_params.get('note_name'),
                'description': synthesized_desc,
                'tags': synthesized_tags,
                'publish_date': str(event_date) if event_date else None,
                'source_uri': retain_params.get('source_uri'),
            },
            min_node_tokens=min_tokens,
        )
        await storage.update_note_page_index(session, note_id, page_index)
        await storage.update_note_description(session, note_id, synthesized_desc)
        if synthesized_tags:
            await storage.update_note_tags(session, note_id, synthesized_tags)

        provided_name: str | None = contents[0].payload.get('note_name') if contents else None
        resolved_title = await resolve_title_from_page_index(
            page_index_toc=page_index['toc'],
            provided_name=provided_name,
            lm=self.page_index_lm,
        )
        await storage.update_note_title(session, note_id, resolved_title)

        # Update Reflection Queue
        await enqueue_for_reflection(session, touched_entity_ids, vault_id, self.queue_service)

        return unit_ids, touched_entity_ids

    async def _extract_page_index(
        self,
        session: AsyncSession,
        contents: list[RetainContent],
        agent_name: str,
        note_id: str | None,
        is_first_batch: bool,
        vault_id: UUID,
        content_fingerprint: str | None = None,
    ) -> tuple[list[str], set[UUID]]:
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
        page_index_output = await index_document(
            full_text=combined_text,
            lm=self.page_index_lm,
            max_scan_tokens=ts.scan_chunk_size_tokens,
            max_node_length=ts.max_node_length_tokens * CHARS_PER_TOKEN,
            block_token_target=ts.block_token_target,
            short_doc_threshold=ts.short_doc_threshold_tokens * CHARS_PER_TOKEN,
            scan_max_concurrency=ts.scan_max_concurrency,
            gap_rescan_threshold_tokens=ts.gap_rescan_threshold_tokens,
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

        await track_document(session, effective_doc_id, contents, is_first_batch, vault_id=vault_id)

        # 2. Flatten TOC tree into node rows and insert
        min_tokens = ts.min_node_tokens
        node_rows, block_chunk_map = await self._persist_page_index_nodes_and_blocks(
            session=session,
            page_index_output=page_index_output,
            note_id=effective_doc_id,
            vault_id=vault_id,
            min_node_tokens=min_tokens,
        )

        # 3. Build hash-stable thin tree with metadata for storage
        retain_params = contents[0].payload if contents else {}
        synthesized_desc = _synthesize_description(
            page_index_output, retain_params.get('note_description')
        )
        user_tags = retain_params.get('tags', [])
        synthesized_tags = _synthesize_tags(page_index_output, user_tags)
        page_index = build_page_index_with_metadata(
            page_index_output.toc,
            metadata={
                'title': retain_params.get('note_name'),
                'description': synthesized_desc,
                'tags': synthesized_tags,
                'publish_date': str(event_date) if event_date else None,
                'source_uri': retain_params.get('source_uri'),
            },
            min_node_tokens=min_tokens,
        )
        await storage.update_note_page_index(session, effective_doc_id, page_index)
        await storage.update_note_description(session, effective_doc_id, synthesized_desc)
        if synthesized_tags:
            await storage.update_note_tags(session, effective_doc_id, synthesized_tags)

        # Resolve and store the document title from the TOC / block summaries.
        # This supersedes the rough title stored by _track_document above.
        provided_name: str | None = contents[0].payload.get('note_name') if contents else None
        resolved_title = await resolve_title_from_page_index(
            page_index_toc=page_index['toc'],
            provided_name=provided_name,
            lm=self.page_index_lm,
        )
        await storage.update_note_title(session, effective_doc_id, resolved_title)

        # 4. Extract facts from block texts
        block_texts = [block.content for block in page_index_output.blocks]
        if not block_texts:
            return [], set()

        raw_facts, chunk_meta = await extract_facts_from_chunks(
            chunks=block_texts,
            event_date=event_date or contents[0].event_date,
            lm=self.lm,
            predictor=self.predictor,
            agent_name=agent_name,
            context='',
            semaphore=self.semaphore,
        )

        if not raw_facts:
            return [], set()

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
                    occurred_start=parse_iso_datetime(f.occurred_start)
                    if f.occurred_start
                    else None,
                    occurred_end=parse_iso_datetime(f.occurred_end) if f.occurred_end else None,
                    causal_relations=_convert_causal_relations(
                        relations_from_llm=f.causal_relations,
                        fact_start_idx=global_fact_idx,
                    ),
                    content_index=0,
                    chunk_index=chunk_idx,
                    context='',
                    mentioned_at=event_date or contents[0].event_date,
                    payload=contents[0].payload or {},
                    who=f.who,
                    where=f.where,
                    vault_id=vault_id,
                )
                extracted_facts.append(ef)
                global_fact_idx += 1

        # Extract facts from YAML frontmatter if present.
        # Frontmatter is at the start, so assign to the first block (seq 0).
        fm_text, _ = _detect_frontmatter(combined_text)
        if fm_text and contents:
            first_block_seq = page_index_output.blocks[0].seq if page_index_output.blocks else 0
            cleaned_fm, user_notes_text = _separate_user_notes_from_frontmatter(fm_text)
            if cleaned_fm:
                fm_facts = await extract_facts_from_frontmatter(
                    frontmatter_text=cleaned_fm,
                    event_date=event_date or contents[0].event_date,
                    lm=self.lm,
                    semaphore=self.semaphore,
                )
                for f in fm_facts:
                    ef = ExtractedFact(
                        fact_text=f.formatted_text,
                        fact_type=f.fact_type,
                        entities=f.entities,
                        occurred_start=parse_iso_datetime(f.occurred_start)
                        if f.occurred_start
                        else None,
                        occurred_end=parse_iso_datetime(f.occurred_end) if f.occurred_end else None,
                        causal_relations=[],
                        content_index=0,
                        chunk_index=first_block_seq,
                        context='frontmatter',
                        mentioned_at=event_date or contents[0].event_date,
                        payload=contents[0].payload or {},
                        who=f.who,
                        where=f.where,
                        vault_id=vault_id,
                    )
                    extracted_facts.append(ef)
                    global_fact_idx += 1
            if user_notes_text:
                un_facts, _ = await extract_facts_from_text(
                    text=user_notes_text,
                    event_date=event_date or contents[0].event_date,
                    lm=self.lm,
                    predictor=self.predictor,
                    agent_name=agent_name,
                    chunk_max_chars=4000,
                    chunk_overlap=200,
                    context='user_notes',
                    semaphore=self.semaphore,
                )
                for f in un_facts:
                    ef = ExtractedFact(
                        fact_text=f.formatted_text,
                        fact_type=f.fact_type,
                        entities=f.entities,
                        occurred_start=parse_iso_datetime(f.occurred_start)
                        if f.occurred_start
                        else None,
                        occurred_end=parse_iso_datetime(f.occurred_end) if f.occurred_end else None,
                        causal_relations=[],
                        content_index=0,
                        chunk_index=first_block_seq,
                        context='user_notes',
                        mentioned_at=event_date or contents[0].event_date,
                        payload=contents[0].payload or {},
                        who=f.who,
                        where=f.where,
                        vault_id=vault_id,
                    )
                    extracted_facts.append(ef)
                    global_fact_idx += 1

        if not raw_facts and not extracted_facts:
            return [], set()

        add_temporal_offsets(extracted_facts, self.SECONDS_PER_FACT)
        processed_facts = await process_embeddings(self.embedding_model, extracted_facts)

        # Deduplication
        is_duplicate = await deduplication.check_duplicates_batch(
            session, processed_facts, storage.check_duplicates_in_window, vault_id=vault_id
        )

        final_processed = [pf for pf, is_dup in zip(processed_facts, is_duplicate) if not is_dup]
        final_extracted = [ef for ef, is_dup in zip(extracted_facts, is_duplicate) if not is_dup]

        if not final_processed:
            return [], set()

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
        await create_links(session, unit_ids, final_processed, vault_id=vault_id)

        await enqueue_for_reflection(session, touched_entity_ids, vault_id, self.queue_service)

        return unit_ids, touched_entity_ids

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

        # Flatten TOC tree into deduplicated node rows
        node_rows = flatten_toc_to_node_rows(
            page_index_output.toc,
            page_index_output,
            vault_id,
            doc_uuid,
            min_node_tokens,
        )

        # Insert nodes (block_id will be backfilled after blocks are created)
        node_ids = await storage.insert_nodes_batch(session, node_rows)

        # Create blocks: compute embeddings on block content, store as chunks.
        # When skip_block_hashes is given, only embed+store non-retained blocks.
        effective_skip = skip_block_hashes or set()
        block_chunk_metadata: list[ChunkMetadata] = []
        seen_block_ids: dict[str, int] = {}  # block.id -> first block.seq
        for block in page_index_output.blocks:
            if block.id in effective_skip:
                continue
            if block.id in seen_block_ids:
                continue  # duplicate content — will reuse the first block's chunk
            seen_block_ids[block.id] = block.seq
            block_chunk_metadata.append(
                ChunkMetadata(
                    chunk_text=block.content,
                    fact_count=0,
                    content_index=0,
                    chunk_index=block.seq,
                    content_hash=block.id,
                    summary=block.summary.model_dump() if block.summary else None,
                    summary_formatted=block.summary.formatted if block.summary else None,
                )
            )

        if block_chunk_metadata:
            emb_texts = [c.chunk_text for c in block_chunk_metadata]
            chunk_embeddings = await embedding_processor.generate_embeddings_batch(
                self.embedding_model, emb_texts
            )
            for cm, emb in zip(block_chunk_metadata, chunk_embeddings):
                cm.embedding = emb

        block_chunk_map = await storage.store_chunks_batch(
            session, note_id, block_chunk_metadata, vault_id=vault_id
        )

        # Backfill Node.block_id using the node_to_block_map and block_chunk_map.
        # Build node_hash -> chunk UUID mapping by cross-referencing:
        #   node.id -> block_hash (node_to_block_map)
        #   block_hash -> block.seq (from blocks list)
        #   block.seq -> chunk UUID (block_chunk_map)
        block_hash_to_seq: dict[str, int] = {}
        for b in page_index_output.blocks:
            if b.id not in block_hash_to_seq:
                block_hash_to_seq[b.id] = b.seq
        node_hash_to_block_id: dict[str, UUID] = {}

        for node_id, block_hash in page_index_output.node_to_block_map.items():
            block_seq = block_hash_to_seq.get(block_hash)
            if block_seq is None:
                continue
            chunk_uuid_str = block_chunk_map.get(block_seq)
            if chunk_uuid_str is None:
                continue
            # Find the TOC node to get its content_hash (used as DB node_hash)
            node_hash = find_node_hash(page_index_output.toc, node_id)
            if node_hash:
                node_hash_to_block_id[node_hash] = UUID(chunk_uuid_str)

        await storage.backfill_node_block_ids(session, note_id, node_hash_to_block_id)

        return node_ids, block_chunk_map

    async def _extract_facts(
        self,
        contents: list[RetainContent],
        agent_name: str,
    ) -> tuple[list[ExtractedFact], list[ChunkMetadata]]:
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
                semaphore=self.semaphore,
            )

        tasks = [_sem_extract(c) for c in contents]
        results = await asyncio.gather(*tasks)

        extracted_facts: list[ExtractedFact] = []
        chunk_metadata: list[ChunkMetadata] = []

        global_chunk_idx = 0
        global_fact_idx = 0

        for content_idx, (content, (facts, chunks)) in enumerate(zip(contents, results)):
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
                        occurred_start=parse_iso_datetime(f.occurred_start)
                        if f.occurred_start
                        else None,
                        occurred_end=parse_iso_datetime(f.occurred_end) if f.occurred_end else None,
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

        # Extract facts from YAML frontmatter if present.
        # Frontmatter is at the start of the document, so assign to chunk 0.
        combined_text = '\n'.join(c.content for c in contents)
        fm_text, _ = _detect_frontmatter(combined_text)
        if fm_text and contents:
            event_date = contents[0].event_date
            cleaned_fm, user_notes_text = _separate_user_notes_from_frontmatter(fm_text)
            if cleaned_fm:
                fm_facts = await extract_facts_from_frontmatter(
                    frontmatter_text=cleaned_fm,
                    event_date=event_date,
                    lm=self.lm,
                    semaphore=self.semaphore,
                )
                for f in fm_facts:
                    ef = ExtractedFact(
                        fact_text=f.formatted_text,
                        fact_type=f.fact_type,
                        entities=f.entities,
                        occurred_start=parse_iso_datetime(f.occurred_start)
                        if f.occurred_start
                        else None,
                        occurred_end=parse_iso_datetime(f.occurred_end) if f.occurred_end else None,
                        causal_relations=[],
                        content_index=0,
                        chunk_index=0,
                        context='frontmatter',
                        mentioned_at=event_date,
                        payload=contents[0].payload or {},
                        who=f.who,
                        where=f.where,
                        vault_id=contents[0].vault_id,
                    )
                    extracted_facts.append(ef)
            if user_notes_text:
                un_facts, _ = await extract_facts_from_text(
                    text=user_notes_text,
                    event_date=event_date,
                    lm=self.lm,
                    predictor=self.predictor,
                    agent_name=agent_name,
                    chunk_max_chars=chunk_max,
                    chunk_overlap=chunk_overlap,
                    context='user_notes',
                    semaphore=self.semaphore,
                )
                for f in un_facts:
                    ef = ExtractedFact(
                        fact_text=f.formatted_text,
                        fact_type=f.fact_type,
                        entities=f.entities,
                        occurred_start=parse_iso_datetime(f.occurred_start)
                        if f.occurred_start
                        else None,
                        occurred_end=parse_iso_datetime(f.occurred_end) if f.occurred_end else None,
                        causal_relations=[],
                        content_index=0,
                        chunk_index=0,
                        context='user_notes',
                        mentioned_at=event_date,
                        payload=contents[0].payload or {},
                        who=f.who,
                        where=f.where,
                        vault_id=contents[0].vault_id,
                    )
                    extracted_facts.append(ef)
                global_fact_idx += 1

        add_temporal_offsets(extracted_facts, self.SECONDS_PER_FACT)
        return extracted_facts, chunk_metadata

    async def _resolve_entities(
        self,
        session: AsyncSession,
        unit_ids: list[str],
        facts: list[ProcessedFact],
        vault_id: UUID = GLOBAL_VAULT_ID,
    ) -> set[UUID]:
        """Resolve entities and link them to units. Returns set of touched entity IDs.

        On transient DB errors (e.g. statement timeout from lock contention),
        logs a warning and returns an empty set. The reflection queue's stale
        recovery will pick up unprocessed entities on the next cycle.
        """
        import re

        entities_data = []

        for i, fact in enumerate(facts):
            unit_id = unit_ids[i]
            # Collect entity names already in the entities list
            existing_names = {ent.text.lower() for ent in fact.entities}

            for ent in fact.entities:
                entities_data.append(
                    {
                        'text': ent.text,
                        'event_date': fact.occurred_start or fact.mentioned_at,
                        'nearby_entities': [{'text': e.text} for e in fact.entities],
                        'unit_id': unit_id,
                        'entity_type': ent.entity_type,
                    }
                )

            # Discover person/location names from who/where fields
            for field_text, default_type in [
                (getattr(fact, 'who', None), 'Person'),
                (getattr(fact, 'where', None), 'Location'),
            ]:
                if not field_text or field_text.upper() in ('N/A', 'NONE'):
                    continue

                # Extract capitalized names from who/where fields
                # (e.g. "Emily, Sarah" or "San Francisco")
                skip = {'the', 'and', 'for', 'with', 'from', 'via', 'n/a'}
                for match in re.finditer(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b', field_text):
                    candidate = match.group(1)
                    if candidate.lower() not in existing_names and candidate.lower() not in skip:
                        entities_data.append(
                            {
                                'text': candidate,
                                'event_date': fact.occurred_start or fact.mentioned_at,
                                'nearby_entities': [{'text': e.text} for e in fact.entities],
                                'unit_id': unit_id,
                                'entity_type': default_type,
                            }
                        )
                        existing_names.add(candidate.lower())

        if not entities_data:
            return set()

        default_date = facts[0].mentioned_at if facts else datetime.now(timezone.utc)

        try:
            resolved_ids = await self.entity_resolver.resolve_entities_batch(
                session, entities_data, default_date
            )
        except DBAPIError as e:
            if _is_statement_timeout(e):
                logger.warning(
                    'Entity resolution timed out (likely lock contention). '
                    'Skipping — entities will be resolved on next ingestion or reflection cycle.'
                )
                return set()
            raise

        unit_entity_pairs = []
        unit_timestamps: dict[str, datetime] = {}
        for data, entity_id in zip(entities_data, resolved_ids):
            unit_entity_pairs.append((data['unit_id'], entity_id))
            # Populate unit_timestamps from event_date (occurred_start or mentioned_at).
            # Keep the earliest timestamp per unit.
            event_dt = data.get('event_date')
            if event_dt is not None:
                existing = unit_timestamps.get(data['unit_id'])
                if existing is None or event_dt < existing:
                    unit_timestamps[data['unit_id']] = event_dt

        try:
            await self.entity_resolver.link_units_to_entities_batch(
                session,
                unit_entity_pairs,
                vault_id=vault_id,
                unit_timestamps=unit_timestamps or None,
            )
        except DBAPIError as e:
            if _is_statement_timeout(e):
                logger.warning(
                    'Entity linking timed out (likely lock contention). '
                    'Skipping — links will be created on next ingestion cycle.'
                )
                return set()
            raise

        return {UUID(rid) for rid in resolved_ids}

    async def prepare_user_notes(
        self,
        user_notes_text: str,
        vault_id: UUID,
        event_date: datetime | None = None,
    ) -> list[ProcessedFact]:
        """LLM extraction + embedding for user_notes text.  No DB access.

        Call :meth:`persist_user_notes` afterwards inside a DB session to
        write the results.  Splitting the two phases lets callers keep the
        expensive LLM/embedding work outside a held DB connection.

        Returns an empty list when there is nothing to persist.
        """
        if not user_notes_text or not user_notes_text.strip():
            return []

        effective_date = event_date or datetime.now(timezone.utc)

        un_facts, _ = await extract_facts_from_text(
            text=user_notes_text,
            event_date=effective_date,
            lm=self.lm,
            predictor=self.predictor,
            agent_name='memex_agent',
            chunk_max_chars=4000,
            chunk_overlap=200,
            context='user_notes',
            semaphore=self.semaphore,
        )

        all_extracted_facts = [
            ExtractedFact(
                fact_text=f.formatted_text,
                fact_type=f.fact_type,
                entities=f.entities,
                occurred_start=parse_iso_datetime(f.occurred_start) if f.occurred_start else None,
                occurred_end=parse_iso_datetime(f.occurred_end) if f.occurred_end else None,
                causal_relations=[],
                content_index=0,
                chunk_index=0,
                context='user_notes',
                mentioned_at=effective_date,
                payload={},
                who=f.who,
                where=f.where,
                vault_id=vault_id,
            )
            for f in un_facts
        ]

        if not all_extracted_facts:
            return []

        add_temporal_offsets(all_extracted_facts, self.SECONDS_PER_FACT)
        return await process_embeddings(self.embedding_model, all_extracted_facts)

    async def persist_user_notes(
        self,
        session: AsyncSession,
        processed_facts: list[ProcessedFact],
        note_id: str,
        vault_id: UUID,
    ) -> tuple[list[str], set[UUID]]:
        """Persist pre-processed user_notes facts into the DB.

        Handles deduplication, storage, entity resolution, linking, and
        reflection enqueuing.  Must be called inside an active DB session.

        Returns:
            ``(unit_ids, touched_entity_ids)``
        """
        if not processed_facts:
            return [], set()

        is_duplicate = await deduplication.check_duplicates_batch(
            session, processed_facts, storage.check_duplicates_in_window, vault_id=vault_id
        )
        final_processed = [pf for pf, dup in zip(processed_facts, is_duplicate) if not dup]

        if not final_processed:
            return [], set()

        for pf in final_processed:
            pf.note_id = note_id

        unit_ids = await storage.insert_facts_batch(session, final_processed, note_id=note_id)

        touched_entity_ids = await self._resolve_entities(
            session, unit_ids, final_processed, vault_id=vault_id
        )
        await create_links(session, unit_ids, final_processed, vault_id=vault_id)

        await enqueue_for_reflection(session, touched_entity_ids, vault_id, self.queue_service)

        return unit_ids, touched_entity_ids

    async def extract_user_notes(
        self,
        session: AsyncSession,
        user_notes_text: str,
        note_id: str,
        vault_id: UUID,
        event_date: datetime | None = None,
    ) -> tuple[list[str], set[UUID]]:
        """Extract facts from user_notes text using the full post-processing pipeline.

        Convenience wrapper that calls :meth:`prepare_user_notes` then
        :meth:`persist_user_notes` within the provided *session*.

        .. note::

           This holds the DB session open during LLM inference.  Prefer
           calling ``prepare_user_notes`` outside a session and
           ``persist_user_notes`` inside one when connection-pool pressure
           is a concern.

        Returns:
            ``(unit_ids, touched_entity_ids)``
        """
        processed = await self.prepare_user_notes(user_notes_text, vault_id, event_date)
        return await self.persist_user_notes(session, processed, note_id, vault_id)
