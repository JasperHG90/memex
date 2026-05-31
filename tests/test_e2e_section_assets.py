"""E2E: per-section image refs (#196) land on Node.assets, the page-index
TOC, and NodeDTO via the read path."""

import base64
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from memex_core.memory.extraction.models import (
    PageIndexBlock,
    PageIndexOutput,
    RawFact,
    TOCNode,
)
from memex_core.memory.sql_models import Node


def _section(idx: int, title: str, body: str) -> tuple[TOCNode, PageIndexBlock]:
    content = f'# {title}\n{body}'
    node = TOCNode(
        id=f'node-{idx}',
        title=title,
        level=1,
        reasoning='section',
        content=content,
        children=[],
        original_header_id=idx,
    )
    block = PageIndexBlock(
        id=f'block-{idx}',
        seq=idx,
        token_count=80,
        start_index=idx * 100,
        end_index=idx * 100 + 50,
        titles_included=[title],
        content=content,
        node_id=f'node-{idx}',
    )
    return node, block


@pytest.mark.integration
@pytest.mark.asyncio
async def test_section_assets_persist_and_surface(client: TestClient, db_session):
    n1, b1 = _section(0, 'Architecture', 'See ![the diagram](img/arch.png) for the layout.')
    n2, b2 = _section(1, 'Results', 'Throughput ![chart](img/results.png "Q3 numbers").')
    n3, b3 = _section(2, 'Plain', 'No images in this section.')

    mock_output = PageIndexOutput(
        toc=[n1, n2, n3],
        blocks=[b1, b2, b3],
        node_to_block_map={'node-0': 'block-0', 'node-1': 'block-1', 'node-2': 'block-2'},
        coverage_ratio=1.0,
        path_used='mock_path',
    )
    mock_facts = [
        RawFact(what='f0', fact_type='world', entities=[], chunk_index=0, content_index=0),
    ]

    with (
        patch('memex_core.memory.extraction.engine.index_document') as mock_index_doc,
        patch('memex_core.memory.extraction.engine.extract_facts_from_chunks') as mock_extract,
        patch(
            'memex_core.memory.extraction.embedding_processor.generate_embeddings_batch'
        ) as mock_embed,
        patch(
            'memex_core.services.ingestion.extract_document_date',
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            'memex_core.services.ingestion.resolve_document_title',
            new_callable=AsyncMock,
            return_value='Assets Doc',
        ),
        patch(
            'memex_core.memory.contradiction.engine.ContradictionEngine.detect_contradictions',
            return_value=None,
        ),
    ):
        mock_index_doc.return_value = mock_output
        mock_extract.return_value = (mock_facts, [('content', 1)])
        mock_embed.side_effect = lambda _backend, texts: [[0.1] * 384 for _ in texts]

        app = client.app
        original = app.state.api.config.server.memory.extraction.text_splitting.strategy
        app.state.api.config.server.memory.extraction.text_splitting.strategy = 'page_index'
        try:
            content = '# Architecture\nbody'
            payload = {
                'name': 'Assets Doc',
                'description': 'Section assets test',
                'content': base64.b64encode(content.encode()).decode('utf-8'),
                'tags': ['test'],
            }
            resp = client.post('/api/v1/ingestions', json=payload)
            assert resp.status_code == 200
            doc_id = UUID(resp.json()['note_id'])

            # 1. nodes.assets persisted from markdown.
            nodes = (await db_session.exec(select(Node).where(Node.note_id == doc_id))).all()
            by_title = {n.title: n for n in nodes}
            assert by_title['Architecture'].assets == [
                {
                    'path': 'img/arch.png',
                    'alt_text': 'the diagram',
                    'filename': 'arch.png',
                    'scope': 'node',
                }
            ]
            assert by_title['Results'].assets[0]['alt_text'] == 'chart'
            assert by_title['Results'].assets[0]['filename'] == 'results.png'
            assert by_title['Plain'].assets == []

            # 2. page-index TOC carries assets (zero extra calls for the agent).
            pi_resp = client.get(f'/api/v1/notes/{doc_id}/page-index')
            assert pi_resp.status_code == 200
            toc_by_title = {n['title']: n for n in pi_resp.json()['page_index']['toc']}
            assert toc_by_title['Architecture']['assets'][0]['path'] == 'img/arch.png'
            assert toc_by_title['Plain']['assets'] == []

            # 3. NodeDTO read path (HTTP) surfaces assets.
            node_resp = client.get(f'/api/v1/nodes/{by_title["Results"].id}')
            assert node_resp.status_code == 200
            dto = node_resp.json()
            assert dto['assets'][0]['filename'] == 'results.png'
            assert dto['assets'][0]['alt_text'] == 'chart'
        finally:
            app.state.api.config.server.memory.extraction.text_splitting.strategy = original


