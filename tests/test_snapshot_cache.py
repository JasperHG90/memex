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

    def test_miss_when_marker_absent_and_cleans_partial(self, tmp_path: Path) -> None:
        full_hash = 'abc' + 'x' * 13
        path = tmp_path / f'suite_a-{full_hash}'
        path.mkdir()
        (path / 'manifest.json').write_text('{}', encoding='utf-8')
        result = _cache.lookup(tmp_path, 'suite_a', full_hash)
        assert not result.hit
        # Partial entry must be cleaned so a subsequent populate doesn't
        # commingle stale + fresh files.
        assert not path.exists()

    def test_miss_when_manifest_absent_and_cleans_partial(self, tmp_path: Path) -> None:
        full_hash = 'abc' + 'x' * 13
        path = tmp_path / f'suite_a-{full_hash}'
        path.mkdir()
        _cache.mark_complete(path)
        result = _cache.lookup(tmp_path, 'suite_a', full_hash)
        assert not result.hit
        assert not path.exists()

    def test_clear_entry_idempotent(self, tmp_path: Path) -> None:
        path = tmp_path / 'gone'
        # Idempotent on missing.
        _cache.clear_cache_entry(path)
        path.mkdir()
        (path / 'x').write_text('y')
        _cache.clear_cache_entry(path)
        assert not path.exists()


