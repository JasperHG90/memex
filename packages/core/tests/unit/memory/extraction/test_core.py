import asyncio
import datetime as dt
from unittest.mock import MagicMock, patch

import pytest
from dspy.utils.dummies import DummyLM


from memex_core.memory.extraction.core import (
    _extract_facts_from_chunk,
    _convert_causal_relations,
    _extract_facts_with_auto_split,
    chunk_text,
    extract_facts_from_text,
)
from memex_core.memory.extraction.exceptions import OutputTooLongException
from memex_core.memory.extraction.models import CausalRelation, RawFact
from memex_core.types import CausalRelationshipTypes


class TestChunkText:
    """Tests for chunk_text utility."""

    def test_chunk_text_empty(self) -> None:
        """Test that empty text returns a list with an empty string."""
        assert chunk_text('', max_chars=100, chunk_overlap=0) == ['']

    def test_chunk_text_exact_match(self) -> None:
        """Test text length exactly equal to max_chars."""
        text = '12345'
        assert chunk_text(text, max_chars=5, chunk_overlap=0) == [text]

    def test_chunk_text_split(self) -> None:
        """Test text length greater than max_chars splits correctly."""
        # Note: RecursiveCharacterTextSplitter behavior might vary slightly,
        # but we expect at least 2 chunks for > max_chars
        text = '1234567890'
        chunks = chunk_text(text, max_chars=5, chunk_overlap=0)
        assert len(chunks) >= 2


class TestExtractFactsFromChunk:
    """Tests for _extract_facts_from_chunk."""

    @pytest.mark.asyncio
    async def test_extract_facts_from_chunk_success(
        self, mock_lm: DummyLM, mock_predictor: MagicMock, sample_raw_fact: RawFact
    ) -> None:
        """Test successful extraction of facts."""
        mock_result = MagicMock()
        mock_result.extracted_facts = MagicMock()
        mock_result.extracted_facts.extracted_facts = [sample_raw_fact]
        mock_predictor.acall.return_value = mock_result

        mock_lm.copy = MagicMock(return_value=mock_lm)  # type: ignore
        mock_lm.history.append(
            {'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}}
        )

        facts, usage = await _extract_facts_from_chunk(
            chunk='test chunk',
            chunk_index=0,
            total_chunks=1,
            event_date=dt.datetime.now(),
            context='test context',
            lm=mock_lm,
            predictor=mock_predictor,
        )

        assert len(facts) == 1
        assert facts[0] == sample_raw_fact
        assert usage.input_tokens == 10
        assert usage.output_tokens == 5
        assert usage.total_tokens == 15

    @pytest.mark.asyncio
    async def test_missing_token_usage(
        self, mock_lm: DummyLM, mock_predictor: MagicMock, sample_raw_fact: RawFact
    ) -> None:
        """Test handling of missing token usage in history."""
        mock_result = MagicMock()
        mock_result.extracted_facts.extracted_facts = [sample_raw_fact]
        mock_predictor.acall.return_value = mock_result
        mock_lm.copy = MagicMock(return_value=mock_lm)  # type: ignore

        # History exists but no usage key
        mock_lm.history.append({'other_key': 'value'})

        facts, usage = await _extract_facts_from_chunk(
            chunk='test',
            chunk_index=0,
            total_chunks=1,
            event_date=dt.datetime.now(),
            context='',
            lm=mock_lm,
            predictor=mock_predictor,
        )
        assert len(facts) == 1
        # It's None now if missing, because we allow None in SQLModel
        assert usage.total_tokens is None

    @pytest.mark.asyncio
    async def test_empty_facts_response(self, mock_lm: DummyLM, mock_predictor: MagicMock) -> None:
        """Test handling of empty facts list from LLM."""
        mock_result = MagicMock()
        mock_result.extracted_facts.extracted_facts = []
        mock_predictor.acall.return_value = mock_result
        mock_lm.copy = MagicMock(return_value=mock_lm)  # type: ignore

        facts, _ = await _extract_facts_from_chunk(
            chunk='test',
            chunk_index=0,
            total_chunks=1,
            event_date=dt.datetime.now(),
            context='',
            lm=mock_lm,
            predictor=mock_predictor,
        )
        assert facts == []

    @pytest.mark.asyncio
    async def test_extract_facts_from_chunk_with_semaphore(
        self, mock_lm: DummyLM, mock_predictor: MagicMock, sample_raw_fact: RawFact
    ) -> None:
        """Test extraction works correctly with a semaphore."""
        mock_result = MagicMock()
        mock_result.extracted_facts = MagicMock()
        mock_result.extracted_facts.extracted_facts = [sample_raw_fact]
        mock_predictor.acall.return_value = mock_result

        semaphore = asyncio.Semaphore(1)

        facts, _ = await _extract_facts_from_chunk(
            chunk='test chunk',
            chunk_index=0,
            total_chunks=1,
            event_date=dt.datetime.now(),
            context='test context',
            lm=mock_lm,
            predictor=mock_predictor,
            semaphore=semaphore,
        )

        assert len(facts) == 1
        assert facts[0] == sample_raw_fact

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ('error_message', 'expected_exception'),
        [
            ('context_length_exceeded', OutputTooLongException),
            ('output is too long', OutputTooLongException),
            ('maximum context length', OutputTooLongException),
        ],
    )
    async def test_extract_facts_from_chunk_context_error(
        self,
        mock_lm: DummyLM,
        mock_predictor: MagicMock,
        error_message: str,
        expected_exception: type[Exception],
    ) -> None:
        """Test that specific error messages raise OutputTooLongException."""
        mock_predictor.acall.side_effect = RuntimeError(error_message)

        with pytest.raises(expected_exception):
            await _extract_facts_from_chunk(
                chunk='test chunk',
                chunk_index=0,
                total_chunks=1,
                event_date=dt.datetime.now(),
                context='test context',
                lm=mock_lm,
                predictor=mock_predictor,
            )

    @pytest.mark.asyncio
    async def test_extract_facts_from_chunk_generic_error(
        self, mock_lm: DummyLM, mock_predictor: MagicMock
    ) -> None:
        """Test that generic errors return empty list and do not crash."""
        mock_predictor.acall.side_effect = RuntimeError('Some random API error')

        facts, usage = await _extract_facts_from_chunk(
            chunk='test chunk',
            chunk_index=0,
            total_chunks=1,
            event_date=dt.datetime.now(),
            context='test context',
            lm=mock_lm,
            predictor=mock_predictor,
        )

        assert facts == []
        assert usage.total_tokens is None


