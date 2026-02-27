"""Tests for MemoryUnitDTO confidence fields and enriched_text property."""

import datetime as dt
from uuid import uuid4

from memex_common.schemas import MemoryUnitDTO
from memex_common.types import FactTypes


def _make_dto(
    text: str = 'qmd is software',
    confidence_alpha: float | None = None,
    confidence_beta: float | None = None,
    mentioned_at: dt.datetime | None = None,
    occurred_start: dt.datetime | None = None,
) -> MemoryUnitDTO:
    return MemoryUnitDTO(
        id=uuid4(),
        note_id=uuid4(),
        text=text,
        fact_type=FactTypes.OPINION,
        vault_id=uuid4(),
        metadata={},
        confidence_alpha=confidence_alpha,
        confidence_beta=confidence_beta,
        mentioned_at=mentioned_at,
        occurred_start=occurred_start,
    )


class TestConfidenceScore:
    def test_returns_mean_of_beta(self):
        dto = _make_dto(confidence_alpha=8.0, confidence_beta=2.0)
        assert dto.confidence_score == 0.8

    def test_returns_none_when_missing(self):
        dto = _make_dto()
        assert dto.confidence_score is None

    def test_returns_none_when_partial(self):
        dto = _make_dto(confidence_alpha=1.0)
        assert dto.confidence_score is None

    def test_handles_zero_total(self):
        dto = _make_dto(confidence_alpha=0.0, confidence_beta=0.0)
        assert dto.confidence_score is None


class TestEnrichedText:
    def test_contradicted_prefix(self):
        dto = _make_dto(
            confidence_alpha=1.0,
            confidence_beta=9.0,
            occurred_start=dt.datetime(2026, 2, 1, tzinfo=dt.timezone.utc),
        )
        result = dto.enriched_text
        assert result.startswith('[CONTRADICTED]')
        assert '[2026-02-01]' in result
        assert 'qmd is software' in result

    def test_no_prefix_when_high_confidence(self):
        dto = _make_dto(
            confidence_alpha=8.0,
            confidence_beta=2.0,
            occurred_start=dt.datetime(2026, 2, 1, tzinfo=dt.timezone.utc),
        )
        result = dto.enriched_text
        assert '[CONTRADICTED]' not in result
        assert '[2026-02-01] qmd is software' in result

    def test_no_prefix_when_no_confidence(self):
        dto = _make_dto(occurred_start=dt.datetime(2026, 2, 1, tzinfo=dt.timezone.utc))
        result = dto.enriched_text
        assert '[CONTRADICTED]' not in result

    def test_uses_mentioned_at_over_occurred_start(self):
        dto = _make_dto(
            mentioned_at=dt.datetime(2026, 3, 15, tzinfo=dt.timezone.utc),
            occurred_start=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
        )
        assert '[2026-03-15]' in dto.enriched_text

    def test_unknown_date_when_no_dates(self):
        dto = _make_dto()
        assert '[Unknown]' in dto.enriched_text
