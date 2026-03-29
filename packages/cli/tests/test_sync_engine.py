from __future__ import annotations

import asyncio
import base64
import time
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from memex_common.schemas import BatchIngestResponse, BatchJobStatus, IngestResponse

from memex_cli.sync.config import SyncConfig
from memex_cli.sync.state import SyncStateDB
from memex_cli.sync.engine import _build_note_dto, sync_vault


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    """Create a vault with notes and an asset."""
    (tmp_path / 'hello.md').write_text('# Hello\nWorld')
    (tmp_path / 'sub').mkdir()
    (tmp_path / 'sub' / 'deep.md').write_text('# Deep note\n![[photo.png]]')
    (tmp_path / 'photo.png').write_bytes(b'\x89PNG' + b'\x00' * 50)
    return tmp_path


@pytest.fixture
def sync_config() -> SyncConfig:
    return SyncConfig()


@pytest.fixture
def mock_api() -> AsyncMock:
    return AsyncMock()


class TestBuildNoteDto:
    def test_basic_note(self, vault: Path) -> None:
        from memex_cli.sync.scanner import VaultNote

        note = VaultNote(
            path=vault / 'hello.md',
            relative_path='hello.md',
            mtime=1000.0,
            size=13,
            assets=[],
        )
        dto = _build_note_dto(note, 'my-vault', 'test-vault', tags=['obsidian'])

        assert dto.name == 'hello'
        assert dto.note_key == 'obsidian:my-vault:hello.md'
        assert dto.vault_id == 'test-vault'
        assert dto.tags == ['obsidian']

    def test_custom_prefix_and_tags(self, vault: Path) -> None:
        from memex_cli.sync.scanner import VaultNote

        note = VaultNote(
            path=vault / 'hello.md',
            relative_path='hello.md',
            mtime=1000.0,
            size=13,
            assets=[],
        )
        dto = _build_note_dto(
            note,
            'my-folder',
            None,
            note_key_prefix='notes',
            tags=['markdown', 'personal'],
        )

        assert dto.note_key == 'notes:my-folder:hello.md'
        assert dto.tags == ['markdown', 'personal']
        decoded = base64.b64decode(dto.content)
        assert b'# Hello' in decoded

    def test_note_with_assets(self, vault: Path) -> None:
        from memex_cli.sync.scanner import VaultAsset, VaultNote

        asset = VaultAsset(
            path=vault / 'photo.png',
            relative_path='photo.png',
            size=54,
        )
        note = VaultNote(
            path=vault / 'sub' / 'deep.md',
            relative_path='sub/deep.md',
            mtime=1000.0,
            size=30,
            assets=[asset],
        )
        dto = _build_note_dto(note, 'my-vault', None)

        assert 'photo.png' in dto.files
        decoded_asset = base64.b64decode(dto.files['photo.png'])
        assert decoded_asset.startswith(b'\x89PNG')


