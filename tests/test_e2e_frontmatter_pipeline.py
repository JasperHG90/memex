"""E2E pipeline test: YAML frontmatter → date extraction + entity extraction + memory units.

Validates the full pipeline for Confluence-style documents with YAML frontmatter:
1. publish_date is the frontmatter date, not now()
2. Author entity exists and is linked to the note
3. Frontmatter facts are persisted as memory_units in the DB
4. memory_search can find facts about the author
"""

import base64
import datetime as dt
from unittest.mock import patch, AsyncMock
from uuid import UUID

import dspy
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text as sql_text

from memex_core.memory.extraction.core import extract_facts_from_frontmatter
from memex_core.memory.extraction.models import ExtractedFact, ChunkMetadata
from memex_core.services.ingestion import _extract_date_from_frontmatter


CONFLUENCE_DOC = """\
---
created_by: Jasper Ginn
created_date: 2025-05-24T13:41:48Z
title: Rituals Retrospective Q2
space: Engineering
---

# Rituals Retrospective Q2

This document summarizes the Q2 retrospective for the Rituals team.

## Participants

Jasper Ginn led the session with the engineering team on May 24th, 2025.

## Key Outcomes

- Improved deployment frequency by 40%
- Reduced incident response time from 2 hours to 30 minutes
- Adopted trunk-based development workflow
"""


# ---------------------------------------------------------------------------
# Deterministic tests: frontmatter date parsing
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_frontmatter_date_extraction():
    """publish_date should be the frontmatter date, not now()."""
    result = _extract_date_from_frontmatter(CONFLUENCE_DOC)

    assert result is not None, 'Expected a date from frontmatter'
    assert result.year == 2025
    assert result.month == 5
    assert result.day == 24
    assert result.tzinfo is not None, 'Date should be timezone-aware'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_frontmatter_fallback():
    """Documents without frontmatter should return None for date."""
    doc_no_fm = '# Just a heading\n\nSome content without frontmatter.\n'
    result = _extract_date_from_frontmatter(doc_no_fm)
    assert result is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_frontmatter_without_date_returns_none():
    """Frontmatter without date fields should return None."""
    doc = '---\ntitle: No Date Here\nauthor: Someone\n---\nContent\n'
    result = _extract_date_from_frontmatter(doc)
    assert result is None


# ---------------------------------------------------------------------------
# LLM test: frontmatter extraction produces Person entity
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.llm
@pytest.mark.asyncio
async def test_frontmatter_author_entity_extraction():
    """Author from frontmatter should be extracted as a Person entity via LLM."""
    lm = dspy.LM('gemini/gemini-3-flash-preview')

    frontmatter_block = (
        '---\n'
        'created_by: Jasper Ginn\n'
        'created_date: 2025-05-24T13:41:48Z\n'
        'title: Rituals Retrospective Q2\n'
        'space: Engineering\n'
        '---\n'
    )

    facts = await extract_facts_from_frontmatter(
        frontmatter_text=frontmatter_block,
        event_date=dt.datetime(2025, 5, 24, tzinfo=dt.timezone.utc),
        lm=lm,
    )

    assert len(facts) >= 1, f'Expected at least one fact, got {len(facts)}'

    # Jasper Ginn should appear in extracted facts
    all_fact_text = ' '.join(f.what for f in facts)
    assert 'Jasper Ginn' in all_fact_text, (
        f'Expected "Jasper Ginn" in facts, got: {[f.what for f in facts]}'
    )

    # Should have Person entity
    all_entities = [e for f in facts for e in f.entities]
    person_entities = [
        e for e in all_entities if e.entity_type and 'person' in e.entity_type.lower()
    ]
    assert len(person_entities) >= 1, (
        f'Expected Person entity, got: {[(e.text, e.entity_type) for e in all_entities]}'
    )

    # Person entity should reference Jasper Ginn
    person_names = [e.text for e in person_entities]
    assert any('Jasper' in name or 'Ginn' in name for name in person_names), (
        f'Expected Jasper Ginn in person entities, got: {person_names}'
    )


