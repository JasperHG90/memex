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

    def test_pdf_note_sets_filename(self, vault: Path) -> None:
        """AC-006: _build_note_dto for a .pdf file includes filename."""
        from memex_cli.sync.scanner import VaultNote

        pdf_path = vault / 'report.pdf'
        pdf_path.write_bytes(b'%PDF-1.4 fake pdf content')

        note = VaultNote(
            path=pdf_path,
            relative_path='report.pdf',
            mtime=1000.0,
            size=25,
            assets=[],
        )
        dto = _build_note_dto(note, 'my-vault', 'test-vault')
        assert dto.filename == 'report.pdf'

    def test_md_note_has_no_filename(self, vault: Path) -> None:
        """AC-006: _build_note_dto for a .md file has filename=None."""
        from memex_cli.sync.scanner import VaultNote

        note = VaultNote(
            path=vault / 'hello.md',
            relative_path='hello.md',
            mtime=1000.0,
            size=13,
            assets=[],
        )
        dto = _build_note_dto(note, 'my-vault', 'test-vault')
        assert dto.filename is None

    def test_honors_vault_override(self, vault: Path) -> None:
        """When override_vault is supplied, note_key embeds the target vault
        name and vault_id is the target UUID — not the active sync vault."""
        from memex_common.schemas import VaultDTO

        from memex_cli.sync.scanner import VaultNote

        override = VaultDTO(id=uuid4(), name='B-vault')
        note = VaultNote(
            path=vault / 'hello.md',
            relative_path='hello.md',
            mtime=1000.0,
            size=13,
            assets=[],
        )
        dto = _build_note_dto(
            note,
            'A-vault',
            'A-vault-id',
            override_vault=override,
        )
        assert dto.note_key == 'obsidian:B-vault:hello.md'
        assert dto.vault_id == str(override.id)


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


