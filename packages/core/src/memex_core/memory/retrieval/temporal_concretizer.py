"""LLM-assisted temporal concretization for ambiguous date expressions.

Falls back to LLM when the regex-based temporal extraction in
``temporal_extraction.py`` cannot resolve a temporal constraint but the query
text still *sounds* temporal (e.g. "during the onboarding", "when we launched").
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, tzinfo as _tzinfo, timezone

import dspy

from memex_core.llm import run_dspy_operation

logger = logging.getLogger('memex.core.memory.retrieval.temporal_concretizer')

# Words/phrases that hint the user has a temporal intent even though
# the regex extractor found nothing concrete.
_AMBIGUOUS_TEMPORAL_TRIGGERS = re.compile(
    r'\b(?:'
    r'during|around\s+the\s+time|when\s+we|when\s+I|when\s+the|'
    r'before\s+the|after\s+the|since\s+the|until\s+the|'
    r'at\s+the\s+time\s+of|in\s+the\s+(?:early|late|middle)\s+(?:part|stage)|'
    r'back\s+when|the\s+(?:week|month|year|day|period|sprint|phase)\s+'
    r'(?:of|before|after|when|that)|'
    r'(?:first|second|third|last)\s+(?:quarter|half|phase|sprint|semester|trimester)'
    r')\b',
    re.IGNORECASE,
)


class TemporalConcretizationSignature(dspy.Signature):
    """Extract an absolute date range from a query with an ambiguous temporal expression.

    Given a natural-language query and a reference date (the "current" date),
    return the most likely absolute start and end dates (ISO-8601, UTC) that the
    user is referring to, or "none" if the query has no meaningful temporal
    constraint.
    """

    query: str = dspy.InputField(desc='The user query containing an ambiguous temporal expression.')
    reference_date: str = dspy.InputField(
        desc='The current date in ISO-8601 format (e.g. "2024-06-15T12:00:00+00:00").'
    )
    start_date: str = dspy.OutputField(
        desc=(
            'Start of the date range in ISO-8601 format (e.g. "2024-03-01T00:00:00+00:00"), '
            'or "none" if no temporal constraint can be inferred.'
        )
    )
    end_date: str = dspy.OutputField(
        desc=(
            'End of the date range in ISO-8601 format (e.g. "2024-03-31T23:59:59+00:00"), '
            'or "none" if no temporal constraint can be inferred.'
        )
    )


def has_ambiguous_temporal_expression(query: str) -> bool:
    """Return True if the query contains temporal-sounding language the regex missed."""
    return bool(_AMBIGUOUS_TEMPORAL_TRIGGERS.search(query))


class TemporalConcretizer:
    """Uses an LLM to resolve ambiguous temporal expressions into date ranges."""

    def __init__(self, lm: dspy.LM) -> None:
        self.lm = lm
        self.predictor = dspy.Predict(TemporalConcretizationSignature)

    async def concretize(
        self,
        query: str,
        reference_date: datetime | None = None,
    ) -> tuple[datetime, datetime] | None:
        """Attempt to extract an absolute date range via the LLM.

        Returns ``(start, end)`` or ``None`` if the LLM cannot resolve the
        temporal expression.
        """
        if reference_date is None:
            reference_date = datetime.now(timezone.utc)
        elif reference_date.tzinfo is None:
            reference_date = reference_date.replace(tzinfo=timezone.utc)

        ref_str = reference_date.isoformat()

        try:
            result = await run_dspy_operation(
                lm=self.lm,
                predictor=self.predictor,
                input_kwargs={'query': query, 'reference_date': ref_str},
                operation_name='retrieval.temporal_concretization',
            )

            start_str: str = getattr(result, 'start_date', 'none')
            end_str: str = getattr(result, 'end_date', 'none')

            if not start_str or not end_str:
                return None
            if start_str.strip().lower() == 'none' or end_str.strip().lower() == 'none':
                return None

            start = _parse_iso(start_str, reference_date.tzinfo)
            end = _parse_iso(end_str, reference_date.tzinfo)

            if start is None or end is None:
                return None

            # Sanity: start must be before end
            if start >= end:
                logger.debug('Temporal concretization returned start >= end: %s >= %s', start, end)
                return None

            # Bounds: range must not exceed 10 years
            max_range_days = 365 * 10
            if (end - start).days > max_range_days:
                logger.debug('Temporal concretization range exceeds 10 years: %s to %s', start, end)
                return None

            # Bounds: neither date more than 100 years from reference_date
            max_distance_days = 365 * 100
            for dt, label in [(start, 'start'), (end, 'end')]:
                if abs((dt - reference_date).days) > max_distance_days:
                    logger.debug(
                        'Temporal concretization %s date too far from reference: %s vs %s',
                        label,
                        dt,
                        reference_date,
                    )
                    return None

            return (start, end)

        except (ValueError, RuntimeError, OSError, KeyError) as e:
            logger.warning('Temporal concretization failed: %s', e)
            return None


def _parse_iso(value: str, tz: _tzinfo | None = None) -> datetime | None:
    """Best-effort ISO-8601 parse, returning *None* on failure."""
    value = value.strip().strip('"\'')
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None and tz is not None:
            dt = dt.replace(tzinfo=tz)
        return dt
    except (ValueError, TypeError):
        return None
