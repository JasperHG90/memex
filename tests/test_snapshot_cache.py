"""Tests for the eval suite snapshot cache + the V3 server export route.

These exercise the cache resolution + lookup helpers (unit-scope) AND the
``POST /api/v1/_eval/snapshot-export`` route end-to-end via TestClient
against a fresh FastAPI app.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.config import OnnxBackend
from memex_core.memory.sql_models import Note, Vault
from memex_eval.suite import snapshot_cache as _cache


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


# ----------------------------------------------------------------------
# Unit tests for the cache module


class TestCacheResolution:
    def test_explicit_wins(self, tmp_path: Path) -> None:
        os.environ.pop('MEMEX_EVAL_SNAPSHOT_ROOT', None)
        explicit = tmp_path / 'explicit'
        root = _cache.resolve_cache_root(explicit)
        assert root == explicit.resolve()
        assert root.is_dir()

    def test_env_var(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        env_root = tmp_path / 'from-env'
        monkeypatch.setenv('MEMEX_EVAL_SNAPSHOT_ROOT', str(env_root))
        root = _cache.resolve_cache_root(None)
        assert root == env_root.resolve()
        assert root.is_dir()

    def test_platformdirs_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv('MEMEX_EVAL_SNAPSHOT_ROOT', raising=False)
        # Don't actually create the user cache dir during tests; just
        # confirm the helper returns a Path under the expected name.
        default = _cache.default_cache_root()
        assert 'memex-eval' in str(default)

    def test_cache_key_truncates_hash(self) -> None:
        key = _cache.cache_key('basic_extraction', 'a' * 64)
        assert key == 'basic_extraction-' + 'a' * 16


class TestCacheLookup:
    def test_miss_when_dir_absent(self, tmp_path: Path) -> None:
        result = _cache.lookup(tmp_path, 'suite_a', 'abc')
        assert not result.hit
        assert result.cache_path.parent == tmp_path

    def test_hit_when_manifest_and_marker_present(self, tmp_path: Path) -> None:
        full_hash = 'abc' + 'x' * 13  # truncated to 16 by cache_key
        path = tmp_path / f'suite_a-{full_hash}'
        path.mkdir()
        (path / 'manifest.json').write_text('{}', encoding='utf-8')
        _cache.mark_complete(path)
        result = _cache.lookup(tmp_path, 'suite_a', full_hash)
        assert result.cache_path == path
        assert result.hit

    def test_miss_when_marker_absent(self, tmp_path: Path) -> None:
        full_hash = 'abc' + 'x' * 13
        path = tmp_path / f'suite_a-{full_hash}'
        path.mkdir()
        (path / 'manifest.json').write_text('{}', encoding='utf-8')
        result = _cache.lookup(tmp_path, 'suite_a', full_hash)
        assert not result.hit

    def test_miss_when_manifest_absent(self, tmp_path: Path) -> None:
        full_hash = 'abc' + 'x' * 13
        path = tmp_path / f'suite_a-{full_hash}'
        path.mkdir()
        _cache.mark_complete(path)
        result = _cache.lookup(tmp_path, 'suite_a', full_hash)
        assert not result.hit

    def test_clear_entry_idempotent(self, tmp_path: Path) -> None:
        path = tmp_path / 'gone'
        # Idempotent on missing.
        _cache.clear_cache_entry(path)
        path.mkdir()
        (path / 'x').write_text('y')
        _cache.clear_cache_entry(path)
        assert not path.exists()


# ----------------------------------------------------------------------
# Server route tests — POST /api/v1/_eval/snapshot-export


def _build_app_with_eval_routes(db_session: AsyncSession):
    """Construct a stand-in FastAPI app that mounts the eval-snapshot
    router and exposes a fake ``app.state.api`` whose metastore points
    at the test session's bound engine."""
    from contextlib import asynccontextmanager

    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from sqlmodel.ext.asyncio.session import AsyncSession as SQLModelSession

    from memex_core.server.eval_snapshot import router as eval_router

    bind = db_session.bind
    assert bind is not None
    maker = async_sessionmaker(bind=bind, class_=SQLModelSession, expire_on_commit=False)

    class _Cfg:
        class server:
            eval_mode = True
            embedding_model = OnnxBackend()

    class _MS:
        engine = bind

        @staticmethod
        def session():
            @asynccontextmanager
            async def _ctx():
                async with maker() as s:
                    yield s

            return _ctx()

    class _Api:
        config = _Cfg()
        metastore = _MS()
        filestore = None

    app = FastAPI()
    app.state.api = _Api()
    app.include_router(eval_router)
    return app