class TestFrontmatterVaultOverride:
    def test_warns_and_falls_back_when_vault_unknown(
        self, tmp_path: Path, mock_api: AsyncMock, sync_config: SyncConfig
    ) -> None:
        """Frontmatter cites a vault the server does not know.

        Expected behavior: list_vaults returns no match → warning logged,
        note falls back to the active vault (note_key uses vault_name, no
        per-file vault_id stored beyond the active default).
        """
        from memex_common.schemas import VaultDTO

        (tmp_path / 'note.md').write_text('---\nvault: nonexistent\n---\n\nbody')

        mock_api.list_vaults.return_value = [
            VaultDTO(id=uuid4(), name='A-vault'),
        ]
        mock_api.ingest.return_value = IngestResponse(
            status='success',
            note_id=str(uuid4()),
            unit_ids=[],
            reason=None,
            overlapping_notes=[],
        )

        result = asyncio.run(sync_vault(tmp_path, mock_api, sync_config, vault_id='A-vault-id'))

        assert result.ingested == 1
        assert result.migrated == 0
        called_dto = mock_api.ingest.call_args.args[0]
        # No override resolved → note_key uses active vault folder name
        assert called_dto.note_key == f'obsidian:{tmp_path.name}:note.md'

    def test_routes_to_override_vault(
        self, tmp_path: Path, mock_api: AsyncMock, sync_config: SyncConfig
    ) -> None:
        """Frontmatter cites a known vault → note_key embeds override name
        and DTO.vault_id is the override UUID."""
        from memex_common.schemas import VaultDTO

        (tmp_path / 'note.md').write_text('---\nvault: B-vault\n---\n\nbody')

        b_id = uuid4()
        mock_api.list_vaults.return_value = [
            VaultDTO(id=uuid4(), name='A-vault'),
            VaultDTO(id=b_id, name='B-vault'),
        ]
        mock_api.ingest.return_value = IngestResponse(
            status='success',
            note_id=str(uuid4()),
            unit_ids=[],
            reason=None,
            overlapping_notes=[],
        )

        result = asyncio.run(sync_vault(tmp_path, mock_api, sync_config, vault_id='A-vault-id'))

        assert result.ingested == 1
        called_dto = mock_api.ingest.call_args.args[0]
        assert called_dto.note_key == 'obsidian:B-vault:note.md'
        assert called_dto.vault_id == str(b_id)

    def test_migration_archives_prior_and_increments_counter(
        self, tmp_path: Path, mock_api: AsyncMock, sync_config: SyncConfig
    ) -> None:
        """Note already synced into A; user adds vault: B; re-sync archives
        the prior note in A, re-ingests into B, and migrated counter is 1."""
        from memex_common.schemas import VaultDTO

        from memex_cli.sync.scanner import VaultNote

        note_path = tmp_path / 'note.md'
        # First, "previously ingested in A" state — record vault_id explicitly.
        old_note_id = str(uuid4())
        state = SyncStateDB(tmp_path / sync_config.state_file)
        # Write file content matching a state mtime in the past.
        note_path.write_text('---\nvault: B-vault\n---\n\nbody')
        stat = note_path.stat()
        state.mark_synced(
            [
                VaultNote(
                    path=note_path,
                    relative_path='note.md',
                    mtime=stat.st_mtime - 100,  # older than the now-written file
                    size=stat.st_size,
                    assets=[],
                )
            ],
            vault_id='A-vault-id',
            note_ids={'note.md': old_note_id},
            note_keys={'note.md': 'obsidian:A-vault:note.md'},
            per_file_vault_ids={'note.md': 'A-vault-id'},
        )
        state.close()

        b_id = uuid4()
        mock_api.list_vaults.return_value = [
            VaultDTO(id=b_id, name='B-vault'),
        ]
        mock_api.set_note_status.return_value = None
        mock_api.ingest.return_value = IngestResponse(
            status='success',
            note_id=str(uuid4()),
            unit_ids=[],
            reason=None,
            overlapping_notes=[],
        )

        result = asyncio.run(sync_vault(tmp_path, mock_api, sync_config, vault_id='A-vault-id'))

        assert result.migrated == 1
        assert result.ingested == 1
        mock_api.set_note_status.assert_called_once()
        args, _ = mock_api.set_note_status.call_args
        assert str(args[0]) == old_note_id
        assert args[1] == 'archived'

        # State now reflects target vault.
        state2 = SyncStateDB(tmp_path / sync_config.state_file)
        row = state2.get_file('note.md')
        assert row is not None
        assert row.vault_id == str(b_id)
        assert row.note_key == 'obsidian:B-vault:note.md'
        state2.close()

    def test_legacy_row_with_override_to_active_vault_no_migration(
        self, tmp_path: Path, mock_api: AsyncMock, sync_config: SyncConfig
    ) -> None:
        """Legacy SyncedFile (vault_id=None, pre-override schema) re-synced
        with a frontmatter override that resolves to the *active* sync vault
        must NOT trigger a migration — the note is already in that vault."""
        from memex_common.schemas import VaultDTO

        from memex_cli.sync.scanner import VaultNote

        note_path = tmp_path / 'note.md'
        old_note_id = str(uuid4())
        active_vault_id = uuid4()
        note_path.write_text('---\nvault: Active-vault\n---\n\nbody')
        stat = note_path.stat()

        state = SyncStateDB(tmp_path / sync_config.state_file)
        state.mark_synced(
            [
                VaultNote(
                    path=note_path,
                    relative_path='note.md',
                    mtime=stat.st_mtime - 100,
                    size=stat.st_size,
                    assets=[],
                )
            ],
            vault_id=str(active_vault_id),
            note_ids={'note.md': old_note_id},
        )
        state.close()

        mock_api.list_vaults.return_value = [
            VaultDTO(id=active_vault_id, name='Active-vault'),
        ]
        mock_api.ingest.return_value = IngestResponse(
            status='success',
            note_id=str(uuid4()),
            unit_ids=[],
            reason=None,
            overlapping_notes=[],
        )

        result = asyncio.run(
            sync_vault(tmp_path, mock_api, sync_config, vault_id=str(active_vault_id))
        )

        assert result.migrated == 0
        mock_api.set_note_status.assert_not_called()


