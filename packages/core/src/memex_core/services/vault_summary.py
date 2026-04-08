"""Service for vault summary generation and maintenance.

Vault summaries provide a structured overview of a vault's contents, split into:
- **Computed fields** (inventory, key_entities): derived from DB aggregates, always fresh
- **Synthesized fields** (themes, narrative): LLM-generated, updated incrementally

Notes are tracked via ``summary_version_incorporated`` — each note records the
summary version that last included it.  Staleness is determined by comparing
this column to ``VaultSummary.version``.

Batching uses a **token budget** on the serialized metadata payload rather than
a fixed note count, so notes with dense chunk summaries are batched more
conservatively than notes with just a title.

Full regeneration is available on demand via ``regenerate_summary()``.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import dspy
from sqlalchemy import update as sa_update
from sqlmodel import col, func, select

from memex_common.config import VaultSummaryConfig
from memex_core.llm import run_dspy_operation
from memex_core.memory.sql_models import (
    Chunk,
    ContentStatus,
    Entity,
    Note,
    UnitEntity,
    VaultSummary,
)
from memex_core.services.vault_summary_signatures import (
    BatchResult,
    LLMTheme,
    NoteMetadata,
    VaultStats,
    VaultSummaryFullSignature,
    VaultSummaryUpdateSignature,
    VaultTopicExtractSignature,
    VaultTopicMergeSignature,
)
from memex_core.storage.metastore import AsyncBaseMetaStoreEngine

logger = logging.getLogger('memex.core.services.vault_summary')

_SYSTEM_TAGS = frozenset({'obsidian', 'cli', 'quick-note', 'note-with-assets', 'system-hint'})


def _themes_to_dicts(themes: list[LLMTheme | dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert LLMTheme objects to dicts for JSONB persistence."""
    return [t.model_dump() if isinstance(t, LLMTheme) else t for t in themes]


def _estimate_tokens(text: str) -> int:
    """Rough chars-to-tokens estimate (÷4)."""
    return len(text) // 4


def _split_into_token_batches(
    notes_data: list[dict[str, Any]],
    max_tokens: int,
    max_notes: int,
) -> list[list[dict[str, Any]]]:
    """Split notes into batches that fit within a token budget.

    Each batch respects both ``max_tokens`` (payload size) and ``max_notes``
    (hard note-count safety cap).  A single note that exceeds ``max_tokens``
    on its own is placed alone in a batch.
    """
    batches: list[list[dict[str, Any]]] = []
    current_batch: list[dict[str, Any]] = []
    current_tokens = 0

    for entry in notes_data:
        entry_tokens = _estimate_tokens(json.dumps(entry, default=str))

        if current_batch and (
            current_tokens + entry_tokens > max_tokens or len(current_batch) >= max_notes
        ):
            batches.append(current_batch)
            current_batch = []
            current_tokens = 0

        current_batch.append(entry)
        current_tokens += entry_tokens

    if current_batch:
        batches.append(current_batch)
    return batches


