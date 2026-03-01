"""Tests for confidence annotations in MCP memex_search output."""

import pytest
from uuid import uuid4
from memex_common.schemas import MemoryUnitDTO, FactTypes


@pytest.mark.asyncio
async def test_contradicted_marker_in_search_output(mock_api, mcp_client):
    """Test that contradicted opinions show [CONTRADICTED]."""
    mock_api.search.return_value = [
        MemoryUnitDTO(
            id=uuid4(),
            note_id=uuid4(),
            text='qmd is a markdown format.',
            fact_type=FactTypes.OPINION,
            score=0.7,
            vault_id=uuid4(),
            metadata={},
            confidence_alpha=1.0,
            confidence_beta=9.0,  # confidence = 0.1
        )
    ]

    result = await mcp_client.call_tool('memex_search', {'query': 'qmd', 'limit': 1})
    output_text = result.content[0].text

    assert 'CONTRADICTED' in output_text
    assert 'treat with skepticism' in output_text


@pytest.mark.asyncio
async def test_disputed_marker_in_search_output(mock_api, mcp_client):
    """Test that disputed opinions (0.3 <= confidence < 0.5) show [Disputed]."""
    mock_api.search.return_value = [
        MemoryUnitDTO(
            id=uuid4(),
            note_id=uuid4(),
            text='qmd might be a markdown format.',
            fact_type=FactTypes.OPINION,
            score=0.6,
            vault_id=uuid4(),
            metadata={},
            confidence_alpha=4.0,
            confidence_beta=6.0,  # confidence = 0.4
        )
    ]

    result = await mcp_client.call_tool('memex_search', {'query': 'qmd', 'limit': 1})
    output_text = result.content[0].text

    assert 'Disputed' in output_text
    assert 'mixed evidence' in output_text


@pytest.mark.asyncio
async def test_no_marker_for_high_confidence(mock_api, mcp_client):
    """Test that high confidence opinions show well-supported label."""
    mock_api.search.return_value = [
        MemoryUnitDTO(
            id=uuid4(),
            note_id=uuid4(),
            text='qmd is software.',
            fact_type=FactTypes.OPINION,
            score=0.9,
            vault_id=uuid4(),
            metadata={},
            confidence_alpha=8.0,
            confidence_beta=2.0,  # confidence = 0.8
        )
    ]

    result = await mcp_client.call_tool('memex_search', {'query': 'qmd', 'limit': 1})
    output_text = result.content[0].text

    assert 'CONTRADICTED' not in output_text
    assert 'Disputed' not in output_text
    assert 'Well-supported opinion' in output_text


@pytest.mark.asyncio
async def test_no_marker_for_null_confidence(mock_api, mcp_client):
    """Test that units without confidence (world facts) have no marker."""
    mock_api.search.return_value = [
        MemoryUnitDTO(
            id=uuid4(),
            note_id=uuid4(),
            text='Python is a programming language.',
            fact_type=FactTypes.WORLD,
            score=0.9,
            vault_id=uuid4(),
            metadata={},
        )
    ]

    result = await mcp_client.call_tool('memex_search', {'query': 'python', 'limit': 1})
    output_text = result.content[0].text

    assert 'CONTRADICTED' not in output_text
    assert 'Disputed' not in output_text
