"""Tests for DSPy-based document date extraction."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from memex_core.processing.dates import (
    DateExtraction,
    _parse_iso_date,
    extract_document_date,
)


class TestParseIsoDate:
    def test_valid_iso(self):
        result = _parse_iso_date('2023-06-15')
        assert result is not None
        assert result.year == 2023
        assert result.month == 6
        assert result.day == 15
        assert result.tzinfo is not None

    def test_valid_with_timezone(self):
        result = _parse_iso_date('2024-01-01T12:00:00+05:00')
        assert result is not None
        assert result.year == 2024
        assert result.tzinfo is not None

    def test_invalid_string(self):
        assert _parse_iso_date('not-a-date') is None

    def test_empty_string(self):
        assert _parse_iso_date('') is None


class TestExtractDocumentDate:
    @pytest.mark.asyncio
    async def test_returns_date_on_high_confidence(self):
        """When the LLM returns a high-confidence date, it should be parsed."""
        mock_extraction = DateExtraction(
            reasoning='Found explicit publication date.',
            original_text='Published on June 15, 2023',
            normalized_date='2023-06-15',
            date_type='publication',
            confidence=0.95,
            is_explicit=True,
        )
        mock_prediction = MagicMock()
        mock_prediction.extracted_date = mock_extraction

        mock_lm = MagicMock()

        with patch(
            'memex_core.processing.dates.run_dspy_operation',
            new_callable=AsyncMock,
            return_value=(mock_prediction, MagicMock()),
        ):
            result = await extract_document_date(
                'Published on June 15, 2023. Article content...', mock_lm
            )
            assert result is not None
            assert result.year == 2023
            assert result.month == 6
            assert result.day == 15

    @pytest.mark.asyncio
    async def test_returns_none_on_low_confidence(self):
        """When the LLM returns a low-confidence date, it should return None."""
        mock_extraction = DateExtraction(
            reasoning='Guessing from vague context.',
            original_text='sometime last year',
            normalized_date='2025-01-01',
            date_type='unknown',
            confidence=0.3,
            is_explicit=False,
        )
        mock_prediction = MagicMock()
        mock_prediction.extracted_date = mock_extraction

        mock_lm = MagicMock()

        with patch(
            'memex_core.processing.dates.run_dspy_operation',
            new_callable=AsyncMock,
            return_value=(mock_prediction, MagicMock()),
        ):
            result = await extract_document_date('Some text with vague dates.', mock_lm)
            assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_empty_date(self):
        """When the LLM finds no date, it should return None."""
        mock_extraction = DateExtraction(
            reasoning='No dates found.',
            original_text='',
            normalized_date='',
            date_type='unknown',
            confidence=0.0,
            is_explicit=False,
        )
        mock_prediction = MagicMock()
        mock_prediction.extracted_date = mock_extraction

        mock_lm = MagicMock()

        with patch(
            'memex_core.processing.dates.run_dspy_operation',
            new_callable=AsyncMock,
            return_value=(mock_prediction, MagicMock()),
        ):
            result = await extract_document_date('Content with no date info.', mock_lm)
            assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_llm_failure(self):
        """When the LLM call fails, it should return None gracefully."""
        mock_lm = MagicMock()

        with patch(
            'memex_core.processing.dates.run_dspy_operation',
            new_callable=AsyncMock,
            side_effect=RuntimeError('LLM unavailable'),
        ):
            result = await extract_document_date('Some content.', mock_lm)
            assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_empty_text(self):
        """Empty text should short-circuit and return None."""
        mock_lm = MagicMock()
        result = await extract_document_date('', mock_lm)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_whitespace_only(self):
        """Whitespace-only text should short-circuit and return None."""
        mock_lm = MagicMock()
        result = await extract_document_date('   \n\t  ', mock_lm)
        assert result is None

    @pytest.mark.asyncio
    async def test_email_date_extraction(self):
        """Simulate extracting a date from an email-like document."""
        mock_extraction = DateExtraction(
            reasoning='Found date in email header.',
            original_text='Date: Thu, 12 Oct 2023 14:30:00 +0000',
            normalized_date='2023-10-12',
            date_type='creation',
            confidence=0.98,
            is_explicit=True,
        )
        mock_prediction = MagicMock()
        mock_prediction.extracted_date = mock_extraction

        mock_lm = MagicMock()

        with patch(
            'memex_core.processing.dates.run_dspy_operation',
            new_callable=AsyncMock,
            return_value=(mock_prediction, MagicMock()),
        ):
            text = 'From: user@example.com\nDate: Thu, 12 Oct 2023 14:30:00 +0000\nSubject: Test'
            result = await extract_document_date(text, mock_lm)
            assert result is not None
            assert result.year == 2023
            assert result.month == 10
            assert result.day == 12
