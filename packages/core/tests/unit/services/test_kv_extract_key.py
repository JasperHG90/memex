"""Unit tests for KVService.extract_key (DSPy key extraction)."""

from unittest.mock import MagicMock

import pytest

from memex_core.services.kv import KVService


@pytest.fixture
def kv_service(mock_metastore, mock_filestore, mock_config):
    return KVService(
        metastore=mock_metastore,
        filestore=mock_filestore,
        config=mock_config,
    )


@pytest.mark.asyncio
async def test_extract_key_returns_extracted_key(kv_service, mock_dspy_lm):
    """extract_key should return the key string from the LLM prediction."""
    prediction = MagicMock()
    prediction.key = 'tool:python:pkg_mgr'

    from memex_core.memory.sql_models import TokenUsage

    usage = TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15)
    mock_dspy_lm.add_response(prediction, usage)

    result = await kv_service.extract_key('I prefer using uv for Python packages', lm=MagicMock())
    assert result == 'tool:python:pkg_mgr'
    assert mock_dspy_lm.call_count == 1


@pytest.mark.asyncio
async def test_extract_key_strips_quotes(kv_service, mock_dspy_lm):
    """extract_key should strip surrounding quotes from the key."""
    prediction = MagicMock()
    prediction.key = '"style:code:indent"'

    from memex_core.memory.sql_models import TokenUsage

    usage = TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15)
    mock_dspy_lm.add_response(prediction, usage)

    result = await kv_service.extract_key('I use 4-space indentation', lm=MagicMock())
    assert result == 'style:code:indent'


@pytest.mark.asyncio
async def test_extract_key_returns_none_for_empty(kv_service, mock_dspy_lm):
    """extract_key should return None when LLM returns empty string."""
    prediction = MagicMock()
    prediction.key = ''

    from memex_core.memory.sql_models import TokenUsage

    usage = TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15)
    mock_dspy_lm.add_response(prediction, usage)

    result = await kv_service.extract_key('some unclear text', lm=MagicMock())
    assert result is None


@pytest.mark.asyncio
async def test_extract_key_returns_none_on_llm_error(kv_service, mock_dspy_lm):
    """extract_key should return None and log warning on LLM failures."""
    # No responses queued — will raise RuntimeError
    # But extract_key catches ValueError, RuntimeError, etc.

    # Queue no response so the mock raises RuntimeError
    result = await kv_service.extract_key('some text', lm=MagicMock())
    assert result is None


@pytest.mark.asyncio
async def test_extract_key_returns_none_when_no_key_attr(kv_service, mock_dspy_lm):
    """extract_key should handle missing key attribute gracefully."""
    prediction = MagicMock(spec=[])  # No attributes at all

    from memex_core.memory.sql_models import TokenUsage

    usage = TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15)
    mock_dspy_lm.add_response(prediction, usage)

    result = await kv_service.extract_key('some text', lm=MagicMock())
    assert result is None
