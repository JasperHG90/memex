"""End-to-end HTTP test: GET /api/v1/notes?date_field=... must work.

The service-level integration tests (``packages/core/tests/integration/
test_int_list_notes_date_field.py``) already cover ``MemexAPI.list_notes``
against a real Postgres. This test covers the remaining hop — the FastAPI
query-param plumbing — so we know the full stack (HTTP → server →
service → SQL) routes the parameter correctly.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

import pytest
from uuid import uuid4
from datetime import datetime, timezone
from httpx import AsyncClient, ASGITransport

from memex_core.memory.sql_models import Note, Vault
from memex_core.server import app as server_app, lifespan


def _setup_env(postgres_container):
    dsn = postgres_container.get_connection_url()
    parsed = urlparse(dsn)
    os.environ['MEMEX_SERVER__META_STORE__TYPE'] = 'postgres'
    os.environ['MEMEX_SERVER__META_STORE__INSTANCE__HOST'] = parsed.hostname or 'localhost'
    os.environ['MEMEX_SERVER__META_STORE__INSTANCE__PORT'] = str(parsed.port or 5432)
    os.environ['MEMEX_SERVER__META_STORE__INSTANCE__DATABASE'] = parsed.path.lstrip('/')
    os.environ['MEMEX_SERVER__META_STORE__INSTANCE__USER'] = parsed.username or 'test'
    os.environ['MEMEX_SERVER__META_STORE__INSTANCE__PASSWORD'] = parsed.password or 'test'
    os.environ['MEMEX_LOAD_LOCAL_CONFIG'] = 'false'
    os.environ['MEMEX_LOAD_GLOBAL_CONFIG'] = 'false'


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_notes_http_date_field_created_at_excludes_future_publish(
    postgres_container, _truncate_db
):
    """Reproduces the production bug via HTTP.

    Seeds a TiinyAI-style note (created March, publish_date December) and
    asserts that ``?after=2026-04-23&date_field=created_at`` does NOT
    return it, but the legacy ``?after=2026-04-23`` (no date_field)
    does — preserving backward-compat.
    """
    _setup_env(postgres_container)

    from memex_core.storage.metastore import get_metastore
    from memex_core.config import parse_memex_config

    config = parse_memex_config()
    metastore = get_metastore(config.server.meta_store)
    await metastore.connect(create_schema=False)

    vault_id = uuid4()
    note_id_tiinyai = uuid4()
    note_id_today = uuid4()

    async with metastore.session() as session:
        session.add(Vault(id=vault_id, name='date-field-http-test'))
        await session.commit()

    async with metastore.session() as session:
        session.add(
            Note(
                id=note_id_tiinyai,
                content_hash='http-h1',
                vault_id=vault_id,
                original_text='misextracted publish date',
                doc_metadata={'name': 'TiinyAI-style'},
                created_at=datetime(2026, 3, 28, tzinfo=timezone.utc),
                publish_date=datetime(2026, 12, 3, tzinfo=timezone.utc),
            )
        )
        session.add(
            Note(
                id=note_id_today,
                content_hash='http-h2',
                vault_id=vault_id,
                original_text='today',
                doc_metadata={'name': 'Today'},
                created_at=datetime(2026, 4, 23, tzinfo=timezone.utc),
            )
        )
        await session.commit()

    await metastore.close()

    async with lifespan(server_app):
        async with AsyncClient(
            transport=ASGITransport(app=server_app), base_url='http://test'
        ) as client:
            # --- HTTP test: default behaviour (no date_field) is coalesce (legacy) ---
            response = await client.get(
                '/api/v1/notes',
                params={
                    'after': '2026-04-23T00:00:00',
                    'vault_id': [str(vault_id)],
                    'limit': 50,
                },
            )
            assert response.status_code == 200
            body = response.text
            # Coalesce picks publish_date=Dec 3 → TiinyAI note matches.
            assert str(note_id_tiinyai) in body
            assert str(note_id_today) in body

            # --- HTTP test: date_field=created_at excludes the TiinyAI note ---
            response = await client.get(
                '/api/v1/notes',
                params={
                    'after': '2026-04-23T00:00:00',
                    'vault_id': [str(vault_id)],
                    'date_field': 'created_at',
                    'limit': 50,
                },
            )
            assert response.status_code == 200
            body = response.text
            assert str(note_id_tiinyai) not in body, (
                'bug regression: TiinyAI-style note leaked through ?date_field=created_at'
            )
            assert str(note_id_today) in body

            # --- HTTP test: date_field=publish_date only returns notes with publish_date set ---
            response = await client.get(
                '/api/v1/notes',
                params={
                    'after': '2026-01-01T00:00:00',
                    'vault_id': [str(vault_id)],
                    'date_field': 'publish_date',
                    'limit': 50,
                },
            )
            assert response.status_code == 200
            body = response.text
            assert str(note_id_tiinyai) in body
            assert str(note_id_today) not in body, (
                'note without publish_date should not match ?date_field=publish_date'
            )

            # --- HTTP test: invalid date_field returns 422 (FastAPI Literal validation) ---
            response = await client.get(
                '/api/v1/notes',
                params={
                    'after': '2026-04-23T00:00:00',
                    'vault_id': [str(vault_id)],
                    'date_field': 'not-a-real-field',
                    'limit': 50,
                },
            )
            assert response.status_code == 422, (
                f'expected 422 for invalid date_field; got {response.status_code}: {response.text}'
            )