class TestFrontmatterVaultOverrideSafety:
    """Adversarial-review regressions: archive-after-success, failed-ingest
    state isolation, override-removed preserves stored routing."""

    def test_archive_skipped_when_new_ingest_fails(
        self, tmp_path: Path, mock_api: AsyncMock, sync_config: SyncConfig
    ) -> None:
        """If the new ingest fails after migration was detected, the old
        note must NOT be archived. Losing the user's only copy of the note
        would be catastrophic."""
        from memex_common.schemas import VaultDTO

        from memex_cli.sync.scanner import VaultNote

        note_path = tmp_path / 'note.md'
        old_note_id = str(uuid4())
        state = SyncStateDB(tmp_path / sync_config.state_file)
        note_path.write_text('---\nvault: B-vault\n---\n\nbody')
        stat = note_path.stat()
        state.mark_synced(
            [
                VaultNote(
                    path=note_path,
                    relative_path='note.md',
                    mtime=stat.st_mtime - 100,
                    size=stat.st_size,
                    assets=[],
                )
            ],
            vault_id='A-vault-id',
            note_ids={'note.md': old_note_id},
            note_keys={'note.md': 'obsidian:A-vault:note.md'},
            per_file_vault_ids={'note.md': 'A-vault-id'},
        )
        state.close()

        b_id = uuid4()
        mock_api.list_vaults.return_value = [VaultDTO(id=b_id, name='B-vault')]
        mock_api.ingest.return_value = IngestResponse(
            status='failed',
            note_id=None,
            unit_ids=[],
            reason='simulated failure',
            overlapping_notes=[],
        )

        result = asyncio.run(sync_vault(tmp_path, mock_api, sync_config, vault_id='A-vault-id'))

        assert result.failed == 1
        assert result.migrated == 0
        mock_api.set_note_status.assert_not_called()

    def test_state_unchanged_when_ingest_fails(
        self, tmp_path: Path, mock_api: AsyncMock, sync_config: SyncConfig
    ) -> None:
        """A failed ingest must not clobber an existing SyncedFile's
        vault_id/note_key — preserving alignment with the server."""
        from memex_common.schemas import VaultDTO

        from memex_cli.sync.scanner import VaultNote

        note_path = tmp_path / 'note.md'
        old_note_id = str(uuid4())
        state = SyncStateDB(tmp_path / sync_config.state_file)
        note_path.write_text('---\nvault: B-vault\n---\n\nbody')
        stat = note_path.stat()
        state.mark_synced(
            [
                VaultNote(
                    path=note_path,
                    relative_path='note.md',
                    mtime=stat.st_mtime - 100,
                    size=stat.st_size,
                    assets=[],
                )
            ],
            vault_id='A-vault-id',
            note_ids={'note.md': old_note_id},
            note_keys={'note.md': 'obsidian:A-vault:note.md'},
            per_file_vault_ids={'note.md': 'A-vault-id'},
        )
        state.close()

        b_id = uuid4()
        mock_api.list_vaults.return_value = [VaultDTO(id=b_id, name='B-vault')]
        mock_api.ingest.return_value = IngestResponse(
            status='failed',
            note_id=None,
            unit_ids=[],
            reason='simulated failure',
            overlapping_notes=[],
        )

        asyncio.run(sync_vault(tmp_path, mock_api, sync_config, vault_id='A-vault-id'))

        state2 = SyncStateDB(tmp_path / sync_config.state_file)
        row = state2.get_file('note.md')
        assert row is not None
        # Pre-existing routing preserved exactly.
        assert row.vault_id == 'A-vault-id'
        assert row.note_key == 'obsidian:A-vault:note.md'
        assert row.note_id == old_note_id
        state2.close()

    def test_batch_skip_does_not_corrupt_note_id_mapping(
        self, tmp_path: Path, mock_api: AsyncMock, sync_config: SyncConfig
    ) -> None:
        """Server returns note_ids aligned to *processed* notes only —
        skipped entries are excluded. When the batch contains skips, the
        CLI must NOT trust positional alignment with input DTOs (which
        previously caused state corruption: skipped note received a
        successor's note_id)."""
        from memex_cli.sync.scanner import VaultNote

        # 3 notes — middle one will be reported as skipped by the server.
        for name in ('a.md', 'b.md', 'c.md'):
            (tmp_path / name).write_text(f'# {name}')

        state = SyncStateDB(tmp_path / sync_config.state_file)
        # Pre-record an existing note_id for 'b.md' that must NOT be
        # overwritten by the corrupt positional mapping.
        existing_b_id = str(uuid4())
        state.mark_synced(
            [VaultNote(path=tmp_path / 'b.md', relative_path='b.md', mtime=0.0, size=1, assets=[])],
            note_ids={'b.md': existing_b_id},
        )
        state.close()

        new_a_id = str(uuid4())
        new_c_id = str(uuid4())
        mock_batch = BatchJobStatus(
            job_id=uuid4(),
            status='completed',
            progress=None,
            result=BatchIngestResponse(
                processed_count=2,
                skipped_count=1,
                failed_count=0,
                # Server returns 2 entries — for a.md and c.md (b.md was
                # skipped). Positional alignment with the 3-DTO input is
                # therefore broken.
                note_ids=[new_a_id, new_c_id],
                errors=[],
            ),
        )
        mock_api.ingest_batch.return_value = mock_batch
        mock_api.get_job_status.return_value = mock_batch

        asyncio.run(sync_vault(tmp_path, mock_api, sync_config, vault_id='active'))

        state2 = SyncStateDB(tmp_path / sync_config.state_file)
        # b.md's existing note_id must NOT be overwritten.
        b_row = state2.get_file('b.md')
        assert b_row is not None
        assert b_row.note_id == existing_b_id
        state2.close()

    def test_override_removed_preserves_prior_routing(
        self, tmp_path: Path, mock_api: AsyncMock, sync_config: SyncConfig
    ) -> None:
        """User adds vault: B → syncs. Then removes the frontmatter key →
        re-syncs. Code should keep ingesting into B (implicit target) using
        the same note_key — not silently overwrite to the active vault."""
        from memex_common.schemas import VaultDTO

        from memex_cli.sync.scanner import VaultNote

        note_path = tmp_path / 'note.md'
        b_id = uuid4()
        prior_note_id = str(uuid4())
        state = SyncStateDB(tmp_path / sync_config.state_file)
        note_path.write_text('# updated body without frontmatter\n')
        stat = note_path.stat()
        state.mark_synced(
            [
                VaultNote(
                    path=note_path,
                    relative_path='note.md',
                    mtime=stat.st_mtime - 100,
                    size=stat.st_size,
                    assets=[],
                )
            ],
            vault_id='A-vault-id',
            note_ids={'note.md': prior_note_id},
            note_keys={'note.md': 'obsidian:B-vault:note.md'},
            per_file_vault_ids={'note.md': str(b_id)},
        )
        state.close()

        # User has previously routed via override; vaults_by_id MUST be
        # consulted even when no current note has a frontmatter override.
        mock_api.list_vaults.return_value = [VaultDTO(id=b_id, name='B-vault')]
        mock_api.ingest.return_value = IngestResponse(
            status='success',
            note_id=prior_note_id,
            unit_ids=[],
            reason=None,
            overlapping_notes=[],
        )

        result = asyncio.run(sync_vault(tmp_path, mock_api, sync_config, vault_id='A-vault-id'))

        assert result.ingested == 1
        assert result.migrated == 0
        # DTO sent to server uses the implicit B-vault target — not the
        # active sync vault — so the existing server-side note is updated
        # idempotently rather than duplicated.
        called_dto = mock_api.ingest.call_args.args[0]
        assert called_dto.note_key == 'obsidian:B-vault:note.md'
        assert called_dto.vault_id == str(b_id)


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


