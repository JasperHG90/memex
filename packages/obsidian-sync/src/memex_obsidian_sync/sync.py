from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

import httpx
import structlog
from pydantic import BaseModel, Field

from memex_common.client import RemoteMemexAPI
from memex_common.schemas import BatchJobStatus, NoteCreateDTO

from .config import ObsidianSyncConfig
from .scanner import VaultNote, scan_vault
from .state import SyncStateDB, diff

logger = structlog.get_logger()

# Callback signature: (phase, current, total, detail)
ProgressCallback = Callable[[str, int, int, str], None]


class SyncResult(BaseModel):
    """Result of a vault sync operation."""

    ingested: int = Field(default=0, description='Number of notes successfully ingested.')
    skipped: int = Field(default=0, description='Number of notes skipped (unchanged content).')
    failed: int = Field(default=0, description='Number of notes that failed to ingest.')
    archived: int = Field(
        default=0,
        description='Number of deleted notes archived in Memex (soft delete, units marked stale).',
    )
    hard_deleted: int = Field(
        default=0,
        description='Number of deleted notes permanently removed from Memex.',
    )
    errors: list[str] = Field(default_factory=list, description='Error messages for failed notes.')
    deleted_detected: list[str] = Field(
        default_factory=list,
        description='Relative paths of files deleted from folder since last sync.',
    )
    total_scanned: int = Field(default=0, description='Total number of .md files found.')
    changed: int = Field(default=0, description='Number of notes with changes to sync.')
    job_id: UUID | None = Field(
        default=None,
        description='Batch job ID when using background mode.',
    )


def _build_note_dto(
    note: VaultNote,
    vault_name: str,
    vault_id: str | None,
    note_key_prefix: str = 'obsidian',
    tags: list[str] | None = None,
) -> NoteCreateDTO:
    """Build a NoteCreateDTO from a VaultNote."""
    content_bytes = note.path.read_bytes()
    note_key = f'{note_key_prefix}:{vault_name}:{note.relative_path}'
    name = note.path.stem

    files_dict: dict[str, bytes] = {}
    for asset in note.assets:
        asset_bytes = asset.path.read_bytes()
        files_dict[asset.relative_path] = base64.b64encode(asset_bytes)

    return NoteCreateDTO(
        name=name,
        description='',
        content=base64.b64encode(content_bytes),
        files=files_dict,
        tags=tags or [],
        note_key=note_key,
        vault_id=vault_id,
    )


async def _poll_job(
    api: RemoteMemexAPI,
    job_id: UUID,
    poll_interval: float = 2.0,
    max_wait: float = 600.0,
    on_progress: ProgressCallback | None = None,
) -> BatchJobStatus | None:
    """Poll a batch job until completion or timeout."""
    elapsed = 0.0
    status = None
    while elapsed < max_wait:
        status = await api.get_job_status(job_id)
        if on_progress and status.progress:
            on_progress('ingesting', 0, 0, status.progress)
        if status.status in ('completed', 'failed'):
            return status
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
    return status


async def _handle_deletes(
    api: RemoteMemexAPI,
    state: SyncStateDB,
    deleted_paths: list[str],
    hard_delete: bool = False,
    on_progress: ProgressCallback | None = None,
) -> tuple[int, int, list[str]]:
    """Handle deleted files by archiving or hard-deleting their Memex notes.

    Returns:
        (archived_count, hard_deleted_count, errors)
    """
    path_to_note_id = state.get_note_ids_for_paths(deleted_paths)
    archived = 0
    hard_deleted_count = 0
    errors: list[str] = []

    paths_with_ids = [(p, nid) for p, nid in path_to_note_id.items()]
    paths_without_ids = [p for p in deleted_paths if p not in path_to_note_id]

    if paths_without_ids:
        logger.warning(
            'Deleted files without stored note_id (cannot archive/delete in Memex)',
            paths=paths_without_ids,
        )

    for i, (path, note_id) in enumerate(paths_with_ids):
        if on_progress:
            action = 'deleting' if hard_delete else 'archiving'
            on_progress(action, i, len(paths_with_ids), path)
        try:
            note_uuid = UUID(note_id)
            if hard_delete:
                await api.delete_note(note_uuid)
                hard_deleted_count += 1
                logger.info('Hard-deleted note', path=path, note_id=note_id)
            else:
                await api.set_note_status(note_uuid, 'archived')
                archived += 1
                logger.info('Archived note', path=path, note_id=note_id)
        except Exception as e:
            errors.append(f'{path}: {e}')
            logger.error('Failed to handle deleted note', path=path, error=str(e))

    # Remove successfully handled files from state
    handled = [p for p, _ in paths_with_ids if p not in {e.split(':')[0] for e in errors}]
    if handled:
        state.remove_files(handled)

    # Also remove files without note_ids from state (can't do anything with them)
    if paths_without_ids:
        state.remove_files(paths_without_ids)

    return archived, hard_deleted_count, errors


