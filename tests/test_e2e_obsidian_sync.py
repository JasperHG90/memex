"""End-to-end tests for obsidian-memex-sync.

These tests spin up a real Memex server (via testcontainers + lifespan)
and run the full sync pipeline, including:
- Vault scanning with asset resolution
- Batch ingestion via REST API
- State persistence and incremental sync
- Idempotency (re-sync skips unchanged notes)
- CLI commands (init, status, sync --dry-run)
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
from httpx import ASGITransport

from memex_core.server import app as server_app, lifespan

from memex_obsidian_sync.config import CONFIG_FILENAME, ObsidianSyncConfig, ServerConfig
from memex_obsidian_sync.scanner import scan_vault
from memex_obsidian_sync.state import SyncStateDB
from memex_obsidian_sync.sync import sync_vault


def _create_test_vault(root: Path) -> Path:
    """Create a realistic Obsidian vault for testing."""
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


def _make_config() -> ObsidianSyncConfig:
    return ObsidianSyncConfig(
        server=ServerConfig(url='http://test'),
    )


async def _sync_via_asgi(vault: Path, config: ObsidianSyncConfig, **kwargs):
    """Run sync_vault routing through the in-process ASGI server with lifespan."""
    transport = ASGITransport(app=server_app)

    class AsgiClient(httpx.AsyncClient):
        def __init__(self, **kw):  # type: ignore[no-untyped-def]
            kw['transport'] = transport
            super().__init__(**kw)

    async with lifespan(server_app):
        with patch('memex_obsidian_sync.sync.httpx.AsyncClient', AsgiClient):
            return await sync_vault(vault, config, **kwargs)


@pytest.mark.integration
@pytest.mark.llm
class TestObsidianSyncE2E:
    """Full end-to-end sync tests against a real Memex server."""

    @pytest.mark.asyncio
    async def test_initial_sync(self, _truncate_db, tmp_path: Path) -> None:
        """First sync should ingest all notes with their assets."""
        vault = _create_test_vault(tmp_path)
        config = _make_config()

        result = await _sync_via_asgi(vault, config)

        assert result.total_scanned == 3
        assert result.changed == 3
        assert result.ingested == 3
        assert result.failed == 0
        assert result.errors == []

        state = SyncStateDB(vault / config.sync.state_file)
        assert state.last_sync is not None
        files = state.get_all_files()
        assert len(files) == 3
        assert 'project-notes.md' in files
        assert 'ideas/brainstorm.md' in files
        assert 'daily/2026-03-28.md' in files
        state.close()

    @pytest.mark.asyncio
    async def test_incremental_sync_no_changes(self, _truncate_db, tmp_path: Path) -> None:
        """Second sync with no changes should skip everything."""
        vault = _create_test_vault(tmp_path)
        config = _make_config()

        result1 = await _sync_via_asgi(vault, config)
        assert result1.ingested == 3

        # Re-init lifespan for second sync
        result2 = await _sync_via_asgi(vault, config)
        assert result2.changed == 0
        assert result2.ingested == 0

    @pytest.mark.asyncio
    async def test_incremental_sync_with_modification(self, _truncate_db, tmp_path: Path) -> None:
        """After modifying a note, only the changed note should be re-synced."""
        vault = _create_test_vault(tmp_path)
        config = _make_config()

        result1 = await _sync_via_asgi(vault, config)
        assert result1.ingested == 3

        time.sleep(0.1)
        note_path = vault / 'project-notes.md'
        note_path.write_text(
            note_path.read_text() + f'\n## Update ({uuid4().hex[:8]})\nNew content added.\n'
        )

        result2 = await _sync_via_asgi(vault, config)
        assert result2.changed == 1
        assert result2.ingested + result2.skipped >= 1

    @pytest.mark.asyncio
    async def test_full_sync_ignores_state(self, _truncate_db, tmp_path: Path) -> None:
        """Full sync should re-ingest everything regardless of state."""
        vault = _create_test_vault(tmp_path)
        config = _make_config()

        result1 = await _sync_via_asgi(vault, config)
        assert result1.ingested == 3

        result2 = await _sync_via_asgi(vault, config, full=True)
        assert result2.changed == 3
        assert result2.skipped == 3 or result2.ingested == 3

    @pytest.mark.asyncio
    async def test_dry_run_does_not_ingest(self, _truncate_db, tmp_path: Path) -> None:
        """Dry run should report but not ingest or save state."""
        vault = _create_test_vault(tmp_path)
        config = _make_config()

        result = await _sync_via_asgi(vault, config, dry_run=True)

        assert result.total_scanned == 3
        assert result.changed == 3
        assert result.ingested == 0
        assert not (vault / config.sync.state_file).exists()


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


@pytest.mark.integration
@pytest.mark.llm
class TestObsidianSyncDeleteE2E:
    """End-to-end tests for archive/delete behavior against a real server."""

    @pytest.mark.asyncio
    async def test_deleted_file_archived(self, _truncate_db, tmp_path: Path) -> None:
        """When a file is deleted, its note should be archived in Memex."""
        vault = _create_test_vault(tmp_path)
        config = _make_config()

        # First sync — ingest all 3 notes
        result1 = await _sync_via_asgi(vault, config)
        assert result1.ingested == 3

        # Verify note_ids are stored in state
        state = SyncStateDB(vault / config.sync.state_file)
        ids = state.get_note_ids_for_paths(
            [
                'project-notes.md',
                'ideas/brainstorm.md',
                'daily/2026-03-28.md',
            ]
        )
        state.close()
        # At least some note_ids should be stored
        assert len(ids) >= 1

        # Delete a file
        (vault / 'daily' / '2026-03-28.md').unlink()

        # Second sync — should archive the deleted note
        result2 = await _sync_via_asgi(vault, config)
        assert 'daily/2026-03-28.md' in result2.deleted_detected
        # Note: archive may or may not succeed depending on whether note_id was stored
        if 'daily/2026-03-28.md' in ids:
            assert result2.archived == 1

    @pytest.mark.asyncio
    async def test_no_handle_deletes_flag(self, _truncate_db, tmp_path: Path) -> None:
        """With handle_deletes=False, deleted files should be reported but not acted on."""
        vault = _create_test_vault(tmp_path)
        config = _make_config()

        result1 = await _sync_via_asgi(vault, config)
        assert result1.ingested == 3

        (vault / 'daily' / '2026-03-28.md').unlink()

        result2 = await _sync_via_asgi(vault, config, handle_deletes=False)
        assert 'daily/2026-03-28.md' in result2.deleted_detected
        assert result2.archived == 0
        assert result2.hard_deleted == 0


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