class VaultSummaryService:
    """Manages vault-level summaries with version-tracked updates and on-demand regeneration."""

    def __init__(
        self,
        metastore: AsyncBaseMetaStoreEngine,
        lm: dspy.LM,
        config: VaultSummaryConfig,
    ) -> None:
        self.metastore = metastore
        self.lm = lm
        self.config = config

    async def get_summary(self, vault_id: UUID) -> VaultSummary | None:
        """Fetch the current vault summary, or None if none exists."""
        async with self.metastore.session() as session:
            stmt = select(VaultSummary).where(col(VaultSummary.vault_id) == vault_id)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def delete_summary(self, vault_id: UUID) -> bool:
        """Delete the vault summary. Returns True if a summary was deleted."""
        async with self.metastore.session() as session:
            stmt = select(VaultSummary).where(col(VaultSummary.vault_id) == vault_id)
            result = await session.execute(stmt)
            summary = result.scalar_one_or_none()
            if summary is None:
                return False
            await session.delete(summary)
            await session.commit()
            return True

    async def mark_needs_regeneration(self, vault_id: UUID) -> None:
        """Flag the vault summary for full regeneration.

        Sets ``needs_regeneration = True`` on the existing summary row.
        No-op if no summary exists yet (regeneration will happen naturally
        when the first summary is created).
        """
        async with self.metastore.session() as session:
            stmt = select(VaultSummary).where(col(VaultSummary.vault_id) == vault_id)
            result = await session.execute(stmt)
            summary = result.scalar_one_or_none()
            if summary is None:
                return
            summary.needs_regeneration = True
            session.add(summary)
            await session.commit()

    async def is_stale(self, vault_id: UUID) -> bool:
        """Check if the vault summary needs updating.

        Returns True if:
        - No summary exists and the vault has active notes
        - ``needs_regeneration`` flag is set
        - Active notes exist with ``summary_version_incorporated`` that is NULL
          or less than the current summary version
        """
        async with self.metastore.session() as session:
            stmt = select(VaultSummary).where(col(VaultSummary.vault_id) == vault_id)
            result = await session.execute(stmt)
            summary = result.scalar_one_or_none()

            if summary is None:
                count_stmt = (
                    select(func.count())
                    .select_from(Note)
                    .where(col(Note.vault_id) == vault_id)
                    .where(col(Note.status) == 'active')
                )
                count = (await session.execute(count_stmt)).scalar() or 0
                return count > 0

            if summary.needs_regeneration:
                return True

            count_stmt = (
                select(func.count())
                .select_from(Note)
                .where(col(Note.vault_id) == vault_id)
                .where(col(Note.status) == 'active')
                .where(
                    (col(Note.summary_version_incorporated).is_(None))
                    | (col(Note.summary_version_incorporated) < summary.version)
                )
            )
            count = (await session.execute(count_stmt)).scalar() or 0
            return count > 0

    async def update_summary(self, vault_id: UUID) -> VaultSummary:
        """Update the vault summary with notes not yet incorporated.

        Fetches notes whose ``summary_version_incorporated`` is NULL or behind
        the current summary version. Uses token-based batching: if the delta
        exceeds the token budget, it is split into sequential batches — each
        batch updates the running themes/narrative and marks its notes.

        If no summary exists, falls back to ``regenerate_summary()``.
        """
        # Phase 1: Read current summary + delta notes + total count
        async with self.metastore.session() as session:
            stmt = select(VaultSummary).where(col(VaultSummary.vault_id) == vault_id)
            result = await session.execute(stmt)
            summary = result.scalar_one_or_none()

            if summary is None:
                return await self.regenerate_summary(vault_id)

            current_narrative = summary.narrative
            current_themes = list(summary.themes)
            current_version = summary.version

            notes_data, _included_ids, all_fetched_ids = await self._fetch_note_metadata(
                session, vault_id, summary_version=current_version
            )

            if not notes_data:
                # Mark skipped notes so they don't perpetually appear as pending
                if all_fetched_ids:
                    mark_stmt = (
                        sa_update(Note)
                        .where(col(Note.vault_id) == vault_id)
                        .where(col(Note.status) == 'active')
                        .where(
                            (col(Note.summary_version_incorporated).is_(None))
                            | (col(Note.summary_version_incorporated) < current_version)
                        )
                        .values(summary_version_incorporated=current_version)
                    )
                    await session.execute(mark_stmt)
                    await session.commit()
                return summary

            total_stmt = (
                select(func.count())
                .select_from(Note)
                .where(col(Note.vault_id) == vault_id)
                .where(col(Note.status) == 'active')
            )
            total_notes = (await session.execute(total_stmt)).scalar() or 0

        # Phase 2: Compute inventory + key_entities (pure SQL, no LLM)
        inventory = await self._compute_inventory(vault_id)
        key_entities = await self._compute_key_entities(vault_id)

        # Phase 3: LLM call(s) outside any DB session — token-batched
        batches = _split_into_token_batches(
            notes_data, self.config.max_batch_tokens, self.config.batch_size
        )

        running_narrative = current_narrative
        running_themes = [LLMTheme(**t) if isinstance(t, dict) else t for t in current_themes]

        for batch in batches:
            notes = [NoteMetadata(**n) for n in batch]
            predictor = dspy.Predict(VaultSummaryUpdateSignature)
            prediction = await run_dspy_operation(
                lm=self.lm,
                predictor=predictor,
                input_kwargs={
                    'current_narrative': running_narrative,
                    'current_themes': running_themes,
                    'new_notes': notes,
                    'vault_stats': VaultStats(
                        total_notes=total_notes,
                        new_since_last=len(batch),
                        max_narrative_tokens=self.config.max_narrative_tokens,
                    ),
                },
                operation_name='vault_summary_update',
            )

            running_narrative = prediction.updated_narrative
            running_themes = prediction.updated_themes

        # Phase 4: Persist with SELECT FOR UPDATE to prevent concurrent overwrites
        async with self.metastore.session() as session:
            stmt = (
                select(VaultSummary).where(col(VaultSummary.vault_id) == vault_id).with_for_update()
            )
            result = await session.execute(stmt)
            summary = result.scalar_one_or_none()
            if summary is None:
                return await self.regenerate_summary(vault_id)

            if summary.version != current_version:
                logger.info(
                    'Vault summary version changed (%d -> %d) during update, skipping',
                    current_version,
                    summary.version,
                )
                return summary

            summary.narrative = running_narrative
            summary.themes = _themes_to_dicts(running_themes)
            summary.inventory = inventory
            summary.key_entities = key_entities
            summary.version += 1
            summary.notes_incorporated = total_notes

            patch_entry = {
                'action': 'update',
                'notes_added': len(notes_data),
                'timestamp': datetime.now(timezone.utc).isoformat(),
            }
            patch_log = list(summary.patch_log)
            patch_log.append(patch_entry)
            if len(patch_log) > self.config.max_patch_log:
                patch_log = patch_log[-self.config.max_patch_log :]
            summary.patch_log = patch_log

            session.add(summary)

            # Mark ALL active notes in the vault with the new version.
            # The updated summary incorporates everything (previous summary + delta),
            # so every active note is now incorporated.  Marking only the delta
            # notes would leave previously-incorporated notes behind the new
            # version, causing an infinite stale→update loop.
            mark_all_stmt = (
                sa_update(Note)
                .where(col(Note.vault_id) == vault_id)
                .where(col(Note.status) == 'active')
                .values(summary_version_incorporated=summary.version)
            )
            await session.execute(mark_all_stmt)

            await session.commit()
            await session.refresh(summary)
            return summary

    async def regenerate_summary(self, vault_id: UUID) -> VaultSummary:
        """Full regeneration of vault summary from all active notes.

        Uses 3-tier strategy based on **token budget** of the serialized
        metadata payload:
        - Tier 1 (≤ max_batch_tokens): single LLM call
        - Tier 2 (≤ max_batch_tokens * 10): two-pass theme clustering
        - Tier 3 (> max_batch_tokens * 10): recursive hierarchical merge
        """
        async with self.metastore.session() as session:
            notes_data, _included_ids, all_fetched_ids = await self._fetch_note_metadata(
                session, vault_id
            )

        note_count = len(notes_data)
        if note_count == 0:
            return await self._create_empty_summary(vault_id)

        # Compute structured fields (pure SQL, no LLM)
        inventory = await self._compute_inventory(vault_id)
        key_entities = await self._compute_key_entities(vault_id)

        # LLM-synthesized fields
        total_payload_tokens = _estimate_tokens(json.dumps(notes_data, default=str))
        max_tokens = self.config.max_batch_tokens

        if total_payload_tokens <= max_tokens:
            narrative, themes = await self._tier1_single_call(notes_data, note_count)
        elif total_payload_tokens <= max_tokens * 10:
            narrative, themes = await self._tier2_two_pass(notes_data, note_count)
        else:
            narrative, themes = await self._tier3_hierarchical(notes_data, note_count)

        # Persist
        async with self.metastore.session() as session:
            stmt = select(VaultSummary).where(col(VaultSummary.vault_id) == vault_id)
            result = await session.execute(stmt)
            summary = result.scalar_one_or_none()

            if summary is None:
                summary = VaultSummary(vault_id=vault_id)
                session.add(summary)

            summary.narrative = narrative
            summary.themes = themes
            summary.inventory = inventory
            summary.key_entities = key_entities
            summary.version = (summary.version or 0) + 1
            summary.notes_incorporated = note_count
            summary.patch_log = []
            summary.needs_regeneration = False

            # Mark ALL active notes with the new version
            mark_all_stmt = (
                sa_update(Note)
                .where(col(Note.vault_id) == vault_id)
                .where(col(Note.status) == 'active')
                .values(summary_version_incorporated=summary.version)
            )
            await session.execute(mark_all_stmt)

            await session.commit()
            await session.refresh(summary)
            return summary

    # ------------------------------------------------------------------ #
    # Computed fields (no LLM)
    # ------------------------------------------------------------------ #

    async def _compute_inventory(self, vault_id: UUID) -> dict[str, Any]:
        """Compute content inventory from DB aggregates. No LLM calls."""
        async with self.metastore.session() as session:
            now = datetime.now(timezone.utc)

            # Total active notes
            total_stmt = (
                select(func.count())
                .select_from(Note)
                .where(col(Note.vault_id) == vault_id)
                .where(col(Note.status) == 'active')
            )
            total_notes = (await session.execute(total_stmt)).scalar() or 0

            # Total entities (via unit_entities join)
            entity_count_stmt = select(func.count(func.distinct(UnitEntity.entity_id))).where(
                col(UnitEntity.vault_id) == vault_id
            )
            total_entities = (await session.execute(entity_count_stmt)).scalar() or 0

            # Date range
            date_range_stmt = (
                select(func.min(Note.publish_date), func.max(Note.publish_date))
                .where(col(Note.vault_id) == vault_id)
                .where(col(Note.status) == 'active')
                .where(col(Note.publish_date).isnot(None))
            )
            date_row = (await session.execute(date_range_stmt)).one_or_none()
            date_range: dict[str, str | None] = {'earliest': None, 'latest': None}
            if date_row and date_row[0]:
                date_range = {
                    'earliest': date_row[0].isoformat() if date_row[0] else None,
                    'latest': date_row[1].isoformat() if date_row[1] else None,
                }

            # Template distribution, source domains, tags — from doc_metadata JSONB
            meta_stmt = (
                select(Note.doc_metadata)
                .where(col(Note.vault_id) == vault_id)
                .where(col(Note.status) == 'active')
                .where(col(Note.doc_metadata).isnot(None))
            )
            meta_rows = (await session.execute(meta_stmt)).all()

            template_counts: Counter[str] = Counter()
            domain_counts: Counter[str] = Counter()
            tag_counts: Counter[str] = Counter()

            for (doc_meta,) in meta_rows:
                if not isinstance(doc_meta, dict):
                    continue
                template = doc_meta.get('template', '')
                if template:
                    template_counts[template] += 1

                source_uri = doc_meta.get('source_uri', '')
                if source_uri:
                    try:
                        domain = urlparse(source_uri).netloc
                        if domain:
                            domain_counts[domain] += 1
                    except Exception:
                        pass

                for tag in doc_meta.get('tags', []):
                    if isinstance(tag, str) and tag and tag.lower() not in _SYSTEM_TAGS:
                        tag_counts[tag] += 1

            # Recent activity
            recent_7d = (
                await session.execute(
                    select(func.count())
                    .select_from(Note)
                    .where(col(Note.vault_id) == vault_id)
                    .where(col(Note.status) == 'active')
                    .where(col(Note.created_at) >= now - timedelta(days=7))
                )
            ).scalar() or 0

            recent_30d = (
                await session.execute(
                    select(func.count())
                    .select_from(Note)
                    .where(col(Note.vault_id) == vault_id)
                    .where(col(Note.status) == 'active')
                    .where(col(Note.created_at) >= now - timedelta(days=30))
                )
            ).scalar() or 0

        return {
            'total_notes': total_notes,
            'total_entities': total_entities,
            'date_range': date_range,
            'by_template': dict(template_counts.most_common(10)),
            'by_source_domain': dict(domain_counts.most_common(10)),
            'top_tags': dict(tag_counts.most_common(15)),
            'recent_activity': {'7d': recent_7d, '30d': recent_30d},
        }

    async def _compute_key_entities(self, vault_id: UUID, limit: int = 10) -> list[dict[str, Any]]:
        """Fetch top entities by vault-scoped mention count. No LLM calls."""
        async with self.metastore.session() as session:
            vault_mentions = func.count(UnitEntity.unit_id).label('vault_mentions')
            stmt = (
                select(
                    Entity.canonical_name,
                    Entity.entity_type,
                    vault_mentions,
                )
                .join(UnitEntity, col(UnitEntity.entity_id) == col(Entity.id))
                .where(col(UnitEntity.vault_id) == vault_id)
                .group_by(col(Entity.id))
                .order_by(vault_mentions.desc())
                .limit(limit)
            )
            rows = (await session.execute(stmt)).all()

        return [
            {
                'name': row.canonical_name,
                'type': row.entity_type or 'unknown',
                'mention_count': row.vault_mentions,
            }
            for row in rows
        ]

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    async def _fetch_note_metadata(
        self,
        session: Any,
        vault_id: UUID,
        summary_version: int | None = None,
    ) -> tuple[list[dict[str, Any]], list[UUID], list[UUID]]:
        """Fetch rich note metadata for vault summarization.

        Joins Note with Chunk to get BlockSummaryDTO data (topic + key_points)
        stored in ``Chunk.summary`` JSONB column. When ``summary_version`` is
        provided, only returns notes not yet incorporated (delta mode).

        Returns ``(metadata_list, included_ids, all_fetched_ids)``.
        ``included_ids`` are notes with meaningful content (1:1 with metadata).
        ``all_fetched_ids`` includes notes that were fetched but skipped due to
        having no meaningful content — callers should mark *all* fetched IDs so
        that empty notes don't perpetually appear as pending.
        """
        # Step 1: Fetch notes
        note_stmt = (
            select(Note.id, Note.title, Note.description, Note.publish_date, Note.doc_metadata)
            .where(col(Note.vault_id) == vault_id)
            .where(col(Note.status) == 'active')
            .order_by(col(Note.created_at))
        )
        if summary_version is not None:
            note_stmt = note_stmt.where(
                (col(Note.summary_version_incorporated).is_(None))
                | (col(Note.summary_version_incorporated) < summary_version)
            )
        notes = (await session.execute(note_stmt)).all()

        if not notes:
            return [], [], []

        note_ids = [n.id for n in notes]

        # Step 2: Batch-fetch chunk summaries (BlockSummaryDTO: {topic, key_points})
        chunk_stmt = (
            select(Chunk.note_id, Chunk.summary)
            .where(col(Chunk.note_id).in_(note_ids))
            .where(col(Chunk.status) == ContentStatus.ACTIVE)
            .where(col(Chunk.summary).isnot(None))
            .order_by(col(Chunk.note_id), col(Chunk.chunk_index))
        )
        chunks = (await session.execute(chunk_stmt)).all()
        summaries_by_note: dict[UUID, list[dict[str, Any]]] = {}
        for c in chunks:
            if isinstance(c.summary, dict):
                summaries_by_note.setdefault(c.note_id, []).append(c.summary)

        # Step 3: Build rich metadata
        results: list[dict[str, Any]] = []
        result_ids: list[UUID] = []
        for n in notes:
            title = n.title or 'Untitled'
            doc_meta = n.doc_metadata or {}
            summaries = summaries_by_note.get(n.id, [])

            # Skip notes with no meaningful content
            if title == 'Untitled' and not n.description and not summaries:
                continue

            source_uri = doc_meta.get('source_uri', '')
            source_domain = ''
            if source_uri:
                try:
                    source_domain = urlparse(source_uri).netloc
                except Exception:
                    pass

            results.append(
                {
                    'title': title,
                    'publish_date': n.publish_date.isoformat() if n.publish_date else None,
                    'tags': doc_meta.get('tags', []),
                    'template': doc_meta.get('template', ''),
                    'author': doc_meta.get('author', ''),
                    'source_domain': source_domain,
                    'description': n.description or '',
                    'summaries': summaries,
                }
            )
            result_ids.append(n.id)
        return results, result_ids, note_ids

    async def _create_empty_summary(self, vault_id: UUID) -> VaultSummary:
        """Create or reset to an empty summary for a vault with no notes."""
        async with self.metastore.session() as session:
            stmt = select(VaultSummary).where(col(VaultSummary.vault_id) == vault_id)
            result = await session.execute(stmt)
            summary = result.scalar_one_or_none()

            if summary is None:
                summary = VaultSummary(vault_id=vault_id)
                session.add(summary)

            summary.narrative = 'This vault is empty.'
            summary.themes = []
            summary.inventory = {'total_notes': 0, 'total_entities': 0}
            summary.key_entities = []
            summary.version = (summary.version or 0) + 1
            summary.notes_incorporated = 0
            summary.patch_log = []

            await session.commit()
            await session.refresh(summary)
            return summary

    async def _tier1_single_call(
        self,
        notes_data: list[dict[str, Any]],
        note_count: int,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Tier 1: single LLM call for small payloads."""
        notes = [NoteMetadata(**n) for n in notes_data]
        predictor = dspy.Predict(VaultSummaryFullSignature)
        prediction = await run_dspy_operation(
            lm=self.lm,
            predictor=predictor,
            input_kwargs={
                'notes': notes,
                'vault_note_count': note_count,
                'max_narrative_tokens': self.config.max_narrative_tokens,
            },
            operation_name='vault_summary_full',
        )
        return prediction.narrative, _themes_to_dicts(prediction.themes)

    async def _tier2_two_pass(
        self,
        notes_data: list[dict[str, Any]],
        note_count: int,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Tier 2: theme extraction per token-batch + synthesis."""
        batches = _split_into_token_batches(
            notes_data, self.config.max_batch_tokens, self.config.batch_size
        )
        batch_results = await self._extract_themes_from_batches(batches)
        return await self._merge_batch_results(batch_results, note_count)

    async def _tier3_hierarchical(
        self,
        notes_data: list[dict[str, Any]],
        note_count: int,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Tier 3: theme extraction + recursive merge for very large vaults.

        Unlike Tier 2 which merges all batch results in a single LLM call,
        Tier 3 recursively merges batch results in groups until the result set
        is small enough for a single final merge.  This prevents the merge call
        from exceeding the LLM context window.
        """
        batches = _split_into_token_batches(
            notes_data, self.config.max_batch_tokens, self.config.batch_size
        )
        batch_results: list[BatchResult] = await self._extract_themes_from_batches(batches)

        merge_group_size = self.config.batch_size
        while len(batch_results) > merge_group_size:
            merge_groups = [
                batch_results[i : i + merge_group_size]
                for i in range(0, len(batch_results), merge_group_size)
            ]
            merged: list[BatchResult] = []
            for group in merge_groups:
                try:
                    narrative, themes = await self._merge_batch_results(group, note_count)
                    merged.append(
                        BatchResult(
                            batch_index=len(merged),
                            themes=[LLMTheme(**t) for t in themes],
                            batch_summary=narrative,
                        )
                    )
                except Exception:
                    logger.warning(
                        'Failed to merge group in hierarchical pass, skipping', exc_info=True
                    )
            if not merged:
                logger.error('All merge groups failed in hierarchical pass')
                return '', []
            batch_results = merged

        return await self._merge_batch_results(batch_results, note_count)

    async def _extract_themes_from_batches(
        self,
        batches: list[list[dict[str, Any]]],
    ) -> list[BatchResult]:
        """Extract themes from each batch via LLM calls."""
        total_batches = len(batches)
        results: list[BatchResult] = []

        for i, batch in enumerate(batches):
            try:
                notes = [NoteMetadata(**n) for n in batch]
                predictor = dspy.Predict(VaultTopicExtractSignature)
                prediction = await run_dspy_operation(
                    lm=self.lm,
                    predictor=predictor,
                    input_kwargs={
                        'notes': notes,
                        'batch_index': i,
                        'total_batches': total_batches,
                    },
                    operation_name='vault_summary_theme_extract',
                )
                results.append(
                    BatchResult(
                        batch_index=i,
                        themes=prediction.themes,
                        batch_summary=getattr(prediction, 'batch_summary', ''),
                    )
                )
            except Exception:
                logger.warning('Failed to extract themes from batch %d, skipping', i, exc_info=True)
                results.append(
                    BatchResult(
                        batch_index=i,
                        themes=[],
                        batch_summary=f'Batch {i} failed to process.',
                    )
                )

        return results

    async def _merge_batch_results(
        self,
        batch_results: list[BatchResult],
        note_count: int,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Merge batch theme results into a final narrative and theme list."""
        predictor = dspy.Predict(VaultTopicMergeSignature)
        prediction = await run_dspy_operation(
            lm=self.lm,
            predictor=predictor,
            input_kwargs={
                'batch_results': batch_results,
                'vault_note_count': note_count,
            },
            operation_name='vault_summary_theme_merge',
        )
        return prediction.narrative, _themes_to_dicts(prediction.themes)
