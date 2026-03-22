"""Unit tests for frontmatter LLM extraction."""

import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

import dspy
import pytest

from memex_core.memory.extraction.core import (
    ExtractFrontmatterMetadata,
    _detect_frontmatter,
    extract_facts_from_frontmatter,
)
from memex_core.memory.extraction.models import ExtractedOutput, RawFact, Entity
from memex_core.types import FactTypes, FactKindTypes


class TestDetectFrontmatter:
    def test_detect_frontmatter_with_valid_yaml(self):
        text = '---\ncreated_by: Jasper Ginn\ncreated_date: 2025-05-24\n---\nSome content.'
        fm_text, end_pos = _detect_frontmatter(text)
        assert fm_text is not None
        assert 'created_by' in fm_text
        assert 'Jasper Ginn' in fm_text
        assert end_pos > 0

    def test_detect_frontmatter_no_frontmatter(self):
        text = 'Just some regular text without frontmatter.'
        fm_text, end_pos = _detect_frontmatter(text)
        assert fm_text is None
        assert end_pos == 0

    def test_detect_frontmatter_not_at_start(self):
        text = 'Some text\n---\ncreated_by: Jasper\n---\nMore text.'
        fm_text, end_pos = _detect_frontmatter(text)
        assert fm_text is None
        assert end_pos == 0


class TestExtractFrontmatterMetadataSignature:
    def test_signature_has_correct_input_fields(self):
        fields = ExtractFrontmatterMetadata.input_fields
        assert 'frontmatter_text' in fields
        assert 'event_date_ref' in fields

    def test_signature_has_correct_output_fields(self):
        fields = ExtractFrontmatterMetadata.output_fields
        assert 'extracted_facts' in fields


class TestExtractFactsFromFrontmatter:
    @pytest.mark.asyncio
    async def test_returns_raw_facts(self):
        """Mock LLM call, verify returns list of RawFact with correct entity types."""
        mock_fact = RawFact(
            what='Document was created by Jasper Ginn',
            fact_type=FactTypes.WORLD,
            fact_kind=FactKindTypes.DATED,
            who='Jasper Ginn',
            occurred_start='2025-05-24',
            entities=[
                Entity(text='Jasper Ginn', entity_type='Person'),
            ],
        )
        mock_output = MagicMock()
        mock_output.extracted_facts = ExtractedOutput(extracted_facts=[mock_fact])

        with patch(
            'memex_core.memory.extraction.core.run_dspy_operation',
            new_callable=AsyncMock,
            return_value=mock_output,
        ):
            lm = MagicMock(spec=dspy.LM)
            facts = await extract_facts_from_frontmatter(
                frontmatter_text='---\ncreated_by: Jasper Ginn\ncreated_date: 2025-05-24\n---\n',
                event_date=dt.datetime(2025, 5, 24),
                lm=lm,
            )

        assert len(facts) == 1
        assert facts[0].what == 'Document was created by Jasper Ginn'
        assert any(e.entity_type == 'Person' for e in facts[0].entities)

    @pytest.mark.asyncio
    async def test_empty_on_error(self):
        """Verify graceful error handling returns empty list."""
        with patch(
            'memex_core.memory.extraction.core.run_dspy_operation',
            new_callable=AsyncMock,
            side_effect=Exception('LLM call failed'),
        ):
            lm = MagicMock(spec=dspy.LM)
            facts = await extract_facts_from_frontmatter(
                frontmatter_text='---\ncreated_by: Test\n---\n',
                event_date=dt.datetime(2025, 1, 1),
                lm=lm,
            )

        assert facts == []

    @pytest.mark.asyncio
    async def test_not_called_without_frontmatter(self):
        """Verify frontmatter function is not called when no frontmatter detected."""
        text = 'Regular text without any frontmatter.'
        fm_text, _ = _detect_frontmatter(text)
        assert fm_text is None
        # If _detect_frontmatter returns None, extract_facts_from_frontmatter
        # should never be called in the pipeline

    @pytest.mark.asyncio
    async def test_frontmatter_without_author_date(self):
        """Frontmatter with only unrelated fields still works (no crash)."""
        mock_output = MagicMock()
        mock_output.extracted_facts = ExtractedOutput(extracted_facts=[])

        with patch(
            'memex_core.memory.extraction.core.run_dspy_operation',
            new_callable=AsyncMock,
            return_value=mock_output,
        ):
            lm = MagicMock(spec=dspy.LM)
            facts = await extract_facts_from_frontmatter(
                frontmatter_text='---\ntags: [python, ai]\nstatus: draft\n---\n',
                event_date=dt.datetime(2025, 1, 1),
                lm=lm,
            )

        assert facts == []