class TestExtractFactsFromChunkMalformedOutput:
    """Tests for malformed LLM output handling in _extract_facts_from_chunk (AUDIT-031).

    These tests patch ``run_dspy_operation`` to bypass the circuit breaker
    and focus on how _extract_facts_from_chunk handles various error modes.
    """

    @pytest.mark.asyncio
    async def test_malformed_result_empty_facts(
        self, mock_lm: DummyLM, mock_predictor: MagicMock
    ) -> None:
        """LLM returns a valid structure but with no facts."""
        from memex_core.memory.sql_models import TokenUsage

        mock_result = MagicMock()
        mock_result.extracted_facts.extracted_facts = []

        with patch(
            'memex_core.memory.extraction.core.run_dspy_operation',
            return_value=(mock_result, TokenUsage()),
        ):
            facts, usage = await _extract_facts_from_chunk(
                chunk='test chunk',
                chunk_index=0,
                total_chunks=1,
                event_date=dt.datetime.now(),
                context='ctx',
                lm=mock_lm,
                predictor=mock_predictor,
            )
            assert facts == []
            assert usage.total_tokens is None

    @pytest.mark.asyncio
    async def test_value_error_from_dspy_parsing(
        self, mock_lm: DummyLM, mock_predictor: MagicMock
    ) -> None:
        """DSPy raises ValueError on unparseable JSON -> returns empty list."""
        with patch(
            'memex_core.memory.extraction.core.run_dspy_operation',
            side_effect=ValueError('Could not parse LLM output as JSON'),
        ):
            facts, usage = await _extract_facts_from_chunk(
                chunk='test chunk',
                chunk_index=0,
                total_chunks=1,
                event_date=dt.datetime.now(),
                context='ctx',
                lm=mock_lm,
                predictor=mock_predictor,
            )
            assert facts == []
            assert usage.total_tokens is None

    @pytest.mark.asyncio
    async def test_key_error_from_malformed_structure(
        self, mock_lm: DummyLM, mock_predictor: MagicMock
    ) -> None:
        """KeyError when expected key missing in response -> returns empty list."""
        with patch(
            'memex_core.memory.extraction.core.run_dspy_operation',
            side_effect=KeyError('extracted_facts'),
        ):
            facts, usage = await _extract_facts_from_chunk(
                chunk='test chunk',
                chunk_index=0,
                total_chunks=1,
                event_date=dt.datetime.now(),
                context='ctx',
                lm=mock_lm,
                predictor=mock_predictor,
            )
            assert facts == []
            assert usage.total_tokens is None

    @pytest.mark.asyncio
    async def test_os_error_returns_empty(
        self, mock_lm: DummyLM, mock_predictor: MagicMock
    ) -> None:
        """OSError (network issue) -> returns empty list."""
        with patch(
            'memex_core.memory.extraction.core.run_dspy_operation',
            side_effect=OSError('Connection refused'),
        ):
            facts, usage = await _extract_facts_from_chunk(
                chunk='test chunk',
                chunk_index=0,
                total_chunks=1,
                event_date=dt.datetime.now(),
                context='ctx',
                lm=mock_lm,
                predictor=mock_predictor,
            )
            assert facts == []
            assert usage.total_tokens is None

    @pytest.mark.asyncio
    async def test_runtime_error_non_context_returns_empty(
        self, mock_lm: DummyLM, mock_predictor: MagicMock
    ) -> None:
        """RuntimeError that is NOT a context-length error -> returns empty list."""
        with patch(
            'memex_core.memory.extraction.core.run_dspy_operation',
            side_effect=RuntimeError('unexpected internal error'),
        ):
            facts, usage = await _extract_facts_from_chunk(
                chunk='test chunk',
                chunk_index=0,
                total_chunks=1,
                event_date=dt.datetime.now(),
                context='ctx',
                lm=mock_lm,
                predictor=mock_predictor,
            )
            assert facts == []
            assert usage.total_tokens is None

    @pytest.mark.asyncio
    async def test_runtime_error_context_length_raises_output_too_long(
        self, mock_lm: DummyLM, mock_predictor: MagicMock
    ) -> None:
        """RuntimeError with 'context_length_exceeded' -> re-raised as OutputTooLongException."""
        with patch(
            'memex_core.memory.extraction.core.run_dspy_operation',
            side_effect=RuntimeError('context_length_exceeded'),
        ):
            with pytest.raises(OutputTooLongException):
                await _extract_facts_from_chunk(
                    chunk='test chunk',
                    chunk_index=0,
                    total_chunks=1,
                    event_date=dt.datetime.now(),
                    context='ctx',
                    lm=mock_lm,
                    predictor=mock_predictor,
                )