@pytest_asyncio.fixture
async def small_vault(db_session: AsyncSession) -> dict[str, UUID]:
    """Minimal vault used by export-route tests."""
    vault = Vault(name='cache-export-test', description='cache test')
    db_session.add(vault)
    await db_session.flush()
    import hashlib

    body = 'cache body'
    note = Note(
        id=uuid4(),
        vault_id=vault.id,
        title='cache note',
        original_text=body,
        content_hash=hashlib.md5(body.encode('utf-8')).hexdigest(),
        status='active',
    )
    db_session.add(note)
    await db_session.commit()
    return {'vault_id': vault.id, 'note_id': note.id}


async def test_eval_export_route_writes_snapshot(
    db_session: AsyncSession,
    small_vault: dict[str, UUID],
    tmp_path: Path,
) -> None:
    """POST /snapshot-export → writes a complete snapshot dir under the
    allowlist root."""
    from fastapi.testclient import TestClient

    allowlist = tmp_path / 'cache'
    allowlist.mkdir()
    output_dir = allowlist / 'snap-1'

    os.environ['MEMEX_EVAL_SNAPSHOT_ROOT'] = str(allowlist)
    try:
        app = _build_app_with_eval_routes(db_session)
        with TestClient(app) as client:
            response = client.post(
                '/api/v1/_eval/snapshot-export',
                json={
                    'vault_id_or_name': str(small_vault['vault_id']),
                    'output_path': str(output_dir),
                },
            )
            assert response.status_code == 201, response.text
            body = response.json()
            assert body['snapshot_path'] == str(output_dir.resolve())
            assert body['snapshot_version'].startswith('1.')
            assert body['table_counts']['notes'] == 1

        assert (output_dir / 'manifest.json').is_file()
        assert (output_dir / 'vault.json').is_file()
        manifest = json.loads((output_dir / 'manifest.json').read_text())
        assert manifest['source_vault_name'] == 'cache-export-test'
    finally:
        os.environ.pop('MEMEX_EVAL_SNAPSHOT_ROOT', None)


async def test_eval_export_route_refuses_path_outside_allowlist(
    db_session: AsyncSession,
    small_vault: dict[str, UUID],
    tmp_path: Path,
) -> None:
    from fastapi.testclient import TestClient

    allowlist = tmp_path / 'cache'
    allowlist.mkdir()
    outside = tmp_path / 'outside'

    os.environ['MEMEX_EVAL_SNAPSHOT_ROOT'] = str(allowlist)
    try:
        app = _build_app_with_eval_routes(db_session)
        with TestClient(app) as client:
            response = client.post(
                '/api/v1/_eval/snapshot-export',
                json={
                    'vault_id_or_name': str(small_vault['vault_id']),
                    'output_path': str(outside),
                },
            )
            assert response.status_code == 400
            assert 'Internal error' not in response.text
    finally:
        os.environ.pop('MEMEX_EVAL_SNAPSHOT_ROOT', None)


async def test_eval_export_route_refuses_overwrite_existing_snapshot(
    db_session: AsyncSession,
    small_vault: dict[str, UUID],
    tmp_path: Path,
) -> None:
    """Re-exporting into a dir that already has manifest.json refuses
    (the V3 contract; cache-populate must clear first)."""
    from fastapi.testclient import TestClient

    allowlist = tmp_path / 'cache'
    allowlist.mkdir()
    output_dir = allowlist / 'snap-existing'
    output_dir.mkdir()
    (output_dir / 'manifest.json').write_text('{}', encoding='utf-8')

    os.environ['MEMEX_EVAL_SNAPSHOT_ROOT'] = str(allowlist)
    try:
        app = _build_app_with_eval_routes(db_session)
        with TestClient(app) as client:
            response = client.post(
                '/api/v1/_eval/snapshot-export',
                json={
                    'vault_id_or_name': str(small_vault['vault_id']),
                    'output_path': str(output_dir),
                },
            )
            # SnapshotExportError → 409
            assert response.status_code == 409
    finally:
        os.environ.pop('MEMEX_EVAL_SNAPSHOT_ROOT', None)


