"""End-to-end tests for obsidian-memex-sync.

Uses the shared `client` fixture from conftest (TestClient with proper
lifespan management) for server tests. Non-server tests run standalone.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from memex_obsidian_sync.config import CONFIG_FILENAME, ObsidianSyncConfig
from memex_obsidian_sync.scanner import scan_vault
from memex_obsidian_sync.state import SyncStateDB
from memex_obsidian_sync.sync import _build_note_dto


def _create_test_vault(root: Path) -> Path:
    """Create a realistic folder of markdown notes for testing."""
    vault = root / 'test-vault'
    vault.mkdir()

    (vault / '.obsidian').mkdir()
    (vault / '.obsidian' / 'app.json').write_text('{}')

    (vault / 'attachments').mkdir()
    (vault / 'attachments' / 'diagram.png').write_bytes(b'\x89PNG\r\n\x1a\n' + b'\x00' * 200)

    uid = uuid4().hex[:8]
    (vault / 'project-notes.md').write_text(
        f'---\ntitle: Project Notes\ntags: [project, planning]\n---\n\n'
        f'# Project Notes ({uid})\n\n'
        f'## Goals\n- Build obsidian sync\n- Test it end-to-end\n\n'
        f'![[attachments/diagram.png]]\n'
    )
    (vault / 'ideas').mkdir()
    (vault / 'ideas' / 'brainstorm.md').write_text(
        f'# Brainstorm ({uid})\n\n'
        f'Some ideas for the project.\n\n'
        f'![diagram](../attachments/diagram.png)\n'
    )
    (vault / 'daily').mkdir()
    (vault / 'daily' / '2026-03-28.md').write_text(
        f'# 2026-03-28 ({uid})\n\n- Worked on obsidian sync tests\n- All tests passing\n'
    )

    return vault


def _ingest_vault_via_client(client: TestClient, vault: Path) -> dict:
    """Ingest a vault's notes via the TestClient batch API (same as test_e2e_batch).

    Returns dict with ingested note_ids and counts.
    """
    vault = vault.resolve()
    vault_name = vault.name
    cfg = ObsidianSyncConfig()
    notes = scan_vault(vault, cfg.sync.exclude, cfg.sync.assets)

    dtos = []
    for note in notes:
        dto = _build_note_dto(note, vault_name, None)
        dtos.append(dto.model_dump(mode='json'))

    payload = {'notes': dtos, 'batch_size': 32}
    response = client.post('/api/v1/ingestions/batch', json=payload)
    assert response.status_code == 202
    job_id = response.json()['job_id']

    # Poll for completion
    for _ in range(30):
        status_resp = client.get(f'/api/v1/ingestions/{job_id}')
        assert status_resp.status_code == 200
        data = status_resp.json()
        if data['status'] in ('completed', 'failed'):
            return data
        time.sleep(0.5)

    raise TimeoutError(f'Batch job {job_id} did not complete in time')


@pytest.mark.integration
@pytest.mark.llm
class TestObsidianSyncE2E:
    """Full E2E tests using the shared TestClient fixture."""

    def test_batch_ingest_from_vault(self, client: TestClient, tmp_path: Path) -> None:
        """Ingest all vault notes via batch API and verify completion."""
        vault = _create_test_vault(tmp_path)
        result = _ingest_vault_via_client(client, vault)

        assert result['status'] == 'completed'
        assert result['result']['processed_count'] == 3
        assert result['result']['failed_count'] == 0
        assert len(result['result']['note_ids']) == 3

    def test_idempotent_reingest(self, client: TestClient, tmp_path: Path) -> None:
        """Re-ingesting the same vault should skip all notes."""
        vault = _create_test_vault(tmp_path)

        result1 = _ingest_vault_via_client(client, vault)
        assert result1['result']['processed_count'] == 3

        result2 = _ingest_vault_via_client(client, vault)
        assert result2['result']['skipped_count'] == 3
        assert result2['result']['processed_count'] == 0

    def test_assets_included_in_ingestion(self, client: TestClient, tmp_path: Path) -> None:
        """Notes with asset references should include base64-encoded files."""
        vault = _create_test_vault(tmp_path)
        notes = scan_vault(
            vault.resolve(), ObsidianSyncConfig().sync.exclude, ObsidianSyncConfig().sync.assets
        )

        project_note = next(n for n in notes if n.relative_path == 'project-notes.md')
        assert len(project_note.assets) == 1

        dto = _build_note_dto(project_note, 'test', None)
        assert 'attachments/diagram.png' in dto.files
        decoded = base64.b64decode(dto.files['attachments/diagram.png'])
        assert decoded.startswith(b'\x89PNG')

    def test_archive_note_status(self, client: TestClient, tmp_path: Path) -> None:
        """Archived notes should have their memory units marked stale."""
        vault = _create_test_vault(tmp_path)
        result = _ingest_vault_via_client(client, vault)
        note_id = result['result']['note_ids'][0]

        # Archive the note
        resp = client.patch(
            f'/api/v1/notes/{note_id}/status',
            json={'status': 'archived'},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data['status'] == 'archived'
        assert data['note_id'] == note_id

    def test_dto_building(self, tmp_path: Path) -> None:
        """DTO builder should encode content and assets correctly."""
        vault = _create_test_vault(tmp_path)
        notes = scan_vault(
            vault.resolve(), ObsidianSyncConfig().sync.exclude, ObsidianSyncConfig().sync.assets
        )
        for note in notes:
            dto = _build_note_dto(note, 'test', 'my-vault', note_key_prefix='notes', tags=['test'])
            assert dto.note_key.startswith('notes:test:')
            assert dto.tags == ['test']
            decoded = base64.b64decode(dto.content)
            assert len(decoded) > 0


@pytest.mark.integration
class TestObsidianSyncE2ENoLLM:
    """Tests that don't require an LLM."""

    def test_assets_uploaded_with_notes(self, tmp_path: Path) -> None:
        vault = _create_test_vault(tmp_path)
        notes = scan_vault(
            vault, ObsidianSyncConfig().sync.exclude, ObsidianSyncConfig().sync.assets
        )

        project_note = next(n for n in notes if n.relative_path == 'project-notes.md')
        assert len(project_note.assets) == 1
        assert project_note.assets[0].relative_path == 'attachments/diagram.png'

        brainstorm = next(n for n in notes if n.relative_path == 'ideas/brainstorm.md')
        assert len(brainstorm.assets) == 1

    def test_excludes_obsidian_internals(self, tmp_path: Path) -> None:
        vault = _create_test_vault(tmp_path)
        notes = scan_vault(
            vault, ObsidianSyncConfig().sync.exclude, ObsidianSyncConfig().sync.assets
        )

        rel_paths = {n.relative_path for n in notes}
        assert not any('.obsidian' in p for p in rel_paths)

    def test_state_db_tracks_files(self, tmp_path: Path) -> None:
        """SQLite state DB should persist and retrieve file tracking data."""
        from memex_obsidian_sync.scanner import VaultNote

        db_path = tmp_path / '.memex-sync.db'
        state = SyncStateDB(db_path)
        try:
            note = VaultNote(
                path=tmp_path / 'a.md',
                relative_path='a.md',
                mtime=1000.0,
                size=100,
                assets=[],
            )
            state.mark_synced([note], vault_id='v1', note_ids={'a.md': 'note-123'})
            assert state.file_count() == 1
            assert state.get_note_ids_for_paths(['a.md']) == {'a.md': 'note-123'}
            assert state.last_sync is not None
        finally:
            state.close()

        # Re-open and verify persistence
        state2 = SyncStateDB(db_path)
        try:
            assert state2.file_count() == 1
            assert state2.vault_id == 'v1'
        finally:
            state2.close()


