"""Service for vault summary generation and maintenance.

Vault summaries provide a high-level overview of a vault's contents. They are
updated periodically (time-based, not per-note) by checking for new notes since
the last update and feeding their rich metadata to an LLM.

Full regeneration is available on demand via ``regenerate_summary()``.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import dspy
from sqlmodel import col, func, select

from memex_common.config import VaultSummaryConfig
from memex_core.llm import run_dspy_operation
from memex_core.memory.sql_models import Chunk, ContentStatus, Note, VaultSummary
from memex_core.services.vault_summary_signatures import (
    VaultSummaryFullSignature,
    VaultSummaryUpdateSignature,
    VaultTopicExtractSignature,
    VaultTopicMergeSignature,
)
from memex_core.storage.metastore import AsyncBaseMetaStoreEngine

logger = logging.getLogger('memex.core.services.vault_summary')


class VaultSummaryService:
    """Manages vault-level summaries with time-based updates and on-demand regeneration."""

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

    async def is_stale(self, vault_id: UUID) -> bool:
        """Check if the vault summary is stale (new notes exist since last update).

        Returns True if:
        - No summary exists (needs initial generation)
        - Notes have been added since the summary's ``updated_at`` timestamp
        """
        async with self.metastore.session() as session:
            stmt = select(VaultSummary).where(col(VaultSummary.vault_id) == vault_id)
            result = await session.execute(stmt)
            summary = result.scalar_one_or_none()

            if summary is None:
                # Check if the vault has any notes at all
                count_stmt = (
                    select(func.count())
                    .select_from(Note)
                    .where(col(Note.vault_id) == vault_id)
                    .where(col(Note.status) == 'active')
                )
                count = (await session.execute(count_stmt)).scalar() or 0
                return count > 0

            # Count notes added since the summary was last updated
            count_stmt = (
                select(func.count())
                .select_from(Note)
                .where(col(Note.vault_id) == vault_id)
                .where(col(Note.status) == 'active')
                .where(col(Note.created_at) > summary.updated_at)
            )
            count = (await session.execute(count_stmt)).scalar() or 0
            return count > 0

    async def update_summary(self, vault_id: UUID) -> VaultSummary:
        """Update the vault summary with notes added since the last update.

        Fetches only the delta (new notes since ``summary.updated_at``) with
        rich metadata (title, summaries, tags, template, author, source_domain,
        publish_date) and asks the LLM to update the existing summary.

        If no summary exists, falls back to ``regenerate_summary()``.
        """
        async with self.metastore.session() as session:
            stmt = select(VaultSummary).where(col(VaultSummary.vault_id) == vault_id)
            result = await session.execute(stmt)
            summary = result.scalar_one_or_none()

        if summary is None:
            return await self.regenerate_summary(vault_id)

        # Fetch delta notes (new since last update)
        async with self.metastore.session() as session:
            notes_data = await self._fetch_note_metadata(
                session, vault_id, since=summary.updated_at
            )

        if not notes_data:
            return summary  # Nothing new

        # Count total active notes for stats
        async with self.metastore.session() as session:
            total_stmt = (
                select(func.count())
                .select_from(Note)
                .where(col(Note.vault_id) == vault_id)
                .where(col(Note.status) == 'active')
            )
            total_notes = (await session.execute(total_stmt)).scalar() or 0

        # LLM call: update summary with delta
        predictor = dspy.Predict(VaultSummaryUpdateSignature)
        prediction = await run_dspy_operation(
            lm=self.lm,
            predictor=predictor,
            input_kwargs={
                'current_summary': summary.summary,
                'current_topics_json': json.dumps(summary.topics),
                'new_notes_json': json.dumps(notes_data, default=str),
                'vault_stats_json': json.dumps(
                    {
                        'total_notes': total_notes,
                        'new_since_last': len(notes_data),
                        'max_summary_tokens': self.config.max_summary_tokens,
                    }
                ),
            },
            operation_name='vault_summary_update',
        )

        try:
            updated_topics = json.loads(prediction.updated_topics_json)
        except (json.JSONDecodeError, AttributeError):
            updated_topics = summary.topics

        # Persist
        async with self.metastore.session() as session:
            stmt = select(VaultSummary).where(col(VaultSummary.vault_id) == vault_id)
            result = await session.execute(stmt)
            summary = result.scalar_one_or_none()
            if summary is None:
                return await self.regenerate_summary(vault_id)

            summary.summary = prediction.updated_summary
            summary.topics = updated_topics
            summary.version += 1
            summary.notes_incorporated = total_notes
            summary.stats = {'total_notes': total_notes, 'new_since_last': len(notes_data)}

            # Append to patch log
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
            await session.commit()
            await session.refresh(summary)
            return summary

    async def regenerate_summary(self, vault_id: UUID) -> VaultSummary:
        """Full regeneration of vault summary from all notes.

        Uses 3-tier strategy based on note count:
        - Tier 1 (<= batch_size): single LLM call
        - Tier 2 (batch_size < n <= batch_size * 10): two-pass topic clustering
        - Tier 3 (> batch_size * 10): recursive hierarchical with topic consolidation
        """
        async with self.metastore.session() as session:
            notes_data = await self._fetch_note_metadata(session, vault_id)

        note_count = len(notes_data)
        if note_count == 0:
            return await self._create_empty_summary(vault_id)

        batch_size = self.config.batch_size
        if note_count <= batch_size:
            summary_text, topics = await self._tier1_single_call(notes_data, note_count)
        elif note_count <= batch_size * 10:
            summary_text, topics = await self._tier2_two_pass(notes_data, note_count, batch_size)
        else:
            summary_text, topics = await self._tier3_hierarchical(
                notes_data, note_count, batch_size
            )

        # Persist
        async with self.metastore.session() as session:
            stmt = select(VaultSummary).where(col(VaultSummary.vault_id) == vault_id)
            result = await session.execute(stmt)
            summary = result.scalar_one_or_none()

            if summary is None:
                summary = VaultSummary(vault_id=vault_id)
                session.add(summary)

            summary.summary = summary_text
            summary.topics = topics
            summary.stats = {'total_notes': note_count}
            summary.version = (summary.version or 0) + 1
            summary.notes_incorporated = note_count
            summary.patch_log = []  # Reset on full regeneration

            await session.commit()
            await session.refresh(summary)
            return summary

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    async def _fetch_note_metadata(
        self,
        session: Any,
        vault_id: UUID,
        since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch rich note metadata for vault summarization.

        Joins Note with Chunk to get BlockSummaryDTO data (topic + key_points)
        stored in ``Chunk.summary`` JSONB column. When ``since`` is provided,
        only returns notes created after that timestamp (delta mode).
        """
        # Step 1: Fetch notes
        note_stmt = (
            select(Note.id, Note.title, Note.description, Note.publish_date, Note.doc_metadata)
            .where(col(Note.vault_id) == vault_id)
            .where(col(Note.status) == 'active')
            .order_by(col(Note.created_at))
        )
        if since:
            note_stmt = note_stmt.where(col(Note.created_at) > since)
        notes = (await session.execute(note_stmt)).all()

        if not notes:
            return []

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
        return results

    async def _create_empty_summary(self, vault_id: UUID) -> VaultSummary:
        """Create or reset to an empty summary for a vault with no notes."""
        async with self.metastore.session() as session:
            stmt = select(VaultSummary).where(col(VaultSummary.vault_id) == vault_id)
            result = await session.execute(stmt)
            summary = result.scalar_one_or_none()

            if summary is None:
                summary = VaultSummary(vault_id=vault_id)
                session.add(summary)

            summary.summary = 'This vault is empty.'
            summary.topics = []
            summary.stats = {'total_notes': 0}
            summary.version = (summary.version or 0) + 1
            summary.notes_incorporated = 0
            summary.last_note_id = None
            summary.patch_log = []

            await session.commit()
            await session.refresh(summary)
            return summary

    async def _tier1_single_call(
        self,
        notes_data: list[dict[str, Any]],
        note_count: int,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Tier 1: single LLM call for small vaults."""
        predictor = dspy.Predict(VaultSummaryFullSignature)
        prediction = await run_dspy_operation(
            lm=self.lm,
            predictor=predictor,
            input_kwargs={
                'notes_json': json.dumps(notes_data, default=str),
                'vault_note_count': note_count,
                'max_summary_tokens': self.config.max_summary_tokens,
            },
            operation_name='vault_summary_full',
        )
        try:
            topics = json.loads(prediction.topics_json)
        except (json.JSONDecodeError, AttributeError):
            topics = []
        return prediction.summary, topics

    async def _tier2_two_pass(
        self,
        notes_data: list[dict[str, Any]],
        note_count: int,
        batch_size: int,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Tier 2: topic extraction per batch + synthesis."""
        batches = [notes_data[i : i + batch_size] for i in range(0, len(notes_data), batch_size)]
        batch_results = await self._extract_topics_from_batches(batches)
        return await self._merge_batch_results(batch_results, note_count)

    async def _tier3_hierarchical(
        self,
        notes_data: list[dict[str, Any]],
        note_count: int,
        batch_size: int,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Tier 3: topic extraction + recursive merge for very large vaults.

        Unlike Tier 2 which merges all batch results in a single LLM call,
        Tier 3 recursively merges batch results in groups of ``batch_size``
        until the result set is small enough for a single final merge.  This
        prevents the merge call from exceeding the LLM context window when
        there are hundreds of topic batches.
        """
        batches = [notes_data[i : i + batch_size] for i in range(0, len(notes_data), batch_size)]
        batch_results = await self._extract_topics_from_batches(batches)

        # Recursively merge until batch_results fits in a single merge call
        while len(batch_results) > batch_size:
            merge_groups = [
                batch_results[i : i + batch_size] for i in range(0, len(batch_results), batch_size)
            ]
            merged: list[dict[str, Any]] = []
            for group in merge_groups:
                try:
                    summary_text, topics = await self._merge_batch_results(group, note_count)
                    merged.append(
                        {
                            'batch_index': len(merged),
                            'topics': topics,
                            'batch_summary': summary_text,
                        }
                    )
                except Exception:
                    logger.warning(
                        'Failed to merge group in hierarchical pass, skipping', exc_info=True
                    )
            batch_results = merged

        return await self._merge_batch_results(batch_results, note_count)

    async def _extract_topics_from_batches(
        self,
        batches: list[list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        """Extract topics from each batch via LLM calls."""
        total_batches = len(batches)
        results: list[dict[str, Any]] = []

        for i, batch in enumerate(batches):
            try:
                predictor = dspy.Predict(VaultTopicExtractSignature)
                prediction = await run_dspy_operation(
                    lm=self.lm,
                    predictor=predictor,
                    input_kwargs={
                        'notes_json': json.dumps(batch, default=str),
                        'batch_index': i,
                        'total_batches': total_batches,
                    },
                    operation_name='vault_summary_topic_extract',
                )
                try:
                    topics = json.loads(prediction.topics_json)
                except (json.JSONDecodeError, AttributeError):
                    topics = []
                results.append(
                    {
                        'batch_index': i,
                        'topics': topics,
                        'batch_summary': getattr(prediction, 'batch_summary', ''),
                    }
                )
            except Exception:
                logger.warning('Failed to extract topics from batch %d, skipping', i, exc_info=True)
                results.append(
                    {
                        'batch_index': i,
                        'topics': [],
                        'batch_summary': f'Batch {i} failed to process.',
                    }
                )

        return results

    async def _merge_batch_results(
        self,
        batch_results: list[dict[str, Any]],
        note_count: int,
    ) -> tuple[str, list[dict[str, Any]]]:
        """Merge batch topic results into a final summary and topic list."""
        predictor = dspy.Predict(VaultTopicMergeSignature)
        prediction = await run_dspy_operation(
            lm=self.lm,
            predictor=predictor,
            input_kwargs={
                'batch_topics_json': json.dumps(batch_results),
                'vault_note_count': note_count,
            },
            operation_name='vault_summary_topic_merge',
        )
        try:
            topics = json.loads(prediction.topics_json)
        except (json.JSONDecodeError, AttributeError):
            topics = []
        return prediction.summary, topics
