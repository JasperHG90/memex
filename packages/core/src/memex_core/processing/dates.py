"""
DSPy-based document date extraction for content without explicit metadata dates.

Uses a lightweight LLM call to identify the most likely document date from the
first ~2000 characters of a document (headers, bylines, date references, etc.).
"""

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import dspy
from dateutil import parser as dateutil_parser
from pydantic import BaseModel, Field
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_core.llm import run_dspy_operation

logger = logging.getLogger('memex.core.processing.dates')

# Maximum characters to send to the LLM for date extraction
_HEADER_CHAR_LIMIT = 2000


class DateExtraction(BaseModel):
    """Structured result from LLM date extraction."""

    reasoning: str = Field(description='Step-by-step reasoning about date clues in the text.')
    original_text: str = Field(description='The exact date-related text found in the document.')
    normalized_date: str = Field(
        description='The extracted date in ISO 8601 format (YYYY-MM-DD). Empty string if none found.',
    )
    date_type: str = Field(
        description='Type of date found: "publication", "creation", "event", "unknown".',
    )
    confidence: float = Field(
        description='Confidence score between 0.0 and 1.0.',
    )
    is_explicit: bool = Field(
        description='Whether the date was explicitly stated (true) vs inferred from context (false).',
    )


class ExtractNoteDate(dspy.Signature):
    """Analyze the beginning of a document to extract its most likely creation or publication date.

    Look for explicit dates in headers, bylines, metadata, timestamps, or date references.
    If no date is found, set normalized_date to an empty string and confidence to 0.0.
    """

    document_header: str = dspy.InputField(
        desc='The first ~2000 characters of the document to analyze for date clues.',
    )
    extracted_date: DateExtraction = dspy.OutputField(
        desc='The extracted date information.',
    )


async def extract_document_date(
    text: str,
    lm: dspy.LM,
    session: AsyncSession | None = None,
    vault_id: UUID | None = None,
) -> datetime | None:
    """Extract the document date from text using a lightweight DSPy LLM call.

    Args:
        text: The full document text (only the first ~2000 chars will be used).
        lm: The DSPy language model instance.
        session: Optional DB session for token usage logging.
        vault_id: Optional vault ID for token usage logging.

    Returns:
        A timezone-aware datetime if a date was extracted with sufficient confidence,
        or None if no date could be determined.
    """
    header = text[:_HEADER_CHAR_LIMIT]
    if not header.strip():
        return None

    predictor = dspy.ChainOfThought(ExtractNoteDate)

    try:
        prediction, _ = await run_dspy_operation(
            lm=lm,
            predictor=predictor,
            input_kwargs={'document_header': header},
            session=session,
            context_metadata={'operation': 'date_extraction'},
            vault_id=vault_id,
        )
    except Exception as e:
        logger.warning('LLM date extraction failed: %s', e, exc_info=True)
        return None

    extraction: Any = prediction.extracted_date
    if not extraction or not extraction.normalized_date:
        return None

    # Require minimum confidence for LLM-extracted dates
    if extraction.confidence < 0.5:
        logger.debug(
            f'Low-confidence date extraction ({extraction.confidence}): '
            f'{extraction.normalized_date!r}'
        )
        return None

    return _parse_iso_date(extraction.normalized_date)


def _parse_iso_date(date_str: str) -> datetime | None:
    """Parse an ISO date string into a timezone-aware UTC datetime."""
    try:
        parsed = dateutil_parser.parse(date_str)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (ValueError, OverflowError):
        logger.warning(f'Could not parse LLM-extracted date: {date_str!r}')
        return None