class TestFrontmatterVaultOverrideReturning:
    """Returning-archived notes with a NEW vault override must migrate
    (old note stays archived in source vault, new note created in target),
    not duplicate (old reactivated AND new created in another vault)."""

    def test_returning_archived_with_override_migrates_not_duplicates(
        self, tmp_path: Path, mock_api: AsyncMock, sync_config: SyncConfig
    ) -> None:
        from memex_common.schemas import VaultDTO

        from memex_cli.sync.scanner import VaultNote

        note_path = tmp_path / 'note.md'
        # Note previously synced into A, then deleted from disk
        # → state has archived=True row, vault_id=A.
        old_note_id = str(uuid4())
        a_vault_id = str(uuid4())
        state = SyncStateDB(tmp_path / sync_config.state_file)
        state.mark_synced(
            [
                VaultNote(
                    path=note_path,
                    relative_path='note.md',
                    mtime=100.0,
                    size=10,
                    assets=[],
                )
            ],
            vault_id=a_vault_id,
            note_ids={'note.md': old_note_id},
            note_keys={'note.md': 'obsidian:A-vault:note.md'},
            per_file_vault_ids={'note.md': a_vault_id},
        )
        state.archive_files(['note.md'])
        state.close()

        # File reappears with vault: B frontmatter.
        note_path.write_text('---\nvault: B-vault\n---\n\nfresh body')

        b_id = uuid4()
        mock_api.list_vaults.return_value = [
            VaultDTO(id=UUID(a_vault_id), name='A-vault'),
            VaultDTO(id=b_id, name='B-vault'),
        ]
        new_note_id = str(uuid4())
        mock_api.ingest.return_value = IngestResponse(
            status='success',
            note_id=new_note_id,
            unit_ids=[],
            reason=None,
            overlapping_notes=[],
        )

        result = asyncio.run(sync_vault(tmp_path, mock_api, sync_config, vault_id='A-vault-id'))

        # No unarchive should fire — old note stays archived in A.
        # set_note_status with 'active' must NOT be called.
        for call in mock_api.set_note_status.call_args_list:
            args = call.args
            assert args[1] != 'active', (
                'returning-migration must NOT reactivate the old note in the source vault'
            )

        assert result.ingested == 1
        # migrated counter increments for the returning-migration case.
        assert result.migrated == 1

        # The new ingest used the override vault.
        called_dto = mock_api.ingest.call_args.args[0]
        assert called_dto.note_key == 'obsidian:B-vault:note.md'
        assert called_dto.vault_id == str(b_id)

        # State now points at the new note in vault B; old archived row
        # cleared because mark_synced flips archived=False.
        state2 = SyncStateDB(tmp_path / sync_config.state_file)
        row = state2.get_file('note.md')
        assert row is not None
        assert row.archived is False
        assert row.vault_id == str(b_id)
        assert row.note_id == new_note_id
        state2.close()


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


