"""
Embedding processing for retain pipeline.

Handles augmenting fact texts with temporal information and generating embeddings.
"""

import asyncio
import logging
from typing import Sequence

from memex_core.memory.extraction.models import ExtractedFact, ProcessedFact
from memex_core.memory.models.protocols import EmbeddingsModel

from memex_core.memory.formatting import format_for_embedding


logger = logging.getLogger('memex.core.memory.extraction.embedding_processor')


def format_facts_for_embedding(
    facts: Sequence[ExtractedFact | ProcessedFact],
) -> list[str]:
    """

    Format facts for embedding using the standardized "Type (Context): Text" schema.



    Args:

        facts: List of ExtractedFact or ProcessedFact objects



    Returns:

        List of formatted strings matching the embedding model's training data.

    """

    formatted_texts = []

    for fact in facts:
        formatted_texts.append(
            format_for_embedding(
                text=fact.fact_text,
                fact_type=fact.fact_type,
                context=fact.context,
            )
        )

    return formatted_texts


def generate_embedding(embeddings_backend: EmbeddingsModel, text: str) -> list[float]:
    """
    Generate embedding for text using the provided embeddings backend.

    Args:
        embeddings_backend: Embeddings instance to use for encoding
        text: Text to embed

    Returns:
        Embedding vector (dimension depends on embeddings backend)
    """
    try:
        # encode expects a list, returns a numpy array (or list of arrays)
        embeddings = embeddings_backend.encode([text])
        # Return the first vector, converted to list if it's numpy
        vector = embeddings[0]
        if hasattr(vector, 'tolist'):
            return vector.tolist()
        return list(vector)
    except (ValueError, RuntimeError, OSError, TypeError) as e:
        logger.error(f'Failed to generate embedding: {str(e)}')
        raise RuntimeError(f'Failed to generate embedding: {str(e)}') from e


async def generate_embeddings_batch(
    embeddings_backend: EmbeddingsModel, texts: list[str]
) -> list[list[float]]:
    """
    Generate embeddings for multiple texts using the provided embeddings backend.

    Runs the embedding generation in a thread pool to avoid blocking the event loop
    for CPU-bound operations.

    Args:
        embeddings_backend: Embeddings instance to use for encoding
        texts: List of texts to embed

    Returns:
        List of embeddings in same order as input texts
    """
    if not texts:
        return []

    try:
        # Run embeddings in thread pool to avoid blocking event loop
        loop = asyncio.get_event_loop()
        embeddings = await loop.run_in_executor(
            None,  # Use default thread pool
            embeddings_backend.encode,
            texts,
        )

        # Convert numpy arrays to lists if necessary
        result = []
        for emb in embeddings:
            if hasattr(emb, 'tolist'):
                result.append(emb.tolist())
            else:
                result.append(list(emb))
        return result
    except (ValueError, RuntimeError, OSError, TypeError) as e:
        logger.error(f'Failed to generate batch embeddings: {str(e)}')
        raise RuntimeError(f'Failed to generate batch embeddings: {str(e)}') from e
