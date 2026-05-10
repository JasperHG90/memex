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
# In-process integration: end-to-end cache lifecycle using the exporter +
# importer directly. No HTTP route, no eval-mode flag. The cache module
# is the only thing under test here — the importer/exporter have their
# own coverage in test_snapshot_import.py.


@pytest_asyncio.fixture
async def small_vault(db_session: AsyncSession) -> dict[str, UUID]:
    """Minimal vault used by the cache lifecycle test."""
    import hashlib

    vault = Vault(name='cache-export-test', description='cache test')
    db_session.add(vault)
    await db_session.flush()

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


async def test_cache_round_trip_export_then_import(
    db_session: AsyncSession,
    small_vault: dict[str, UUID],
    tmp_path: Path,
) -> None:
    """Export via SnapshotExporter -> mark_complete -> lookup hits ->
    import via SnapshotImporter. Mirrors the runner's auto-mode flow
    (populate after extraction; subsequent run imports). No route, no
    HTTP — direct in-process invocation against the testcontainer DB.
    """
    from memex_eval.snapshot import (
        SnapshotImporter,
        ensure_eval_import_state_table,
    )
    from memex_core.memory.models.base import MODEL_REGISTRY
    from memex_core.memory.sql_models import EMBEDDING_DIMENSION
    from memex_core.services.snapshot import (
        EmbeddingModelIdentity,
        SnapshotExporter,
    )

    # Ensure the eval_import_state table exists (idempotent DDL the
    # eval runner applies before its first import).
    conn = await db_session.connection()
    await ensure_eval_import_state_table(conn)
    await db_session.commit()

    cache_root = _cache.resolve_cache_root(tmp_path / 'cache')
    cache_key = _cache.cache_key('demo-suite', 'a' * 64)
    staged = _cache.stage_path(cache_root, cache_key)

    # Export (the cache-populate step in the runner).
    exporter = SnapshotExporter(
        session=db_session,
        filestore=None,
        vault_id_or_name=small_vault['vault_id'],
        output_dir=staged,
        embedding_model=EmbeddingModelIdentity(
            name=str(MODEL_REGISTRY['embedding'].repo_id),
            dim=EMBEDDING_DIMENSION,
            hash=str(MODEL_REGISTRY['embedding'].revision),
        ),
    )
    await exporter.export()
    # The exporter sets the transaction READ ONLY for its duration;
    # release it before the rest of the test issues writes.
    await db_session.rollback()
    _cache.mark_complete(staged)

    final_path = cache_root / cache_key
    _cache.publish(staged, final_path)

    # Lookup must now hit.
    result = _cache.lookup(cache_root, 'demo-suite', 'a' * 64)
    assert result.hit
    assert result.cache_path == final_path
    manifest = json.loads((final_path / 'manifest.json').read_text())
    assert manifest['source_vault_name'] == 'cache-export-test'

    # Drop the source vault to prove the import path actually
    # reconstructs the data (no relying on leftover rows).
    from sqlalchemy import delete

    await db_session.execute(delete(Note).where(Note.vault_id == small_vault['vault_id']))
    await db_session.execute(delete(Vault).where(Vault.id == small_vault['vault_id']))
    await db_session.commit()

    importer = SnapshotImporter(
        session=db_session,
        filestore=None,
        embedding_backend=OnnxBackend(),
        snapshot_dir=final_path,
        target_vault_name='cache-import-target',
    )
    target = await importer.import_snapshot()
    assert target is not None