class TestPollJob:
    """Tests for _poll_job: stale detection, connection resilience, terminal states."""

    def test_polls_until_completed(self) -> None:
        """Should keep polling until status is 'completed'."""
        from memex_cli.sync.engine import _poll_job

        job_id = uuid4()
        api = AsyncMock()

        processing = BatchJobStatus(
            job_id=job_id,
            status='processing',
            progress='Processed 5/10 notes',
            processed_count=5,
            total_count=10,
        )
        completed = BatchJobStatus(
            job_id=job_id,
            status='completed',
            progress='Completed: 10/10 processed',
            processed_count=10,
            total_count=10,
            result=BatchIngestResponse(
                processed_count=10, skipped_count=0, failed_count=0, note_ids=[], errors=[]
            ),
        )
        api.get_job_status.side_effect = [processing, processing, completed]

        result = asyncio.run(_poll_job(api, job_id, poll_interval=0.01))
        assert result is not None
        assert result.status == 'completed'
        assert api.get_job_status.call_count == 3

    def test_polls_until_failed(self) -> None:
        """Should stop polling when status is 'failed'."""
        from memex_cli.sync.engine import _poll_job

        job_id = uuid4()
        api = AsyncMock()

        failed = BatchJobStatus(job_id=job_id, status='failed', progress='Failed')
        api.get_job_status.return_value = failed

        result = asyncio.run(_poll_job(api, job_id, poll_interval=0.01))
        assert result is not None
        assert result.status == 'failed'

    def test_connection_errors_retry_then_give_up(self) -> None:
        """After max consecutive errors, returns last known status."""
        from memex_cli.sync.engine import _poll_job

        job_id = uuid4()
        api = AsyncMock()

        good_status = BatchJobStatus(
            job_id=job_id,
            status='processing',
            progress='Processed 5/10 notes',
            processed_count=5,
            total_count=10,
        )
        # One good response, then 30 consecutive errors
        api.get_job_status.side_effect = [good_status] + [ConnectionError('server down')] * 30

        result = asyncio.run(_poll_job(api, job_id, poll_interval=0.01))
        # Should return the last known good status
        assert result is not None
        assert result.status == 'processing'
        assert result.processed_count == 5

    def test_connection_errors_recover(self) -> None:
        """Transient errors should be retried and recover."""
        from memex_cli.sync.engine import _poll_job

        job_id = uuid4()
        api = AsyncMock()

        processing = BatchJobStatus(
            job_id=job_id,
            status='processing',
            progress='Processed 5/10 notes',
            processed_count=5,
            total_count=10,
        )
        completed = BatchJobStatus(
            job_id=job_id,
            status='completed',
            progress='Completed: 10/10 processed',
            processed_count=10,
            total_count=10,
            result=BatchIngestResponse(
                processed_count=10, skipped_count=0, failed_count=0, note_ids=[], errors=[]
            ),
        )
        # Good, 3 errors, then completed
        api.get_job_status.side_effect = [
            processing,
            ConnectionError('blip'),
            ConnectionError('blip'),
            ConnectionError('blip'),
            completed,
        ]

        result = asyncio.run(_poll_job(api, job_id, poll_interval=0.01))
        assert result is not None
        assert result.status == 'completed'

    def test_no_response_returns_none(self) -> None:
        """If server is unreachable from the start, returns None."""
        from memex_cli.sync.engine import _poll_job

        job_id = uuid4()
        api = AsyncMock()
        api.get_job_status.side_effect = ConnectionError('unreachable')

        result = asyncio.run(_poll_job(api, job_id, poll_interval=0.01))
        assert result is None

    def test_progress_callback_called(self) -> None:
        """Progress callback should be invoked with parsed counts."""
        from memex_cli.sync.engine import _poll_job

        job_id = uuid4()
        api = AsyncMock()
        progress_calls: list[tuple] = []

        def on_progress(phase: str, current: int, total: int, detail: str) -> None:
            progress_calls.append((phase, current, total, detail))

        completed = BatchJobStatus(
            job_id=job_id,
            status='completed',
            progress='Completed: 10/10 processed',
            processed_count=10,
            total_count=10,
            result=BatchIngestResponse(
                processed_count=10, skipped_count=0, failed_count=0, note_ids=[], errors=[]
            ),
        )
        api.get_job_status.return_value = completed

        asyncio.run(_poll_job(api, job_id, poll_interval=0.01, on_progress=on_progress))
        assert len(progress_calls) == 1
        assert progress_calls[0] == ('ingesting', 10, 10, 'Completed: 10/10 processed')


