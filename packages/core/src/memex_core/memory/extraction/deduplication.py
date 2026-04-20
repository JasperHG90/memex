"""
Deduplication logic for retain pipeline.

Checks for duplicate facts using semantic similarity and temporal proximity.
"""

import logging
from collections import defaultdict
from datetime import datetime, timezone
from uuid import UUID

from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.memory.extraction.models import ProcessedFact
from memex_core.config import GLOBAL_VAULT_ID

logger = logging.getLogger('memex.core.memory.extraction.deduplication')


async def check_duplicates_batch(
    session: AsyncSession,
    facts: list[ProcessedFact],
    duplicate_checker_fn,
    vault_id: UUID = GLOBAL_VAULT_ID,
    event_date: datetime | None = None,
) -> list[bool]:
    """
    Check which facts are duplicates using batched time-window queries.

    Groups facts by 12-hour time buckets to efficiently check for duplicates
    within a 24-hour window. Scoped to the provided vault_id.

    Args:
        session: Database session
        facts: List of ProcessedFact objects to check
        duplicate_checker_fn: Async function(session, texts, embeddings, date, window_hours, vault_id)
                              that returns List[bool] indicating duplicates
        vault_id: Vault ID to scope the check.

    Returns:
        List of boolean flags (same length as facts) indicating if each fact is a duplicate
    """
    if not facts:
        return []

    # Group facts by event_date (rounded to 12-hour buckets)
    # We also include vault_id in the grouping if it varies across facts,
    # but ExtractionEngine usually processes one vault at a time.
    # To be robust, we group by (bucket_date, fact_vault_id)
    time_vault_buckets = defaultdict(list)
    _fallback = event_date or datetime.now(timezone.utc)

    for idx, fact in enumerate(facts):
        fact_date = fact.occurred_start or fact.mentioned_at or _fallback

        bucket_date = fact_date.replace(
            hour=(fact_date.hour // 12) * 12, minute=0, second=0, microsecond=0
        )

        # Use provided vault_id OR fact's own vault_id
        effective_vault_id = vault_id if vault_id != GLOBAL_VAULT_ID else fact.vault_id

        time_vault_buckets[(bucket_date, effective_vault_id)].append((idx, fact))

    # Process each bucket in batch
    all_is_duplicate = [False] * len(facts)

    for (bucket_date, b_vault_id), bucket_items in time_vault_buckets.items():
        indices = [item[0] for item in bucket_items]
        texts = [item[1].fact_text for item in bucket_items]
        embeddings = [item[1].embedding for item in bucket_items]

        # Check duplicates for this time bucket
        dup_flags = await duplicate_checker_fn(
            session,
            texts,
            embeddings,
            bucket_date,
            window_hours=24,
            vault_ids=[b_vault_id] if b_vault_id else None,
        )

        # Map results back to original indices
        for idx, is_dup in zip(indices, dup_flags):
            all_is_duplicate[idx] = is_dup

    return all_is_duplicate


def filter_duplicates(
    facts: list[ProcessedFact], is_duplicate_flags: list[bool]
) -> list[ProcessedFact]:
    """
    Filter out duplicate facts based on duplicate flags.

    Args:
        facts: List of ProcessedFact objects
        is_duplicate_flags: Boolean flags indicating which facts are duplicates

    Returns:
        List of non-duplicate facts
    """
    if len(facts) != len(is_duplicate_flags):
        raise ValueError(
            f'Mismatch between facts ({len(facts)}) and flags ({len(is_duplicate_flags)})'
        )

    return [fact for fact, is_dup in zip(facts, is_duplicate_flags) if not is_dup]
