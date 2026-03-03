"""Integration tests for Alembic migrations against a real PostgreSQL database.

These tests verify that:
1. Migrations apply cleanly from scratch (001 → 002 → head)
2. Migration 002 correctly handles opinion→world and experience→event renames
3. The alembic_version stamp persists after migration

Alembic commands are run via subprocess to avoid nested event loop conflicts
with pytest-asyncio.
"""

import os
import subprocess
import sys
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import create_async_engine


def _run_alembic(postgres_uri: str, *args: str) -> subprocess.CompletedProcess:
    """Run an alembic command in a subprocess with the test DB URL."""
    from memex_core.migration import _PACKAGE_DIR

    ini_path = str(_PACKAGE_DIR / 'alembic.ini')
    env = {**os.environ, 'MEMEX_DATABASE_URL': postgres_uri}
    result = subprocess.run(
        [sys.executable, '-m', 'alembic', '-c', ini_path, *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f'alembic {" ".join(args)} failed:\nstdout: {result.stdout}\nstderr: {result.stderr}'
        )
    return result


@pytest_asyncio.fixture()
async def clean_db(postgres_uri):
    """Drop all tables so migrations start from scratch."""
    engine = create_async_engine(postgres_uri, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text('DROP SCHEMA public CASCADE'))
        await conn.execute(text('CREATE SCHEMA public'))
        await conn.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
        await conn.execute(text('CREATE EXTENSION IF NOT EXISTS pg_trgm'))
    await engine.dispose()


@pytest.mark.integration
class TestMigration002:
    """Test migration 002: remove opinions, rename experience→event."""

    @pytest.mark.asyncio
    async def test_upgrade_from_001_stamps_version(self, clean_db, postgres_uri):
        """Running upgrade stamps alembic_version at each step."""
        engine = create_async_engine(postgres_uri, poolclass=NullPool)

        # Apply migration 001
        _run_alembic(postgres_uri, 'upgrade', '001_full_baseline')

        async with engine.connect() as conn:
            result = await conn.execute(text('SELECT version_num FROM alembic_version'))
            assert result.scalar() == '001_full_baseline'

        # Apply migration 002
        _run_alembic(postgres_uri, 'upgrade', '002_remove_opinions_rename_event')

        async with engine.connect() as conn:
            result = await conn.execute(text('SELECT version_num FROM alembic_version'))
            assert result.scalar() == '002_remove_opinions_rename_event'

        await engine.dispose()

    @pytest.mark.asyncio
    async def test_upgrade_converts_opinion_to_world(self, clean_db, postgres_uri):
        """Migration 002 converts opinion facts to world, experience to event."""
        engine = create_async_engine(postgres_uri, poolclass=NullPool)

        # Apply migration 001
        _run_alembic(postgres_uri, 'upgrade', '001_full_baseline')

        # Insert test data with old fact types
        vault_id = uuid4()
        unit_id_opinion = uuid4()
        unit_id_experience = uuid4()
        unit_id_world = uuid4()

        async with engine.begin() as conn:
            await conn.execute(
                text('INSERT INTO vaults (id, name, description) VALUES (:id, :name, :desc)'),
                {'id': str(vault_id), 'name': f'test-vault-{uuid4()}', 'desc': 'test'},
            )

            # Migration 001 uses create_all which applies current model constraints.
            # Replace with old-style constraint that allows opinion/experience.
            await conn.execute(
                text(
                    'DO $$ DECLARE r RECORD; BEGIN '
                    'FOR r IN (SELECT conname FROM pg_constraint '
                    "WHERE conrelid = 'memory_units'::regclass AND contype = 'c' "
                    "AND pg_get_constraintdef(oid) LIKE '%fact_type%') "
                    "LOOP EXECUTE 'ALTER TABLE memory_units DROP CONSTRAINT ' || r.conname; "
                    'END LOOP; END $$'
                )
            )
            await conn.execute(
                text(
                    'ALTER TABLE memory_units ADD CONSTRAINT memory_units_fact_type_check '
                    "CHECK (fact_type IN ('world', 'experience', 'opinion', 'observation'))"
                )
            )

            for uid, ftype in [
                (unit_id_opinion, 'opinion'),
                (unit_id_experience, 'experience'),
                (unit_id_world, 'world'),
            ]:
                await conn.execute(
                    text(
                        'INSERT INTO memory_units '
                        '(id, text, fact_type, vault_id, embedding, event_date) '
                        'VALUES (:id, :text, :ft, :vid, :emb, NOW())'
                    ),
                    {
                        'id': str(uid),
                        'text': f'Test fact {uid}',
                        'ft': ftype,
                        'vid': str(vault_id),
                        'emb': str([0.1] * 384),
                    },
                )

        # Apply migration 002
        _run_alembic(postgres_uri, 'upgrade', '002_remove_opinions_rename_event')

        # Verify fact types were converted
        async with engine.connect() as conn:
            result = await conn.execute(
                text('SELECT id::text, fact_type FROM memory_units ORDER BY text')
            )
            rows = {r[0]: r[1] for r in result.fetchall()}

            assert rows[str(unit_id_opinion)] == 'world'
            assert rows[str(unit_id_experience)] == 'event'
            assert rows[str(unit_id_world)] == 'world'

            # Verify evidence_log table was dropped
            result = await conn.execute(
                text(
                    'SELECT EXISTS ('
                    '  SELECT 1 FROM information_schema.tables '
                    "  WHERE table_name = 'evidence_log'"
                    ')'
                )
            )
            assert result.scalar() is False

        await engine.dispose()

    @pytest.mark.asyncio
    async def test_full_upgrade_to_head(self, clean_db, postgres_uri):
        """Running upgrade to head from scratch works end-to-end."""
        _run_alembic(postgres_uri, 'upgrade', 'head')

        engine = create_async_engine(postgres_uri, poolclass=NullPool)
        async with engine.connect() as conn:
            result = await conn.execute(text('SELECT version_num FROM alembic_version'))
            assert result.scalar() == '002_remove_opinions_rename_event'
        await engine.dispose()

    @pytest.mark.asyncio
    async def test_stamp_persists(self, clean_db, postgres_uri):
        """Stamping a revision persists in alembic_version."""
        # Apply 001 first
        _run_alembic(postgres_uri, 'upgrade', '001_full_baseline')

        # Stamp to 002
        _run_alembic(postgres_uri, 'stamp', '002_remove_opinions_rename_event')

        engine = create_async_engine(postgres_uri, poolclass=NullPool)
        async with engine.connect() as conn:
            result = await conn.execute(text('SELECT version_num FROM alembic_version'))
            assert result.scalar() == '002_remove_opinions_rename_event'
        await engine.dispose()
