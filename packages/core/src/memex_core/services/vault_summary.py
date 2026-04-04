"""Service for vault summary generation and maintenance."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import dspy
from sqlmodel import col, select

from memex_common.config import VaultSummaryConfig
from memex_core.llm import run_dspy_operation
from memex_core.memory.sql_models import Note, VaultSummary
from memex_core.services.vault_summary_signatures import (
    VaultSummaryFullSignature,
    VaultSummaryPatchSignature,
    VaultTopicExtractSignature,
    VaultTopicMergeSignature,
)
from memex_core.storage.metastore import AsyncBaseMetaStoreEngine

logger = logging.getLogger('memex.core.services.vault_summary')


class VaultSummaryService:
    """Manages vault-level summaries with patch-on-ingest and full regeneration."""

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

    async def patch_summary(
        self,
        vault_id: UUID,
        note_id: UUID,
        title: str,
        description: str,
    ) -> VaultSummary:
        """Patch the vault summary to incorporate a single new note (O(1) per note)."""
        async with self.metastore.session() as session:
            stmt = select(VaultSummary).where(col(VaultSummary.vault_id) == vault_id)
            result = await session.execute(stmt)
            summary = result.scalar_one_or_none()

            if summary is None:
                # First note in the vault — create a new summary directly
                summary = VaultSummary(
                    vault_id=vault_id,
                    summary=description or title,
                    topics=[],
                    stats={'total_notes': 1},
                    version=1,
                    notes_incorporated=1,
                    last_note_id=note_id,
                    patch_log=[
                        {
                            'note_id': str(note_id),
                            'action': 'initial',
                            'timestamp': datetime.now(timezone.utc).isoformat(),
                        }
                    ],
                )
                session.add(summary)
                await session.commit()
                await session.refresh(summary)
                return summary

            # Patch existing summary with LLM
            predictor = dspy.Predict(VaultSummaryPatchSignature)
            prediction = await run_dspy_operation(
                lm=self.lm,
                predictor=predictor,
                input_kwargs={
                    'current_summary': summary.summary,
                    'current_topics_json': json.dumps(summary.topics),
                    'current_stats_json': json.dumps(summary.stats),
                    'note_title': title,
                    'note_description': description,
                },
                operation_name='vault_summary_patch',
            )

            # Parse LLM output
            try:
                updated_topics = json.loads(prediction.updated_topics_json)
            except (json.JSONDecodeError, AttributeError):
                updated_topics = summary.topics

            # Update summary
            summary.summary = prediction.updated_summary
            summary.topics = updated_topics
            summary.version += 1
            summary.notes_incorporated += 1
            summary.last_note_id = note_id
            summary.stats = {
                **summary.stats,
                'total_notes': summary.notes_incorporated,
            }

            # Append to patch log, bounded to max_patch_log entries
            patch_entry = {
                'note_id': str(note_id),
                'action': 'patch',
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
        - Tier 3 (> batch_size * 10): three-pass hierarchical with topic consolidation
        """
        # Fetch all note (title, description) pairs
        async with self.metastore.session() as session:
            stmt = (
                select(Note.id, Note.title, Note.description)
                .where(col(Note.vault_id) == vault_id)
                .where(col(Note.status) == 'active')
                .order_by(col(Note.created_at))
            )
            result = await session.execute(stmt)
            notes = result.all()

        note_count = len(notes)
        if note_count == 0:
            return await self._create_empty_summary(vault_id)

        notes_data = [
            {
                'title': n.title or 'Untitled',
                'description': n.description or '',
            }
            for n in notes
        ]
        last_note_id = notes[-1].id

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
            summary.last_note_id = last_note_id
            summary.patch_log = []  # Reset on full regeneration

            await session.commit()
            await session.refresh(summary)
            return summary

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
                'notes_json': json.dumps(notes_data),
                'vault_note_count': note_count,
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
        """Tier 3: topic extraction + merge + synthesis for very large vaults."""
        batches = [notes_data[i : i + batch_size] for i in range(0, len(notes_data), batch_size)]
        batch_results = await self._extract_topics_from_batches(batches)
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
                        'notes_json': json.dumps(batch),
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
