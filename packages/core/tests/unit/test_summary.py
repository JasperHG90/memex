"""Tests for MemexAPI.summarize_search_results method."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_summarize_search_results(api, mock_session):
    """Verify summarize_search_results calls run_dspy_operation and commits inside session."""
    mock_prediction = MagicMock()
    mock_prediction.summary = 'Result [0] says X and [1] says Y.'

    with patch('memex_core.api.run_dspy_operation', new_callable=AsyncMock) as mock_run:
        mock_run.return_value = (mock_prediction, {'tokens': 100})

        result = await api.summarize_search_results(
            query='test query',
            texts=['first result', 'second result'],
        )

    assert result == 'Result [0] says X and [1] says Y.'

    # Verify run_dspy_operation was called with correct kwargs
    mock_run.assert_called_once()
    call_kwargs = mock_run.call_args[1]
    assert call_kwargs['input_kwargs']['query'] == 'test query'
    assert call_kwargs['input_kwargs']['search_results'] == ['first result', 'second result']
    assert call_kwargs['context_metadata'] == {'operation': 'search_summary'}
    assert call_kwargs['session'] is mock_session

    # Verify session.commit was called inside the context manager
    mock_session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_summarize_search_results_propagates_errors(api):
    """Verify exceptions from run_dspy_operation propagate to caller."""
    with patch('memex_core.api.run_dspy_operation', new_callable=AsyncMock) as mock_run:
        mock_run.side_effect = RuntimeError('LLM call failed')

        with pytest.raises(RuntimeError, match='LLM call failed'):
            await api.summarize_search_results(
                query='test query',
                texts=['some text'],
            )