@pytest.mark.integration
@pytest.mark.asyncio
async def test_backfill_section_assets_populates_and_is_idempotent(client: TestClient, db_session):
    """The CLI backfill re-derives assets for nodes whose assets are empty."""
    from memex_cli.db import _backfill_section_assets

    n1, b1 = _section(0, 'Diagram', 'Here ![the schema](img/schema.png) is.')
    mock_output = PageIndexOutput(
        toc=[n1],
        blocks=[b1],
        node_to_block_map={'node-0': 'block-0'},
        coverage_ratio=1.0,
        path_used='mock_path',
    )
    mock_facts = [RawFact(what='f', fact_type='world', entities=[], chunk_index=0, content_index=0)]

    with (
        patch('memex_core.memory.extraction.engine.index_document') as mock_index_doc,
        patch('memex_core.memory.extraction.engine.extract_facts_from_chunks') as mock_extract,
        patch(
            'memex_core.memory.extraction.embedding_processor.generate_embeddings_batch'
        ) as mock_embed,
        patch(
            'memex_core.services.ingestion.extract_document_date',
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            'memex_core.services.ingestion.resolve_document_title',
            new_callable=AsyncMock,
            return_value='Backfill Doc',
        ),
        patch(
            'memex_core.memory.contradiction.engine.ContradictionEngine.detect_contradictions',
            return_value=None,
        ),
    ):
        mock_index_doc.return_value = mock_output
        mock_extract.return_value = (mock_facts, [('content', 1)])
        mock_embed.side_effect = lambda _backend, texts: [[0.1] * 384 for _ in texts]

        app = client.app
        config = app.state.api.config
        original = config.server.memory.extraction.text_splitting.strategy
        config.server.memory.extraction.text_splitting.strategy = 'page_index'
        try:
            payload = {
                'name': 'Backfill Doc',
                'description': 'Backfill test',
                'content': base64.b64encode(b'# Diagram\nbody').decode('utf-8'),
                'tags': ['test'],
            }
            resp = client.post('/api/v1/ingestions', json=payload)
            assert resp.status_code == 200
            doc_id = UUID(resp.json()['note_id'])

            # Simulate pre-feature nodes: clear the assets the ingest populated.
            nodes = (await db_session.exec(select(Node).where(Node.note_id == doc_id))).all()
            for node in nodes:
                node.assets = []
                db_session.add(node)
            await db_session.commit()

            # Backfill re-derives them from the stored section text.
            scanned, updated = await _backfill_section_assets(config, None)
            assert updated >= 1

            await db_session.commit()  # release the read snapshot
            refreshed = (await db_session.exec(select(Node).where(Node.note_id == doc_id))).all()
            diagram = next(n for n in refreshed if n.title == 'Diagram')
            await db_session.refresh(diagram)
            assert diagram.assets[0]['path'] == 'img/schema.png'
            assert diagram.assets[0]['alt_text'] == 'the schema'

            # Idempotent: a second run finds nothing left to populate.
            _, updated_again = await _backfill_section_assets(config, None)
            assert updated_again == 0
        finally:
            config.server.memory.extraction.text_splitting.strategy = original
