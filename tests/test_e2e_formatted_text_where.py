"""E2E test: RawFact.formatted_text includes 'where', propagated to MemoryUnit.text.

Validates the full pipeline:
  RawFact(where='Paris') → formatted_text includes 'Where: Paris'
    → ExtractedFact.fact_text → MemoryUnit.text in database
    → searchable via KeywordStrategy
"""

import base64
import json
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock, AsyncMock
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from memex_core.memory.extraction.models import ExtractedFact, ChunkMetadata, RawFact
from memex_core.memory.sql_models import TokenUsage
from memex_core.types import FactTypes, FactKindTypes


def parse_ndjson(text_: str):
    return [json.loads(line) for line in text_.splitlines() if line.strip()]


class TestFormattedTextWhereUnit:
    """Unit-level tests for where in formatted_text."""

    def test_where_included_in_formatted_text(self) -> None:
        """RawFact.formatted_text includes Where: when where is set."""
        fact = RawFact(
            what='Meeting held',
            when='2026-03-19',
            where='Amsterdam office, 3rd floor',
            who='Alice, Bob',
            why='Quarterly review',
            fact_type=FactTypes.EVENT,
            fact_kind=FactKindTypes.CONVERSATION,
        )
        assert 'Where: Amsterdam office, 3rd floor' in fact.formatted_text
        # Verify order: When before Where before Involving
        text_ = fact.formatted_text
        when_pos = text_.index('When:')
        where_pos = text_.index('Where:')
        involving_pos = text_.index('Involving:')
        assert when_pos < where_pos < involving_pos, (
            f'Expected When < Where < Involving order, got: {text_}'
        )

    def test_where_na_excluded(self) -> None:
        """Where: N/A is excluded from formatted_text."""
        fact = RawFact(
            what='Something happened',
            where='N/A',
            fact_type=FactTypes.WORLD,
            fact_kind=FactKindTypes.CONVERSATION,
        )
        assert 'Where' not in fact.formatted_text

    def test_where_none_excluded(self) -> None:
        """Where: None is excluded from formatted_text."""
        fact = RawFact(
            what='Something happened',
            where=None,
            fact_type=FactTypes.WORLD,
            fact_kind=FactKindTypes.CONVERSATION,
        )
        assert 'Where' not in fact.formatted_text

    def test_where_only_field_present(self) -> None:
        """formatted_text with only what and where."""
        fact = RawFact(
            what='Landmark exists',
            where='Tokyo, Japan',
            fact_type=FactTypes.WORLD,
            fact_kind=FactKindTypes.CONVERSATION,
        )
        assert fact.formatted_text == 'Landmark exists | Where: Tokyo, Japan'


@pytest.mark.integration
@pytest.mark.llm
def test_where_propagates_to_memory_unit_via_ingestion(client: TestClient) -> None:
    """Full e2e: where in RawFact → ExtractedFact.fact_text → MemoryUnit.text in DB.

    Uses the ingestion API with mocked LLM extraction to verify the entire pipeline.
    """
    vault_name = 'Where Test Vault'
    resp = client.post('/api/v1/vaults', json={'name': vault_name})
    assert resp.status_code == 200
    vault_id = UUID(resp.json()['id'])

    now = datetime.now(timezone.utc)

    # Simulate what the LLM would extract — fact_text already includes 'Where: ...'
    # because in production, engine.py calls raw_fact.formatted_text to build fact_text
    mock_facts = [
        ExtractedFact(
            fact_text='Team standup held | When: 2026-03-19 | Where: Amsterdam HQ | Involving: Alice, Bob | Sprint planning',
            fact_type='event',
            entities=[],
            chunk_index=0,
            content_index=0,
            mentioned_at=now,
            vault_id=vault_id,
            who='Alice, Bob',
            where='Amsterdam HQ',
        ),
    ]
    mock_chunks = [
        ChunkMetadata(
            chunk_text='Team standup was held at Amsterdam HQ on March 19.',
            fact_count=1,
            chunk_index=0,
            content_index=0,
        )
    ]
    mock_usage = TokenUsage(total_tokens=50)
    mock_embeddings = [[0.1] * 384]

    extract_path = 'memex_core.memory.extraction.engine.ExtractionEngine._extract_facts'
    embed_path = 'memex_core.memory.extraction.embedding_processor.generate_embeddings_batch'

    with patch(extract_path) as mock_extract, patch(embed_path) as mock_embed:
        mock_extract.return_value = (mock_facts, mock_chunks, mock_usage)
        mock_embed.return_value = mock_embeddings

        note_content = b'Team standup was held at Amsterdam HQ on March 19.'
        b64_content = base64.b64encode(note_content).decode('utf-8')

        with patch(
            'memex_core.services.vaults.VaultService.resolve_vault_identifier',
            new_callable=AsyncMock,
            return_value=vault_id,
        ):
            ingest_resp = client.post(
                '/api/v1/ingestions',
                json={
                    'name': 'Standup Note',
                    'description': 'Daily standup',
                    'content': b64_content,
                    'files': {},
                    'tags': [],
                },
            )
            assert ingest_resp.status_code == 200, f'Ingest failed: {ingest_resp.text}'

    # Retrieve and verify 'Where: Amsterdam HQ' is in the stored memory unit text
    app = client.app
    real_embedder = app.state.api.embedder
    mock_embedder = MagicMock()
    mock_embedder.encode.return_value = [[0.1] * 384]
    app.state.api.embedder = mock_embedder

    try:
        retrieve_resp = client.post(
            '/api/v1/memories/search',
            json={'query': 'Amsterdam standup', 'limit': 5, 'vault_ids': [vault_name]},
        )
        assert retrieve_resp.status_code == 200, f'Retrieve failed: {retrieve_resp.text}'
        results = parse_ndjson(retrieve_resp.text)
        assert len(results) > 0, 'Expected at least one result'

        texts = [r['text'] for r in results]
        assert any('Where: Amsterdam HQ' in t for t in texts), (
            f'Expected "Where: Amsterdam HQ" in memory unit text. Got: {texts}'
        )
    finally:
        app.state.api.embedder = real_embedder


@pytest.mark.asyncio
async def test_where_in_memory_unit_searchable_via_keyword(db_session: AsyncSession) -> None:
    """Memory units with 'Where: <location>' in text are findable via KeywordStrategy."""
    from memex_core.config import GLOBAL_VAULT_ID
    from memex_core.memory.sql_models import MemoryUnit
    from memex_core.memory.retrieval.strategies import KeywordStrategy
    from uuid import uuid4

    # Insert a unit whose text includes 'Where: Reykjavik' (simulating the formatted_text output)
    unit = MemoryUnit(
        text=f'Conference attended | Where: Reykjavik, Iceland | Involving: Team {uuid4()}',
        event_date=datetime.now(timezone.utc),
        vault_id=GLOBAL_VAULT_ID,
        embedding=[0.0] * 384,
    )
    db_session.add(unit)
    await db_session.commit()
    await db_session.refresh(unit)

    strategy = KeywordStrategy()
    stmt = strategy.get_statement('Reykjavik', None, limit=10)
    result = await db_session.execute(stmt)
    rows = result.all()

    found_ids = [r[0] for r in rows]
    assert unit.id in found_ids, 'KeywordStrategy should find unit by location in text'