class TestSyncVault:
    def test_dry_run_does_not_ingest(
        self, vault: Path, mock_api: AsyncMock, sync_config: SyncConfig
    ) -> None:
        result = asyncio.run(
            sync_vault(vault, mock_api, sync_config, vault_id='test-vault', dry_run=True)
        )

        assert result.total_scanned >= 2
        assert result.changed >= 2
        assert result.ingested == 0

    def test_no_changes_after_sync(
        self, vault: Path, mock_api: AsyncMock, sync_config: SyncConfig
    ) -> None:
        """After a successful sync, a second sync should find no changes."""
        mock_batch = BatchJobStatus(
            job_id=uuid4(),
            status='completed',
            progress=None,
            result=BatchIngestResponse(
                processed_count=2,
                skipped_count=0,
                failed_count=0,
                note_ids=[],
                errors=[],
            ),
        )

        mock_api.ingest_batch.return_value = mock_batch
        mock_api.get_job_status.return_value = mock_batch

        result1 = asyncio.run(sync_vault(vault, mock_api, sync_config, vault_id='test-vault'))
        assert result1.ingested == 2

        result2 = asyncio.run(sync_vault(vault, mock_api, sync_config, vault_id='test-vault'))
        assert result2.changed == 0

    def test_single_note_uses_direct_ingest(
        self, tmp_path: Path, mock_api: AsyncMock, sync_config: SyncConfig
    ) -> None:
        """When only one note changed, use direct ingest instead of batch."""
        (tmp_path / 'only.md').write_text('# Only note')

        mock_response = IngestResponse(
            status='success',
            note_id=str(uuid4()),
            unit_ids=[],
            reason=None,
            overlapping_notes=[],
        )

        mock_api.ingest.return_value = mock_response

        result = asyncio.run(sync_vault(tmp_path, mock_api, sync_config, vault_id='test-vault'))

        mock_api.ingest.assert_called_once()
        assert result.ingested == 1

    def test_full_ignores_state(
        self, vault: Path, mock_api: AsyncMock, sync_config: SyncConfig
    ) -> None:
        """Full sync should sync everything even if state says all synced."""
        from memex_cli.sync.scanner import VaultNote

        # Pre-populate state DB with all notes "already synced" (future mtime)
        db_path = vault / sync_config.state_file
        state = SyncStateDB(db_path)
        future_mtime = time.time() + 9999
        state.mark_synced(
            [
                VaultNote(
                    path=vault / 'hello.md',
                    relative_path='hello.md',
                    mtime=future_mtime,
                    size=100,
                    assets=[],
                ),
                VaultNote(
                    path=vault / 'sub' / 'deep.md',
                    relative_path='sub/deep.md',
                    mtime=future_mtime,
                    size=100,
                    assets=[],
                ),
            ]
        )
        state.close()

        result = asyncio.run(
            sync_vault(vault, mock_api, sync_config, vault_id='test-vault', full=True, dry_run=True)
        )
        assert result.changed >= 2

    def test_single_note_stores_note_id(
        self, tmp_path: Path, mock_api: AsyncMock, sync_config: SyncConfig
    ) -> None:
        """After single-note ingestion, note_id should be stored in state."""
        (tmp_path / 'only.md').write_text('# Only note')
        note_id = str(uuid4())

        mock_response = IngestResponse(
            status='success',
            note_id=note_id,
            unit_ids=[],
            reason=None,
            overlapping_notes=[],
        )

        mock_api.ingest.return_value = mock_response

        asyncio.run(sync_vault(tmp_path, mock_api, sync_config, vault_id='test-vault'))

        # Check state has the note_id
        state = SyncStateDB(tmp_path / sync_config.state_file)
        ids = state.get_note_ids_for_paths(['only.md'])
        assert ids.get('only.md') == note_id
        state.close()


