"""E2E test: background file upload must await the contradiction detection coroutine."""

import asyncio
import io
import warnings

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock
from datetime import datetime, timezone

from memex_core.config import GLOBAL_VAULT_ID
from memex_core.memory.extraction.models import ExtractedFact, ChunkMetadata
from memex_core.memory.sql_models import TokenUsage


@pytest.mark.asyncio
async def test_background_upload_awaits_contradiction_task(client: TestClient):
    """
    Uploading a non-markdown file with background=true must await the
    contradiction detection coroutine returned by MemoryEngine.retain.

    Regression test for: coroutine 'ContradictionEngine.detect_contradictions'
    was never awaited.
    """
    vault_id = GLOBAL_VAULT_ID

    now = datetime.now(timezone.utc)
    mock_facts = [
        ExtractedFact(
            fact_text='The sky is blue.',
            fact_type='world',
            entities=[],
            chunk_index=0,
            content_index=0,
            mentioned_at=now,
            vault_id=vault_id,
        ),
    ]
    mock_chunks = [
        ChunkMetadata(
            chunk_text='The sky is blue.',
            fact_count=1,
            chunk_index=0,
            content_index=0,
        )
    ]
    mock_usage = TokenUsage(total_tokens=10)
    mock_embeddings = [[0.1] * 384]

    # Track whether detect_contradictions was awaited
    contradiction_awaited = asyncio.Event()

    async def spy_detect_contradictions(*args, **kwargs):
        """Spy that records it was awaited, then delegates to a no-op."""
        contradiction_awaited.set()

    extract_path = 'memex_core.memory.extraction.engine.ExtractionEngine._extract_facts'
    embed_path = 'memex_core.memory.extraction.embedding_processor.generate_embeddings_batch'
    date_path = 'memex_core.processing.dates.extract_document_date'
    contradiction_path = (
        'memex_core.memory.contradiction.engine.ContradictionEngine.detect_contradictions'
    )

    with (
        patch(extract_path, return_value=(mock_facts, mock_chunks, mock_usage)),
        patch(embed_path, return_value=mock_embeddings),
        patch(date_path, new_callable=AsyncMock, return_value=now),
        patch(contradiction_path, side_effect=spy_detect_contradictions) as mock_detect,
    ):
        # Upload a non-markdown file with background=true
        file_content = b'The sky is blue. This is a text document.'
        file = io.BytesIO(file_content)

        with warnings.catch_warnings(record=True) as caught_warnings:
            warnings.simplefilter('always')

            response = client.post(
                '/api/v1/ingestions/upload?background=true',
                files=[('files', ('document.txt', file, 'text/plain'))],
            )

            assert response.status_code == 202, (
                f'Expected 202, got {response.status_code}: {response.text}'
            )
            resp_data = response.json()
            assert resp_data['status'] in ('accepted', 'pending')

        # Verify no "coroutine was never awaited" warnings
        coroutine_warnings = [
            w
            for w in caught_warnings
            if issubclass(w.category, RuntimeWarning) and 'was never awaited' in str(w.message)
        ]
        assert not coroutine_warnings, (
            f'Got unawaited coroutine warning(s): {[str(w.message) for w in coroutine_warnings]}'
        )

    # Verify the contradiction coroutine was actually called and awaited
    assert mock_detect.called, 'ContradictionEngine.detect_contradictions was never called'
    assert contradiction_awaited.is_set(), (
        'ContradictionEngine.detect_contradictions coroutine was created but never awaited'
    )