class TestExtractFactsWithAutoSplit:
    """Tests for _extract_facts_with_auto_split."""

    @pytest.mark.asyncio
    async def test_no_split_needed(
        self, mock_lm: DummyLM, mock_predictor: MagicMock, sample_raw_fact: RawFact
    ) -> None:
        """Test simple case where no splitting is required."""
        with patch('memex_core.memory.extraction.core._extract_facts_from_chunk') as mock_extract:
            mock_extract.return_value = ([sample_raw_fact], MagicMock())

            facts, _ = await _extract_facts_with_auto_split(
                chunk='test chunk',
                chunk_index=0,
                total_chunks=1,
                event_date=dt.datetime.now(),
                context='test context',
                lm=mock_lm,
                predictor=mock_predictor,
            )

            assert len(facts) == 1
            assert facts[0] == sample_raw_fact
            mock_extract.assert_called_once()

    @pytest.mark.asyncio
    async def test_split_on_error(
        self, mock_lm: DummyLM, mock_predictor: MagicMock, sample_raw_fact: RawFact
    ) -> None:
        """Test that chunk is split when OutputTooLongException is raised."""
        with patch('memex_core.memory.extraction.core._extract_facts_from_chunk') as mock_extract:
            # First call raises exception, subsequent calls return facts
            mock_extract.side_effect = [
                OutputTooLongException(),  # Main chunk fails
                ([sample_raw_fact], MagicMock(total_tokens=10)),  # First half succeeds
                ([sample_raw_fact], MagicMock(total_tokens=10)),  # Second half succeeds
            ]

            facts, usage = await _extract_facts_with_auto_split(
                chunk='long chunk',
                chunk_index=0,
                total_chunks=1,
                event_date=dt.datetime.now(),
                context='test context',
                lm=mock_lm,
                predictor=mock_predictor,
            )

            assert len(facts) == 2  # One from each half
            assert usage.total_tokens == 20  # Sum of usage
            assert mock_extract.call_count == 3

    @pytest.mark.asyncio
    async def test_deep_recursion_split(
        self, mock_lm: DummyLM, mock_predictor: MagicMock, sample_raw_fact: RawFact
    ) -> None:
        """Test multiple levels of recursion in splitting."""
        with patch('memex_core.memory.extraction.core._extract_facts_from_chunk') as mock_extract:
            # Simulate a failure tree:
            # Root (Exception)
            #   -> Left Child (Exception)
            #       -> Left-Left (Success)
            #       -> Left-Right (Success)
            #   -> Right Child (Success)
            mock_extract.side_effect = [
                OutputTooLongException(),  # Root
                OutputTooLongException(),  # Left Child
                ([sample_raw_fact], MagicMock(total_tokens=1)),  # Left-Left
                ([sample_raw_fact], MagicMock(total_tokens=1)),  # Left-Right
                ([sample_raw_fact], MagicMock(total_tokens=1)),  # Right Child
            ]

            facts, usage = await _extract_facts_with_auto_split(
                chunk='test ' * 20,
                chunk_index=0,
                total_chunks=1,
                event_date=dt.datetime.now(),
                context='',
                lm=mock_lm,
                predictor=mock_predictor,
            )

            assert len(facts) == 3
            assert usage.total_tokens == 3
            assert mock_extract.call_count == 5