class TestDeleteHandling:
    def test_archive_on_delete_default(
        self, vault: Path, mock_api: AsyncMock, sync_config: SyncConfig
    ) -> None:
        """Deleted files should be archived (not hard-deleted) by default."""
        from memex_cli.sync.scanner import VaultNote

        # Pre-populate state with a file that no longer exists on disk
        state = SyncStateDB(vault / sync_config.state_file)
        state.mark_synced(
            [
                VaultNote(
                    path=vault / 'gone.md',
                    relative_path='gone.md',
                    mtime=1000.0,
                    size=100,
                    assets=[],
                )
            ],
            note_ids={'gone.md': str(uuid4())},
        )
        state.close()

        mock_api.set_note_status.return_value = {'status': 'archived'}
        mock_batch = BatchJobStatus(
            job_id=uuid4(),
            status='completed',
            progress=None,
            result=BatchIngestResponse(
                processed_count=2,
                skipped_count=0,
                failed_count=0,
                note_ids=[],
                errors=[],
            ),
        )
        mock_api.ingest_batch.return_value = mock_batch
        mock_api.get_job_status.return_value = mock_batch

        result = asyncio.run(
            sync_vault(vault, mock_api, sync_config, vault_id='test-vault', handle_deletes=True)
        )

        assert result.archived == 1
        assert result.hard_deleted == 0
        mock_api.set_note_status.assert_called_once()
        call_args = mock_api.set_note_status.call_args
        assert call_args[0][1] == 'archived'

    def test_hard_delete_flag(
        self, vault: Path, mock_api: AsyncMock, sync_config: SyncConfig
    ) -> None:
        """With hard_delete=True, deleted files should be permanently removed."""
        from memex_cli.sync.scanner import VaultNote

        note_id = str(uuid4())
        state = SyncStateDB(vault / sync_config.state_file)
        state.mark_synced(
            [
                VaultNote(
                    path=vault / 'gone.md',
                    relative_path='gone.md',
                    mtime=1000.0,
                    size=100,
                    assets=[],
                )
            ],
            note_ids={'gone.md': note_id},
        )
        state.close()

        mock_api.delete_note.return_value = True
        mock_batch = BatchJobStatus(
            job_id=uuid4(),
            status='completed',
            progress=None,
            result=BatchIngestResponse(
                processed_count=2,
                skipped_count=0,
                failed_count=0,
                note_ids=[],
                errors=[],
            ),
        )
        mock_api.ingest_batch.return_value = mock_batch
        mock_api.get_job_status.return_value = mock_batch

        result = asyncio.run(
            sync_vault(
                vault,
                mock_api,
                sync_config,
                vault_id='test-vault',
                handle_deletes=True,
                hard_delete=True,
            )
        )

        assert result.hard_deleted == 1
        assert result.archived == 0
        mock_api.delete_note.assert_called_once()

    def test_no_handle_deletes(
        self, vault: Path, mock_api: AsyncMock, sync_config: SyncConfig
    ) -> None:
        """With handle_deletes=False, deleted files should just be reported."""
        from memex_cli.sync.scanner import VaultNote

        state = SyncStateDB(vault / sync_config.state_file)
        state.mark_synced(
            [
                VaultNote(
                    path=vault / 'gone.md',
                    relative_path='gone.md',
                    mtime=1000.0,
                    size=100,
                    assets=[],
                )
            ],
            note_ids={'gone.md': str(uuid4())},
        )
        state.close()

        mock_batch = BatchJobStatus(
            job_id=uuid4(),
            status='completed',
            progress=None,
            result=BatchIngestResponse(
                processed_count=2,
                skipped_count=0,
                failed_count=0,
                note_ids=[],
                errors=[],
            ),
        )
        mock_api.ingest_batch.return_value = mock_batch
        mock_api.get_job_status.return_value = mock_batch

        result = asyncio.run(
            sync_vault(vault, mock_api, sync_config, vault_id='test-vault', handle_deletes=False)
        )

        assert result.archived == 0
        assert result.hard_deleted == 0
        assert 'gone.md' in result.deleted_detected
        # Should NOT call set_note_status or delete_note
        mock_api.set_note_status.assert_not_called()
        mock_api.delete_note.assert_not_called()

    def test_deleted_without_note_id_skipped(
        self, vault: Path, mock_api: AsyncMock, sync_config: SyncConfig
    ) -> None:
        """Files deleted without a stored note_id can't be archived in Memex."""
        from memex_cli.sync.scanner import VaultNote

        # No note_ids provided — simulating pre-existing state without tracking
        state = SyncStateDB(vault / sync_config.state_file)
        state.mark_synced(
            [
                VaultNote(
                    path=vault / 'gone.md',
                    relative_path='gone.md',
                    mtime=1000.0,
                    size=100,
                    assets=[],
                )
            ],
        )
        state.close()

        mock_batch = BatchJobStatus(
            job_id=uuid4(),
            status='completed',
            progress=None,
            result=BatchIngestResponse(
                processed_count=2,
                skipped_count=0,
                failed_count=0,
                note_ids=[],
                errors=[],
            ),
        )
        mock_api.ingest_batch.return_value = mock_batch
        mock_api.get_job_status.return_value = mock_batch

        result = asyncio.run(
            sync_vault(vault, mock_api, sync_config, vault_id='test-vault', handle_deletes=True)
        )

        # No archive or delete because there's no note_id to act on
        assert result.archived == 0
        assert result.hard_deleted == 0
        mock_api.set_note_status.assert_not_called()