async def test_cache_round_trip_export_then_import(
    db_session: AsyncSession,
    small_vault: dict[str, UUID],
    tmp_path: Path,
) -> None:
    """Export → mark complete → lookup hits → import succeeds.

    Exercises the full cache lifecycle the auto-mode runner relies on.
    """
    from fastapi.testclient import TestClient

    from memex_core.services.snapshot import (
        SnapshotImporter,
        ensure_eval_import_state_table,
    )

    # Apply the eval_import_state DDL once for the import side.
    conn = await db_session.connection()
    await ensure_eval_import_state_table(conn)
    await db_session.commit()

    allowlist = tmp_path / 'cache'
    allowlist.mkdir()
    cache_root = _cache.resolve_cache_root(allowlist)
    cache_path = cache_root / _cache.cache_key('demo-suite', 'a' * 64)

    os.environ['MEMEX_EVAL_SNAPSHOT_ROOT'] = str(allowlist)
    try:
        app = _build_app_with_eval_routes(db_session)
        with TestClient(app) as client:
            response = client.post(
                '/api/v1/_eval/snapshot-export',
                json={
                    'vault_id_or_name': str(small_vault['vault_id']),
                    'output_path': str(cache_path),
                },
            )
            assert response.status_code == 201, response.text
        # Mark complete + verify lookup hits.
        _cache.mark_complete(cache_path)
        result = _cache.lookup(cache_root, 'demo-suite', 'a' * 64)
        assert result.hit
        assert result.cache_path == cache_path

        # Now drop the source vault and re-import via V12.
        from sqlalchemy import delete

        await db_session.execute(delete(Note).where(Note.vault_id == small_vault['vault_id']))
        await db_session.execute(delete(Vault).where(Vault.id == small_vault['vault_id']))
        await db_session.commit()

        importer = SnapshotImporter(
            session=db_session,
            filestore=None,
            embedding_backend=OnnxBackend(),
            snapshot_dir=cache_path,
            allowlist_root=allowlist,
            target_vault_name='cache-import-target',
        )
        target = await importer.import_snapshot()
        assert target is not None
    finally:
        os.environ.pop('MEMEX_EVAL_SNAPSHOT_ROOT', None)


async def test_eval_export_route_absent_when_eval_mode_off(client) -> None:
    """The shared `client` fixture starts the app with eval_mode=False
    (per conftest); the export route must not be reachable."""
    response = client.post(
        '/api/v1/_eval/snapshot-export',
        json={'vault_id_or_name': 'whatever', 'output_path': '/tmp/x'},
    )
    assert response.status_code == 404


async def test_export_route_resolves_vault_name(
    db_session: AsyncSession,
    small_vault: dict[str, UUID],
    tmp_path: Path,
) -> None:
    """Vault selector accepts a name string in addition to UUID."""
    from fastapi.testclient import TestClient

    allowlist = tmp_path / 'cache'
    allowlist.mkdir()
    output_dir = allowlist / 'by-name'

    os.environ['MEMEX_EVAL_SNAPSHOT_ROOT'] = str(allowlist)
    try:
        app = _build_app_with_eval_routes(db_session)
        with TestClient(app) as client:
            response = client.post(
                '/api/v1/_eval/snapshot-export',
                json={
                    'vault_id_or_name': 'cache-export-test',
                    'output_path': str(output_dir),
                },
            )
            assert response.status_code == 201, response.text
        assert (output_dir / 'manifest.json').is_file()
    finally:
        os.environ.pop('MEMEX_EVAL_SNAPSHOT_ROOT', None)