class TestSyncVaultConnectionLoss:
    """Tests for sync_vault handling of non-terminal poll results."""

    def test_connection_loss_reports_error_with_job_id(
        self, vault: Path, mock_api: AsyncMock, sync_config: SyncConfig
    ) -> None:
        """When polling loses connection, result should contain job_id and error message."""
        job_id = uuid4()
        mock_api.ingest_batch.return_value = BatchJobStatus(job_id=job_id, status='pending')
        # Server goes down immediately
        mock_api.get_job_status.side_effect = ConnectionError('server down')

        result = asyncio.run(sync_vault(vault, mock_api, sync_config, vault_id='test-vault'))

        assert result.job_id == job_id
        assert len(result.errors) == 1
        assert str(job_id) in result.errors[0]
        assert 'unreachable' in result.errors[0].lower() or 'running' in result.errors[0].lower()

    def test_connection_loss_mid_processing_reports_job_id(
        self, vault: Path, mock_api: AsyncMock, sync_config: SyncConfig
    ) -> None:
        """When server dies mid-processing, result preserves the job_id for manual follow-up."""
        job_id = uuid4()
        processing = BatchJobStatus(
            job_id=job_id,
            status='processing',
            progress='Processed 1/2 notes',
            processed_count=1,
            total_count=2,
        )
        mock_api.ingest_batch.return_value = BatchJobStatus(job_id=job_id, status='pending')
        # One good poll, then 30 errors
        mock_api.get_job_status.side_effect = [processing] + [ConnectionError('gone')] * 30

        result = asyncio.run(sync_vault(vault, mock_api, sync_config, vault_id='test-vault'))

        assert result.job_id == job_id
        assert len(result.errors) == 1
        assert 'still be running' in result.errors[0].lower()