class TestUnarchiveOnReturn:
    def test_unarchive_returning_note(
        self, vault: Path, mock_api: AsyncMock, sync_config: SyncConfig
    ) -> None:
        """When a previously-archived note reappears, it should be unarchived and re-ingested."""
        from memex_cli.sync.scanner import VaultNote

        note_id = str(uuid4())

        # Pre-populate state: note was synced then archived (simulates skip tag flow)
        state = SyncStateDB(vault / sync_config.state_file)
        state.mark_synced(
            [
                VaultNote(
                    path=vault / 'hello.md',
                    relative_path='hello.md',
                    mtime=500.0,
                    size=100,
                    assets=[],
                )
            ],
            note_ids={'hello.md': note_id},
        )
        state.archive_files(['hello.md'])
        state.close()

        mock_response = IngestResponse(
            status='success',
            note_id=note_id,
            unit_ids=[],
            reason=None,
            overlapping_notes=[],
        )

        mock_api.set_note_status.return_value = {'status': 'active'}
        mock_api.ingest.return_value = mock_response
        # Batch ingest for the other note (sub/deep.md)
        mock_batch = BatchJobStatus(
            job_id=uuid4(),
            status='completed',
            progress=None,
            result=BatchIngestResponse(
                processed_count=1,
                skipped_count=0,
                failed_count=0,
                note_ids=[],
                errors=[],
            ),
        )
        mock_api.ingest_batch.return_value = mock_batch
        mock_api.get_job_status.return_value = mock_batch

        result = asyncio.run(sync_vault(vault, mock_api, sync_config, vault_id='test-vault'))

        # The archived note should have been unarchived
        assert result.unarchived == 1
        # set_note_status called with 'active' for the returning note
        mock_api.set_note_status.assert_any_call(UUID(note_id), 'active')
        # The returning note was re-ingested (along with sub/deep.md as a new note)
        ingest_keys = [c.args[0].note_key for c in mock_api.ingest.call_args_list]
        assert any('hello.md' in k for k in ingest_keys)

        # State should show the note as unarchived
        state = SyncStateDB(vault / sync_config.state_file)
        assert state.get_archived_files() == {}
        assert 'hello.md' in state.get_all_files()
        state.close()

    def test_archive_preserves_state_for_unarchive(
        self, vault: Path, mock_api: AsyncMock, sync_config: SyncConfig
    ) -> None:
        """After archiving, state entry is preserved (not deleted) so unarchive works."""
        from memex_cli.sync.scanner import VaultNote

        note_id = str(uuid4())

        # Sync a note, then simulate it being "deleted" (skip tag added)
        state = SyncStateDB(vault / sync_config.state_file)
        state.mark_synced(
            [
                VaultNote(
                    path=vault / 'gone.md',
                    relative_path='gone.md',
                    mtime=1000.0,
                    size=100,
                    assets=[],
                )
            ],
            note_ids={'gone.md': note_id},
        )
        state.close()

        mock_api.set_note_status.return_value = {'status': 'archived'}
        mock_batch = BatchJobStatus(
            job_id=uuid4(),
            status='completed',
            progress=None,
            result=BatchIngestResponse(
                processed_count=2,
                skipped_count=0,
                failed_count=0,
                note_ids=[],
                errors=[],
            ),
        )
        mock_api.ingest_batch.return_value = mock_batch
        mock_api.get_job_status.return_value = mock_batch

        result = asyncio.run(
            sync_vault(vault, mock_api, sync_config, vault_id='test-vault', handle_deletes=True)
        )

        assert result.archived == 1

        # State entry should be preserved as archived (not deleted)
        state = SyncStateDB(vault / sync_config.state_file)
        archived = state.get_archived_files()
        assert archived.get('gone.md') == note_id
        state.close()


class TestSyncVaultConfig:
    def test_respects_exclude(
        self, vault: Path, mock_api: AsyncMock, sync_config: SyncConfig
    ) -> None:
        (vault / 'templates').mkdir()
        (vault / 'templates' / 'daily.md').write_text('# Template')

        sync_config.exclude.extends_exclude = ['templates']
        result = asyncio.run(
            sync_vault(vault, mock_api, sync_config, vault_id='test-vault', dry_run=True)
        )

        assert result.total_scanned >= 2