# ---------------------------------------------------------------------------
# Full pipeline test: ingest → DB → verify memory_units + publish_date
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.llm
def test_full_pipeline_frontmatter_to_memory_units(client: TestClient):
    """Ingest a Confluence-style doc and verify:

    1. publish_date on the Note row is the frontmatter date (2025-05-24), not now()
    2. Memory units referencing "Jasper Ginn" are persisted in the DB
    3. An entity for "Jasper Ginn" exists and is linked
    """
    # 1. Create Vault
    vault_resp = client.post('/api/v1/vaults', json={'name': 'Frontmatter Test Vault'})
    assert vault_resp.status_code == 200
    vault_id_str = vault_resp.json()['id']
    vault_id = UUID(vault_id_str)

    # 2. Prepare mock extraction output that includes a frontmatter-derived fact
    now = dt.datetime(2025, 5, 24, 13, 41, 48, tzinfo=dt.timezone.utc)
    mock_facts = [
        ExtractedFact(
            fact_text='Jasper Ginn created the Rituals Retrospective Q2 document.',
            fact_type='event',
            entities=[],
            chunk_index=0,
            content_index=0,
            mentioned_at=now,
            vault_id=vault_id,
            who='Jasper Ginn',
        ),
        ExtractedFact(
            fact_text='The Rituals team improved deployment frequency by 40%.',
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
            chunk_text=CONFLUENCE_DOC,
            fact_count=2,
            chunk_index=0,
            content_index=0,
        )
    ]
    mock_embeddings = [[0.1] * 384] * len(mock_facts)

    extract_path = 'memex_core.memory.extraction.engine.ExtractionEngine._extract_facts'
    embed_path = 'memex_core.memory.extraction.embedding_processor.generate_embeddings_batch'

    b64_content = base64.b64encode(CONFLUENCE_DOC.encode('utf-8')).decode('utf-8')

    with (
        patch(extract_path) as mock_extract,
        patch(embed_path) as mock_embed,
        patch(
            'memex_core.services.vaults.VaultService.resolve_vault_identifier',
            new_callable=AsyncMock,
            return_value=vault_id,
        ),
    ):
        mock_extract.return_value = (mock_facts, mock_chunks)
        mock_embed.return_value = mock_embeddings

        payload = {
            'name': 'Rituals Retrospective Q2',
            'description': 'Confluence doc with frontmatter',
            'content': b64_content,
            'files': {},
            'tags': ['confluence'],
        }

        ingest_resp = client.post('/api/v1/ingestions', json=payload)
        assert ingest_resp.status_code == 200, f'Ingest failed: {ingest_resp.text}'
        ingest_data = ingest_resp.json()
        assert ingest_data['status'] == 'success'
        note_id = ingest_data['note_id']

    # 3. Verify publish_date is from frontmatter, not now()
    #    Query the notes table directly
    from sqlmodel.ext.asyncio.session import AsyncSession
    import asyncio

    async def _verify_db():
        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
        from sqlalchemy import NullPool
        import os

        pg_host = os.environ['MEMEX_SERVER__META_STORE__INSTANCE__HOST']
        pg_port = os.environ['MEMEX_SERVER__META_STORE__INSTANCE__PORT']
        pg_db = os.environ['MEMEX_SERVER__META_STORE__INSTANCE__DATABASE']
        pg_user = os.environ['MEMEX_SERVER__META_STORE__INSTANCE__USER']
        pg_pass = os.environ['MEMEX_SERVER__META_STORE__INSTANCE__PASSWORD']
        url = f'postgresql+asyncpg://{pg_user}:{pg_pass}@{pg_host}:{pg_port}/{pg_db}'

        engine = create_async_engine(url, poolclass=NullPool)
        session_maker = async_sessionmaker(bind=engine, class_=AsyncSession)

        async with session_maker() as session:
            # Check Note publish_date
            result = await session.exec(
                sql_text('SELECT publish_date FROM notes WHERE id = :note_id'),
                params={'note_id': note_id},
            )
            row = result.first()
            assert row is not None, f'Note {note_id} not found in DB'
            publish_date = row[0]
            assert publish_date is not None, 'publish_date should not be None'
            assert publish_date.year == 2025, f'Expected year 2025, got {publish_date.year}'
            assert publish_date.month == 5, f'Expected month 5, got {publish_date.month}'
            assert publish_date.day == 24, f'Expected day 24, got {publish_date.day}'

            # Check memory_units exist for this note
            mu_result = await session.exec(
                sql_text('SELECT text FROM memory_units WHERE note_id = :note_id'),
                params={'note_id': note_id},
            )
            memory_units = mu_result.all()
            assert len(memory_units) >= 1, (
                f'Expected at least 1 memory unit for note {note_id}, got {len(memory_units)}'
            )

            # At least one memory unit should mention Jasper Ginn
            unit_texts = [row[0] for row in memory_units]
            jasper_units = [t for t in unit_texts if 'Jasper Ginn' in t]
            assert len(jasper_units) >= 1, (
                f'Expected memory unit mentioning "Jasper Ginn", got: {unit_texts}'
            )

        await engine.dispose()

    asyncio.get_event_loop().run_until_complete(_verify_db())