async def sync_vault(
    vault_path: Path,
    config: ObsidianSyncConfig,
    full: bool = False,
    dry_run: bool = False,
    background: bool = False,
    handle_deletes: bool = True,
    hard_delete: bool = False,
    notes_filter: list[str] | None = None,
    on_progress: ProgressCallback | None = None,
) -> SyncResult:
    """Sync a folder of Markdown notes to Memex.

    Args:
        vault_path: Path to the notes directory.
        config: Parsed configuration.
        full: If True, ignore last sync state and re-sync everything.
        dry_run: If True, report what would be synced without syncing.
        background: If True, submit the batch job and return immediately.
        handle_deletes: If True (default), archive deleted notes in Memex.
        hard_delete: If True, permanently delete instead of archiving.
            Only has effect when handle_deletes is True.
        notes_filter: If provided, only sync notes with these relative paths.
        on_progress: Optional callback for progress updates.
    """
    vault_path = vault_path.resolve()
    vault_name = vault_path.name
    result = SyncResult()

    # 1. Scan
    if on_progress:
        on_progress('scanning', 0, 0, 'Scanning for notes...')
    all_notes = scan_vault(vault_path, config.sync.exclude, config.sync.assets)
    result.total_scanned = len(all_notes)
    if on_progress:
        on_progress(
            'scanning',
            result.total_scanned,
            result.total_scanned,
            f'Found {result.total_scanned} notes',
        )

    # 2. Diff
    db_path = vault_path / config.sync.state_file
    state = SyncStateDB(db_path)
    try:
        if full:
            changed = list(all_notes)
            deleted: list[str] = []
        else:
            changed, deleted = diff(state, all_notes)
        result.deleted_detected = deleted

        if notes_filter is not None:
            filter_set = set(notes_filter)
            changed = [n for n in changed if n.relative_path in filter_set]

        result.changed = len(changed)

        if not changed and not deleted:
            return result

        if dry_run:
            return result

        # 3. Build DTOs for changed notes
        base_url = f'{config.server.url.rstrip("/")}/api/v1/'
        headers: dict[str, str] = {}
        if config.server.api_key:
            headers['X-API-Key'] = config.server.api_key.get_secret_value()

        async with httpx.AsyncClient(base_url=base_url, timeout=240.0, headers=headers) as client:
            api = RemoteMemexAPI(client)

            # 3a. Ingest changed notes
            if changed:
                if on_progress:
                    on_progress('preparing', 0, len(changed), 'Preparing notes...')
                dtos: list[NoteCreateDTO] = []
                for i, note in enumerate(changed):
                    try:
                        dto = _build_note_dto(
                            note,
                            vault_name,
                            config.server.vault_id,
                            note_key_prefix=config.sync.note_key_prefix,
                            tags=list(config.sync.default_tags),
                        )
                        dtos.append(dto)
                    except Exception as e:
                        result.failed += 1
                        result.errors.append(f'{note.relative_path}: {e}')
                    if on_progress:
                        on_progress('preparing', i + 1, len(changed), note.relative_path)

                if dtos:
                    if on_progress:
                        on_progress('ingesting', 0, len(dtos), 'Submitting to Memex...')

                    note_ids: dict[str, str] = {}

                    if len(dtos) == 1:
                        try:
                            resp = await api.ingest(dtos[0], background=background)
                            if background:
                                result.job_id = resp.job_id if hasattr(resp, 'job_id') else None
                                result.changed = 1
                                return result
                            if resp.status == 'success':
                                result.ingested = 1
                                if resp.note_id:
                                    note_ids[changed[0].relative_path] = resp.note_id
                            elif resp.status == 'skipped':
                                result.skipped = 1
                            else:
                                result.failed = 1
                                if resp.reason:
                                    result.errors.append(resp.reason)
                        except Exception as e:
                            result.failed = 1
                            result.errors.append(str(e))
                    else:
                        try:
                            job_status = await api.ingest_batch(
                                dtos,
                                vault_id=config.server.vault_id,
                                batch_size=config.sync.batch_size,
                            )

                            if background:
                                result.job_id = job_status.job_id
                                return result

                            if on_progress:
                                on_progress(
                                    'ingesting',
                                    0,
                                    len(dtos),
                                    f'Batch job {job_status.job_id} submitted...',
                                )

                            final = await _poll_job(
                                api,
                                job_status.job_id,
                                on_progress=on_progress,
                            )
                            if final is not None and final.result:
                                result.ingested = final.result.processed_count
                                result.skipped = final.result.skipped_count
                                result.failed = final.result.failed_count
                                for err in final.result.errors:
                                    result.errors.append(str(err))
                                # Map note_ids back to changed notes by index
                                for idx, nid in enumerate(final.result.note_ids):
                                    if idx < len(changed) and nid:
                                        note_ids[changed[idx].relative_path] = nid
                            elif final is not None and final.status == 'failed':
                                result.failed = len(dtos)
                                result.errors.append(f'Batch job {job_status.job_id} failed')
                        except Exception as e:
                            result.failed = len(dtos)
                            result.errors.append(str(e))

                    # Update state with note_ids
                    if result.ingested > 0 or result.skipped > 0:
                        state.mark_synced(changed, config.server.vault_id, note_ids=note_ids)

            # 3b. Handle deleted files
            if deleted and handle_deletes:
                arc, hd, del_errors = await _handle_deletes(
                    api,
                    state,
                    deleted,
                    hard_delete=hard_delete,
                    on_progress=on_progress,
                )
                result.archived = arc
                result.hard_deleted = hd
                result.errors.extend(del_errors)

        if on_progress:
            on_progress('done', result.ingested, result.changed, 'Sync complete')

    finally:
        state.close()

    return result