class TestExtractFactsFromText:
    """Tests for extract_facts_from_text orchestration."""

    @pytest.mark.asyncio
    async def test_orchestration(
        self, mock_lm: DummyLM, mock_predictor: MagicMock, sample_raw_fact: RawFact
    ) -> None:
        """Test that text is chunked and processed."""
        with patch(
            'memex_core.memory.extraction.core._extract_facts_with_auto_split'
        ) as mock_split_extract:
            mock_split_extract.return_value = ([sample_raw_fact], MagicMock(total_tokens=10))

            facts, metadata, usage = await extract_facts_from_text(
                text='chunk1 chunk2',
                event_date=dt.datetime.now(),
                lm=mock_lm,
                predictor=mock_predictor,
                agent_name='TestAgent',
                chunk_max_chars=6,  # Small size to force chunking
                chunk_overlap=0,
            )

            assert len(facts) > 0
            assert (usage.total_tokens or 0) > 0
            assert mock_split_extract.call_count >= 1

    @pytest.mark.asyncio
    async def test_empty_text(self, mock_lm: DummyLM, mock_predictor: MagicMock) -> None:
        """Test processing of empty text."""
        # chunk_text("") returns [""]
        # _extract_facts_with_auto_split will be called once with ""
        with patch(
            'memex_core.memory.extraction.core._extract_facts_with_auto_split'
        ) as mock_split_extract:
            mock_split_extract.return_value = ([], MagicMock(total_tokens=0))

            facts, metadata, usage = await extract_facts_from_text(
                text='',
                event_date=dt.datetime.now(),
                lm=mock_lm,
                predictor=mock_predictor,
                agent_name='Test',
                chunk_max_chars=100,
                chunk_overlap=0,
            )

            assert facts == []
            assert usage.total_tokens in (0, None)
            # Metadata might contain one entry for the empty chunk depending on implementation
            # Current implementation: chunk_text returns [""] -> loop runs once -> metadata has 1 entry
            # But if the mock prevents the loop from running, metadata might be empty
            assert len(metadata) in (0, 1)


class TestConvertCausalRelations:
    """Tests for _convert_causal_relations."""

    def test_convert_valid_relations(self) -> None:
        """Test conversion of valid relations with index adjustment."""
        relations = [
            CausalRelation(
                relationship_type=CausalRelationshipTypes.CAUSED_BY,
                target_fact_index=0,
                strength=1.0,
            ),
            CausalRelation(
                relationship_type=CausalRelationshipTypes.ENABLES, target_fact_index=1, strength=0.8
            ),
        ]

        converted = _convert_causal_relations(relations, fact_start_idx=10)

        assert len(converted) == 2
        assert converted[0].target_fact_index == 10  # 0 + 10
        assert converted[1].target_fact_index == 11  # 1 + 10

    def test_filter_invalid_relations(self) -> None:
        """Test that relations with negative target indices are filtered."""
        relations = [
            CausalRelation(
                relationship_type=CausalRelationshipTypes.CAUSED_BY,
                target_fact_index=-1,
                strength=1.0,
            )
        ]

        converted = _convert_causal_relations(relations, fact_start_idx=0)

        assert len(converted) == 0

    def test_boundary_strength_values(self) -> None:
        """Test relations with boundary strength values."""
        relations = [
            CausalRelation(
                relationship_type=CausalRelationshipTypes.CAUSES, target_fact_index=0, strength=0.0
            ),
            CausalRelation(
                relationship_type=CausalRelationshipTypes.PREVENTS,
                target_fact_index=0,
                strength=1.0,
            ),
        ]
        converted = _convert_causal_relations(relations, fact_start_idx=0)
        assert len(converted) == 2
        assert converted[0].strength == 0.0
        assert converted[1].strength == 1.0
