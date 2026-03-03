import datetime as dt
from unittest.mock import MagicMock

import pytest
from dspy.utils.dummies import DummyLM

from memex_core.memory.extraction.models import BaseFact
from memex_core.memory.extraction.core import _extract_facts_from_chunk


class TestFactClassificationPrompts:
    """
    TDD tests for Fact Classification Refinement.
    Ensures that the Prompt descriptions and Instructions contain the necessary
    guidance to distinguish 'World' from 'Experience'.
    """

    def test_base_fact_docstrings(self) -> None:
        """
        Verify that BaseFact.fact_type description contains the refined definitions.
        Target phrases:
        - "narrative events" for Experience
        - "outcomes of actions" or "result of an event" for World
        """
        field_info = BaseFact.model_fields['fact_type']
        description = field_info.description.lower()

        # Check for World definition enhancements
        # We want to explicitly include "outcomes" or "state" resulting from events
        assert 'outcomes of actions' in description or 'resulting state' in description, (
            "World definition missing 'outcomes/state' guidance."
        )

        # Check for Experience definition enhancements
        # We want to restrict it to "narrative" or "episodic" (which is already there, but we want to emphasize narrative)
        # The current one says "Specific episodic events". We want "narrative events".
        assert 'narrative' in description, "Experience definition missing 'narrative' keyword."

    @pytest.mark.asyncio
    async def test_extraction_rules_contain_classification_guidance(
        self, mock_lm: DummyLM, mock_predictor: MagicMock
    ) -> None:
        """
        Verify that the special_instructions passed to DSPy include
        explicit guidance on World vs Experience ambiguities.
        """
        # Setup mock
        mock_result = MagicMock()
        mock_result.extracted_facts.extracted_facts = []
        mock_predictor.acall.return_value = mock_result
        mock_lm.copy = MagicMock(return_value=mock_lm)  # type: ignore

        # Execute
        await _extract_facts_from_chunk(
            chunk='test',
            chunk_index=0,
            total_chunks=1,
            event_date=dt.datetime.now(),
            context='',
            lm=mock_lm,
            predictor=mock_predictor,
        )

        # Verify
        call_kwargs = mock_predictor.acall.call_args.kwargs
        instructions = call_kwargs['special_instructions']

        # Assertions for the new guidelines we plan to add in core.py
        # We check for the core principle: defining state vs describing an event
        lower_instructions = instructions.lower()

        assert 'classify facts describing "what something is"' in lower_instructions
        assert '"how it works"' in lower_instructions
        assert 'as world' in lower_instructions

        assert 'even if described with past-tense verbs' in lower_instructions, (
            'Instructions missing guidance on verb tense traps.'
        )