class TestAtomicPublish:
    def test_stage_path_creates_unique_tmp_dirs(self, tmp_path: Path) -> None:
        a = _cache.stage_path(tmp_path, 'suite-deadbeefcafef00d')
        b = _cache.stage_path(tmp_path, 'suite-deadbeefcafef00d')
        assert a != b
        assert a.is_dir() and b.is_dir()
        assert a.name.startswith('.tmp-suite-deadbeefcafef00d-')

    def test_publish_into_empty_slot(self, tmp_path: Path) -> None:
        staged = _cache.stage_path(tmp_path, 'k')
        (staged / 'manifest.json').write_text('{}', encoding='utf-8')
        _cache.mark_complete(staged)
        final = tmp_path / 'k'
        _cache.publish(staged, final)
        assert not staged.exists()
        assert (final / 'manifest.json').is_file()
        assert (final / _cache.CACHE_COMPLETE_MARKER).is_file()

    def test_publish_replaces_existing(self, tmp_path: Path) -> None:
        # Pre-existing cache entry with old content.
        final = tmp_path / 'k'
        final.mkdir()
        (final / 'manifest.json').write_text('{"old": true}', encoding='utf-8')
        _cache.mark_complete(final)
        # Stage a new export with different content.
        staged = _cache.stage_path(tmp_path, 'k')
        (staged / 'manifest.json').write_text('{"new": true}', encoding='utf-8')
        _cache.mark_complete(staged)
        _cache.publish(staged, final)
        assert not staged.exists()
        loaded = json.loads((final / 'manifest.json').read_text())
        assert loaded == {'new': True}
        # No stray .old- prefixes left over after a successful publish.
        assert not any(p.name.startswith('.old-') for p in tmp_path.iterdir())

    def test_publish_refuses_unmarked_staging(self, tmp_path: Path) -> None:
        staged = _cache.stage_path(tmp_path, 'k')
        (staged / 'manifest.json').write_text('{}', encoding='utf-8')
        # No mark_complete call.
        with pytest.raises(RuntimeError, match='missing marker'):
            _cache.publish(staged, tmp_path / 'k')

    def test_discard_staged_is_idempotent(self, tmp_path: Path) -> None:
        staged = _cache.stage_path(tmp_path, 'k')
        (staged / 'a.txt').write_text('x')
        _cache.discard_staged(staged)
        assert not staged.exists()
        # No raise on missing.
        _cache.discard_staged(staged)

    def test_failure_during_populate_preserves_existing_cache(self, tmp_path: Path) -> None:
        # Existing valid cache entry.
        final = tmp_path / 'k'
        final.mkdir()
        (final / 'manifest.json').write_text('{"existing": true}', encoding='utf-8')
        _cache.mark_complete(final)
        # Simulate a populate that fails before mark_complete (e.g. an
        # export error). The runner calls discard_staged on failure;
        # the old final entry must remain intact.
        staged = _cache.stage_path(tmp_path, 'k')
        (staged / 'manifest.json').write_text('{"new": "partial"}', encoding='utf-8')
        # NOTE: no mark_complete — the populate "failed" mid-flight.
        _cache.discard_staged(staged)
        # Existing entry untouched.
        assert (final / _cache.CACHE_COMPLETE_MARKER).is_file()
        assert json.loads((final / 'manifest.json').read_text()) == {'existing': True}

    def test_serial_publishes_to_same_slot_leave_no_dirs(self, tmp_path: Path) -> None:
        # Five sequential populates of the same slot. Each publish wins
        # because the lock is released between calls; the last one's
        # content remains. No leaked staging or .old- dirs.
        staged_paths = [_cache.stage_path(tmp_path, 'k') for _ in range(5)]
        assert len({p.name for p in staged_paths}) == 5
        final = tmp_path / 'k'
        for i, sp in enumerate(staged_paths):
            (sp / 'manifest.json').write_text(f'{{"i": {i}}}', encoding='utf-8')
            _cache.mark_complete(sp)
            _cache.publish(sp, final)
        assert json.loads((final / 'manifest.json').read_text()) == {'i': 4}
        leftovers = [
            p
            for p in tmp_path.iterdir()
            if p.name.startswith('.tmp-') or p.name.startswith('.old-')
        ]
        assert leftovers == []

    def test_publish_recovery_restores_old_when_second_rename_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Existing valid cache.
        final = tmp_path / 'k'
        final.mkdir()
        (final / 'manifest.json').write_text('{"existing": true}', encoding='utf-8')
        _cache.mark_complete(final)
        # Stage new content.
        staged = _cache.stage_path(tmp_path, 'k')
        (staged / 'manifest.json').write_text('{"new": true}', encoding='utf-8')
        _cache.mark_complete(staged)
        # Inject a failure on the SECOND os.rename call (the
        # staged → final rename). The first (final → old) and third
        # (old → final, recovery) must succeed.
        real_rename = os.rename
        calls = {'n': 0}

        def flaky_rename(src, dst):
            calls['n'] += 1
            if calls['n'] == 2:
                raise OSError(13, 'simulated rename failure')
            return real_rename(src, dst)

        monkeypatch.setattr('memex_eval.suite.snapshot_cache.os.rename', flaky_rename)
        with pytest.raises(OSError, match='simulated'):
            _cache.publish(staged, final)
        # Original entry restored — content unchanged.
        assert json.loads((final / 'manifest.json').read_text()) == {'existing': True}
        assert (final / _cache.CACHE_COMPLETE_MARKER).is_file()

    def test_publish_concurrent_processes_serialize(self, tmp_path: Path) -> None:
        # Real concurrency test: spawn two threads each running a full
        # stage→mark→publish cycle against the SAME slot. The fcntl
        # lock serializes; last-writer-wins. Both threads return without
        # raising; final state has exactly one valid entry; no leaked
        # .tmp-/.old- dirs.
        import threading

        final = tmp_path / 'k'
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []

        def worker(tag: str) -> None:
            try:
                sp = _cache.stage_path(tmp_path, 'k')
                (sp / 'manifest.json').write_text(f'{{"tag": "{tag}"}}', encoding='utf-8')
                _cache.mark_complete(sp)
                barrier.wait()  # both call publish at ~the same time
                _cache.publish(sp, final)
            except BaseException as e:  # pragma: no cover - debugging aid
                errors.append(e)

        t1 = threading.Thread(target=worker, args=('A',))
        t2 = threading.Thread(target=worker, args=('B',))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        assert errors == []
        # Final slot is a valid cache entry written by whichever thread won.
        assert (final / 'manifest.json').is_file()
        assert (final / _cache.CACHE_COMPLETE_MARKER).is_file()
        winning_tag = json.loads((final / 'manifest.json').read_text())['tag']
        assert winning_tag in ('A', 'B')
        # No leftover staging or .old- dirs.
        leftovers = [
            p
            for p in tmp_path.iterdir()
            if p.name.startswith('.tmp-') or p.name.startswith('.old-')
        ]
        assert leftovers == []


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