class TestProgressCallback:
    """Tests that on_progress 'done' fires correctly in all outcomes."""

    def test_done_fires_on_success(
        self, vault: Path, mock_api: AsyncMock, sync_config: SyncConfig
    ) -> None:
        progress_calls: list[tuple] = []

        def on_progress(phase: str, current: int, total: int, detail: str) -> None:
            progress_calls.append((phase, current, total, detail))

        mock_batch = BatchJobStatus(
            job_id=uuid4(),
            status='completed',
            result=BatchIngestResponse(
                processed_count=2, skipped_count=0, failed_count=0, note_ids=[], errors=[]
            ),
        )
        mock_api.ingest_batch.return_value = mock_batch
        mock_api.get_job_status.return_value = mock_batch

        asyncio.run(
            sync_vault(vault, mock_api, sync_config, vault_id='test-vault', on_progress=on_progress)
        )

        done_calls = [c for c in progress_calls if c[0] == 'done']
        assert len(done_calls) == 1
        assert 'ingested' in done_calls[0][3].lower()

    def test_done_fires_on_all_failed(
        self, vault: Path, mock_api: AsyncMock, sync_config: SyncConfig
    ) -> None:
        progress_calls: list[tuple] = []

        def on_progress(phase: str, current: int, total: int, detail: str) -> None:
            progress_calls.append((phase, current, total, detail))

        mock_batch = BatchJobStatus(
            job_id=uuid4(),
            status='completed',
            result=BatchIngestResponse(
                processed_count=0,
                skipped_count=0,
                failed_count=2,
                note_ids=[],
                errors=[{'chunk_start': 0, 'error': 'test error'}],
            ),
        )
        mock_api.ingest_batch.return_value = mock_batch
        mock_api.get_job_status.return_value = mock_batch

        asyncio.run(
            sync_vault(vault, mock_api, sync_config, vault_id='test-vault', on_progress=on_progress)
        )

        done_calls = [c for c in progress_calls if c[0] == 'done']
        assert len(done_calls) == 1
        assert 'failed' in done_calls[0][3].lower()

    def test_done_fires_on_connection_loss(
        self, vault: Path, mock_api: AsyncMock, sync_config: SyncConfig
    ) -> None:
        progress_calls: list[tuple] = []

        def on_progress(phase: str, current: int, total: int, detail: str) -> None:
            progress_calls.append((phase, current, total, detail))

        job_id = uuid4()
        mock_api.ingest_batch.return_value = BatchJobStatus(job_id=job_id, status='pending')
        mock_api.get_job_status.side_effect = ConnectionError('server down')

        asyncio.run(
            sync_vault(vault, mock_api, sync_config, vault_id='test-vault', on_progress=on_progress)
        )

        done_calls = [c for c in progress_calls if c[0] == 'done']
        assert len(done_calls) == 1
        assert str(job_id) in done_calls[0][3] or 'unreachable' in done_calls[0][3].lower()


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