@pytest.mark.integration
class TestObsidianSyncCLI:
    """Test CLI commands."""

    def test_init_creates_config(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from memex_obsidian_sync.cli import app

        vault = tmp_path / 'vault'
        vault.mkdir()

        runner = CliRunner()
        result = runner.invoke(app, ['init', str(vault)])
        assert result.exit_code == 0
        assert (vault / CONFIG_FILENAME).exists()

        from memex_obsidian_sync.config import load_config

        cfg = load_config(vault)
        assert cfg.server.url == 'http://localhost:8321'

    def test_init_refuses_if_exists(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from memex_obsidian_sync.cli import app

        vault = tmp_path / 'vault'
        vault.mkdir()
        (vault / CONFIG_FILENAME).write_text('[server]\nurl = "custom"')

        runner = CliRunner()
        result = runner.invoke(app, ['init', str(vault)])
        assert result.exit_code == 1

    def test_status_shows_pending_changes(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from memex_obsidian_sync.cli import app

        vault = _create_test_vault(tmp_path)

        runner = CliRunner()
        result = runner.invoke(app, ['status', str(vault)])
        assert result.exit_code == 0
        assert 'never' in result.output
        assert '3' in result.output

    def test_sync_dry_run_cli(self, tmp_path: Path) -> None:
        from typer.testing import CliRunner

        from memex_obsidian_sync.cli import app

        vault = _create_test_vault(tmp_path)

        runner = CliRunner()
        result = runner.invoke(app, ['sync', str(vault), '--dry-run'])
        assert result.exit_code == 0
        assert 'would be synced' in result.output
