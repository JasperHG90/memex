"""Fact processing helpers for the extraction pipeline.

Pure functions for processing extracted facts before persistence:
temporal offset assignment and embedding generation.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from memex_core.memory.extraction import embedding_processor
from memex_core.memory.extraction.models import ExtractedFact, ProcessedFact

logger = logging.getLogger('memex.core.memory.extraction.pipeline.fact_processing')


def add_temporal_offsets(
    facts: list[ExtractedFact],
    seconds_per_fact: int = 10,
) -> None:
    """Add slight time offsets to facts to preserve ordering within a batch.

    Each fact within a content item receives a cumulative offset
    (``fact_position * seconds_per_fact``) applied to its
    ``occurred_start``, ``occurred_end``, and ``mentioned_at`` fields.
    Offsets reset when ``content_index`` changes.

    Args:
        facts: Extracted facts to modify in-place.
        seconds_per_fact: Seconds between consecutive facts.
    """
    current_content_idx = 0
    content_fact_start = 0

    for i, fact in enumerate(facts):
        if fact.content_index != current_content_idx:
            current_content_idx = fact.content_index
            content_fact_start = i

        fact_position = i - content_fact_start
        offset = timedelta(seconds=fact_position * seconds_per_fact)

        if fact.occurred_start:
            fact.occurred_start += offset
        if fact.occurred_end:
            fact.occurred_end += offset
        if fact.mentioned_at:
            fact.mentioned_at += offset


async def process_embeddings(
    embedding_model: embedding_processor.EmbeddingsModel,
    facts: list[ExtractedFact],
) -> list[ProcessedFact]:
    """Augment extracted facts with date-formatted text and generate embeddings.

    Args:
        embedding_model: Model implementing the ``EmbeddingsModel`` protocol.
        facts: Extracted facts to embed.

    Returns:
        List of ``ProcessedFact`` objects with embeddings attached.
    """
    formatted_texts = embedding_processor.format_facts_for_embedding(facts)
    embeddings = await embedding_processor.generate_embeddings_batch(
        embedding_model, formatted_texts
    )

    processed = []
    for fact, emb in zip(facts, embeddings):
        pf = ProcessedFact.from_extracted_fact(fact, emb)
        pf.vault_id = fact.vault_id
        processed.append(pf)
    return processed
