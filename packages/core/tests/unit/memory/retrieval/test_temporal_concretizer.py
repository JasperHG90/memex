"""Unit tests for LLM-assisted temporal concretization."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from memex_core.memory.retrieval.temporal_concretizer import (
    TemporalConcretizer,
    has_ambiguous_temporal_expression,
    _parse_iso,
)


REF = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# has_ambiguous_temporal_expression
# ---------------------------------------------------------------------------


class TestHasAmbiguousTemporalExpression:
    """Test the trigger-word detector for LLM fallback."""

    @pytest.mark.parametrize(
        'query',
        [
            'what did we discuss during the onboarding',
            'things that happened when we launched the product',
            'notes from before the migration',
            'what changed after the reorg',
            'back when we used Django',
            'the sprint before the release',
            'first quarter results',
        ],
    )
    def test_triggers_on_ambiguous_temporal_phrases(self, query: str) -> None:
        assert has_ambiguous_temporal_expression(query) is True

    @pytest.mark.parametrize(
        'query',
        [
            'what is the project architecture',
            'explain how the retrieval engine works',
            'list all entities',
            'what happened last week',  # handled by regex, not ambiguous
            'notes from March 2024',  # handled by regex
        ],
    )
    def test_no_trigger_on_concrete_or_non_temporal(self, query: str) -> None:
        assert has_ambiguous_temporal_expression(query) is False


# ---------------------------------------------------------------------------
# _parse_iso helper
# ---------------------------------------------------------------------------


class TestParseIso:
    def test_parses_valid_iso(self) -> None:
        dt = _parse_iso('2024-03-01T00:00:00+00:00')
        assert dt is not None
        assert dt.year == 2024
        assert dt.month == 3

    def test_adds_tz_when_naive(self) -> None:
        dt = _parse_iso('2024-03-01T00:00:00', tz=timezone.utc)
        assert dt is not None
        assert dt.tzinfo == timezone.utc

    def test_returns_none_on_garbage(self) -> None:
        assert _parse_iso('not-a-date') is None

    def test_returns_none_on_none_string(self) -> None:
        assert _parse_iso('none') is None  # fromisoformat raises ValueError


# ---------------------------------------------------------------------------
# TemporalConcretizer
# ---------------------------------------------------------------------------


class TestTemporalConcretizer:
    """Unit tests for the LLM-based concretizer with mocked DSPy."""

    @pytest.fixture()
    def _patch_dspy(self):
        """Patch run_dspy_operation at the concretizer's import site."""
        self.mock_run = AsyncMock()
        with patch(
            'memex_core.memory.retrieval.temporal_concretizer.run_dspy_operation',
            side_effect=self.mock_run,
        ):
            yield

    @pytest.mark.usefixtures('_patch_dspy')
    async def test_concretize_returns_date_range(self) -> None:
        """LLM returns valid ISO dates -> parsed into (start, end)."""
        self.mock_run.return_value = SimpleNamespace(
            start_date='2024-01-15T00:00:00+00:00',
            end_date='2024-02-15T23:59:59+00:00',
        )

        concretizer = TemporalConcretizer(lm=object())  # type: ignore[arg-type]
        result = await concretizer.concretize(
            'what happened during the onboarding', reference_date=REF
        )
        assert result is not None
        start, end = result
        assert start.year == 2024 and start.month == 1 and start.day == 15
        assert end.year == 2024 and end.month == 2 and end.day == 15

    @pytest.mark.usefixtures('_patch_dspy')
    async def test_concretize_returns_none_when_llm_says_none(self) -> None:
        """LLM returns 'none' strings -> concretizer returns None."""
        self.mock_run.return_value = SimpleNamespace(
            start_date='none',
            end_date='none',
        )
        concretizer = TemporalConcretizer(lm=object())  # type: ignore[arg-type]
        result = await concretizer.concretize('some vague query', reference_date=REF)
        assert result is None

    @pytest.mark.usefixtures('_patch_dspy')
    async def test_concretize_returns_none_on_bad_dates(self) -> None:
        """LLM returns unparseable dates -> concretizer returns None."""
        self.mock_run.return_value = SimpleNamespace(
            start_date='maybe last month',
            end_date='some time ago',
        )
        concretizer = TemporalConcretizer(lm=object())  # type: ignore[arg-type]
        result = await concretizer.concretize('back when we used Django', reference_date=REF)
        assert result is None

    @pytest.mark.usefixtures('_patch_dspy')
    async def test_concretize_returns_none_when_start_after_end(self) -> None:
        """If LLM returns start >= end, concretizer returns None."""
        self.mock_run.return_value = SimpleNamespace(
            start_date='2024-06-01T00:00:00+00:00',
            end_date='2024-05-01T00:00:00+00:00',
        )
        concretizer = TemporalConcretizer(lm=object())  # type: ignore[arg-type]
        result = await concretizer.concretize('during some event', reference_date=REF)
        assert result is None

    @pytest.mark.usefixtures('_patch_dspy')
    async def test_concretize_defaults_reference_date_to_now(self) -> None:
        """When reference_date is None, defaults to now (UTC)."""
        self.mock_run.return_value = SimpleNamespace(
            start_date='2024-06-01T00:00:00+00:00',
            end_date='2024-06-15T23:59:59+00:00',
        )
        concretizer = TemporalConcretizer(lm=object())  # type: ignore[arg-type]
        result = await concretizer.concretize('during the onboarding')
        assert result is not None

    @pytest.mark.usefixtures('_patch_dspy')
    async def test_concretize_handles_llm_error_gracefully(self) -> None:
        """If LLM call fails, concretizer returns None."""
        self.mock_run.side_effect = RuntimeError('LLM unavailable')
        concretizer = TemporalConcretizer(lm=object())  # type: ignore[arg-type]
        result = await concretizer.concretize('during the onboarding', reference_date=REF)
        assert result is None
