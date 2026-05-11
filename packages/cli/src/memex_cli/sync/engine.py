from __future__ import annotations

import asyncio
import base64
import re
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

import structlog
from pydantic import BaseModel, Field

from memex_common.client import RemoteMemexAPI
from memex_common.schemas import BatchJobStatus, NoteCreateDTO, VaultDTO

from .config import SyncConfig
from .scanner import VaultNote, parse_frontmatter, scan_vault
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
    unarchived: int = Field(
        default=0,
        description='Number of previously archived notes reactivated in Memex.',
    )
    migrated: int = Field(
        default=0,
        description='Number of notes migrated to a different vault via frontmatter override. '
        'Each migrated note is archived in its prior vault and re-ingested in the target.',
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
    override_vault: VaultDTO | None = None,
) -> NoteCreateDTO:
    """Build a NoteCreateDTO from a VaultNote.

    WHY: note_key embeds vault_name (mutable) rather than vault_id (stable).
    Renaming a vault folder mid-sync would re-key all of its notes — a known
    latent limitation. V2 mitigates re-keying for the *frontmatter-override*
    case via explicit migration in sync_vault; a deeper fix would store
    vault_id in the key. Filed in BACKLOG.
    """
    content_bytes = note.path.read_bytes()
    if override_vault is not None:
        effective_vault_name = override_vault.name
        effective_vault_id: str | None = str(override_vault.id)
    else:
        effective_vault_name = vault_name
        effective_vault_id = vault_id
    note_key = f'{note_key_prefix}:{effective_vault_name}:{note.relative_path}'
    name = note.path.stem

    files_dict: dict[str, bytes] = {}
    for asset in note.assets:
        asset_bytes = asset.path.read_bytes()
        files_dict[asset.relative_path] = base64.b64encode(asset_bytes)

    # Set filename for non-markdown files so the server can detect the format
    # and convert via FileContentProcessor
    is_markdown = note.path.suffix.lower() == '.md'
    filename = None if is_markdown else note.path.name

    return NoteCreateDTO(
        name=name,
        description='',
        content=base64.b64encode(content_bytes),
        files=files_dict,
        tags=tags or [],
        note_key=note_key,
        vault_id=effective_vault_id,
        filename=filename,
    )


def _post_sync_state(
    notes: list[VaultNote],
    effective_targets: dict[str, VaultDTO],
    note_key_prefix: str,
) -> tuple[dict[str, str], dict[str, str]]:
    """Compute (note_keys, per_file_vault_ids) maps for state.mark_synced.

    Only notes with an effective target (explicit frontmatter override or
    implicit stored-state target) get an entry — mark_synced preserves
    existing values for notes omitted from the dicts. This avoids
    overwriting per-file vault_id when the user re-syncs a note that has
    no override (which would silently desync state from the server's
    actual placement of the note).
    """
    note_keys: dict[str, str] = {}
    per_file_vault_ids: dict[str, str] = {}
    for note in notes:
        target = effective_targets.get(note.relative_path)
        if target is None:
            continue
        note_keys[note.relative_path] = f'{note_key_prefix}:{target.name}:{note.relative_path}'
        per_file_vault_ids[note.relative_path] = str(target.id)
    return note_keys, per_file_vault_ids


def _read_frontmatter_vault(note: VaultNote, vault_key: str) -> str | None:
    """Return the raw frontmatter value of ``vault_key`` for ``note``, or None.

    Markdown-only — binary file types in include_extensions have no frontmatter.
    """
    if note.path.suffix.lower() != '.md':
        return None
    try:
        content = note.path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return None
    raw = parse_frontmatter(content).get(vault_key, '').strip()
    return raw or None


async def _resolve_frontmatter_vaults(
    api: RemoteMemexAPI,
    notes: list[VaultNote],
    vault_key: str,
) -> tuple[dict[str, VaultDTO], dict[str, VaultDTO]]:
    """Resolve frontmatter ``vault:`` overrides for the given notes.

    Returns ``(overrides, by_id)`` where:
    - ``overrides`` maps ``relative_path → VaultDTO`` for every note whose
      frontmatter cites a vault that resolves to an existing memex vault.
      Unknown vault names are logged once each and excluded — caller falls
      back to the active sync vault for those paths.
    - ``by_id`` is the full ``vault_id (str) → VaultDTO`` directory pulled
      from ``list_vaults()``. Callers reuse this to resolve *implicit*
      targets for notes that have a stored ``SyncedFile.vault_id`` but no
      explicit frontmatter override (keeping repeat syncs idempotent on the
      server's previously-stored ``note_key``).

    Memoizes via a single ``list_vaults()`` lookup so each unique override
    name costs zero additional HTTP round-trips after the first call.
    """
    requested: dict[str, str] = {}
    for note in notes:
        raw = _read_frontmatter_vault(note, vault_key)
        if raw is not None:
            requested[note.relative_path] = raw

    if not requested:
        return {}, {}

    try:
        all_vaults = await api.list_vaults()
    except Exception as e:
        logger.warning(
            'Failed to list vaults for frontmatter override resolution; '
            'falling back to default vault for all overrides.',
            error=str(e),
        )
        return {}, {}

    by_name: dict[str, VaultDTO] = {v.name: v for v in all_vaults}
    by_id: dict[str, VaultDTO] = {str(v.id): v for v in all_vaults}

    resolved: dict[str, VaultDTO] = {}
    warned: set[str] = set()
    for rel_path, raw in requested.items():
        vault = by_name.get(raw) or by_id.get(raw)
        if vault is not None:
            resolved[rel_path] = vault
        elif raw not in warned:
            logger.warning(
                'Frontmatter vault override does not match any known vault; '
                'falling back to default vault.',
                path=rel_path,
                requested_vault=raw,
            )
            warned.add(raw)

    return resolved, by_id


async def _fetch_vaults_by_id(api: RemoteMemexAPI) -> dict[str, VaultDTO]:
    """Best-effort cache of vault_id → VaultDTO for implicit-target lookup."""
    try:
        all_vaults = await api.list_vaults()
    except Exception as e:
        logger.warning(
            'Failed to list vaults; implicit vault targets unresolved.',
            error=str(e),
        )
        return {}
    return {str(v.id): v for v in all_vaults}


def _compute_effective_targets(
    notes: list[VaultNote],
    overrides: dict[str, VaultDTO],
    state: SyncStateDB,
    vaults_by_id: dict[str, VaultDTO],
) -> dict[str, VaultDTO]:
    """Compose explicit frontmatter overrides with implicit stored-state
    targets to produce per-note routing decisions.

    Priority per note:
    1. Frontmatter override (caller already resolved to a VaultDTO).
    2. Stored ``SyncedFile.vault_id`` looked up in ``vaults_by_id`` — so a
       note that was previously routed via override and then had its
       override removed continues to be ingested into the same vault on
       subsequent syncs (the documented "removing the override does not
       auto-revert" behavior).
    3. None — caller falls back to the active sync vault.
    """
    targets: dict[str, VaultDTO] = {}
    for note in notes:
        explicit = overrides.get(note.relative_path)
        if explicit is not None:
            targets[note.relative_path] = explicit
            continue
        existing = state.get_file(note.relative_path)
        if existing is None or existing.vault_id is None:
            continue
        implicit = vaults_by_id.get(existing.vault_id)
        if implicit is not None:
            targets[note.relative_path] = implicit
    return targets


async def _poll_job(
    api: RemoteMemexAPI,
    job_id: UUID,
    poll_interval: float = 2.0,
    on_progress: ProgressCallback | None = None,
) -> BatchJobStatus | None:
    """Poll a batch job until it reaches a terminal state (completed/failed).

    Polls indefinitely while the server reports ``pending`` or ``processing``.
    If the server becomes unreachable, retries up to 30 consecutive times
    (~1 minute at *poll_interval*) before giving up and returning the last
    known status so callers can handle partial progress.

    Logs a warning when progress stalls for 5 minutes but keeps polling.
    """
    status: BatchJobStatus | None = None
    last_progress_value = -1
    stale_polls = 0
    consecutive_errors = 0
    max_consecutive_errors = 30
    stale_warn_threshold = int(300 / poll_interval)  # 5 minutes

    while True:
        try:
            status = await api.get_job_status(job_id)
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            if consecutive_errors >= max_consecutive_errors:
                logger.error(
                    'Lost connection to server while polling batch job',
                    job_id=str(job_id),
                    error=str(e),
                    consecutive_errors=consecutive_errors,
                )
                return status
            logger.warning(
                'Failed to poll batch job, retrying',
                job_id=str(job_id),
                error=str(e),
                attempt=consecutive_errors,
            )
            await asyncio.sleep(poll_interval)
            continue

        assert status is not None  # assigned in try, exception continues

        if on_progress and (status.progress or status.total_count):
            current = status.processed_count or 0
            total = status.total_count or 0
            # Fallback: parse "Processed X/Y notes" if server lacks numeric fields
            if not current and not total and status.progress:
                m = re.search(r'(\d+)/(\d+)', status.progress)
                if m:
                    current, total = int(m.group(1)), int(m.group(2))
            on_progress('ingesting', current, total, status.progress or '')

            # Stale detection: warn but keep polling
            if current == last_progress_value:
                stale_polls += 1
                if stale_polls == stale_warn_threshold:
                    logger.warning(
                        'Batch job progress stalled',
                        job_id=str(job_id),
                        stuck_at=f'{current}/{total}',
                        stale_seconds=int(stale_polls * poll_interval),
                    )
                    stale_polls = 0  # reset so we warn again after another 5 min
            else:
                stale_polls = 0
                last_progress_value = current

        if status.status in ('completed', 'failed'):
            return status
        await asyncio.sleep(poll_interval)


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

    # Update state for successfully handled files
    handled = [p for p, _ in paths_with_ids if p not in {e.split(':')[0] for e in errors}]
    if handled:
        if hard_delete:
            state.remove_files(handled)
        else:
            # Soft-delete: mark as archived so we can unarchive if the note returns
            state.archive_files(handled)

    # Also remove files without note_ids from state (can't do anything with them)
    if paths_without_ids:
        state.remove_files(paths_without_ids)

    return archived, hard_deleted_count, errors


async def sync_vault(
    vault_path: Path,
    api: RemoteMemexAPI,
    sync_config: SyncConfig,
    vault_id: str | None = None,
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
        api: RemoteMemexAPI client (already connected).
        sync_config: Sync behavior configuration.
        vault_id: Target vault ID (None = server default).
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
    all_notes = scan_vault(
        vault_path,
        sync_config.exclude,
        sync_config.assets,
        sync_config.include_extensions,
    )
    result.total_scanned = len(all_notes)
    if on_progress:
        on_progress(
            'scanning',
            result.total_scanned,
            result.total_scanned,
            f'Found {result.total_scanned} notes',
        )

    # 2. Diff
    db_path = vault_path / sync_config.state_file
    state = SyncStateDB(db_path)
    try:
        if full:
            changed = list(all_notes)
            deleted: list[str] = []
            returning: list[VaultNote] = []
        else:
            changed, deleted, returning = diff(state, all_notes)
        result.deleted_detected = deleted

        if notes_filter is not None:
            filter_set = set(notes_filter)
            changed = [n for n in changed if n.relative_path in filter_set]

        result.changed = len(changed)

        if not changed and not deleted and not returning:
            return result

        if dry_run:
            return result

        # Resolve frontmatter vault overrides for changed + returning notes.
        # ``overrides`` maps rel_path → VaultDTO for paths whose frontmatter
        # cites a known vault (unknown names log a warning and are excluded).
        # ``vaults_by_id`` is the directory used both here and to resolve
        # implicit targets (stored-state) without a second HTTP round-trip.
        override_candidates = list(changed) + list(returning)
        overrides, vaults_by_id = await _resolve_frontmatter_vaults(
            api,
            override_candidates,
            sync_config.exclude.frontmatter_vault_key,
        )
        if not vaults_by_id:
            # No overrides were requested → directory not fetched yet, but we
            # still need it to honor implicit targets for previously-routed
            # notes. One extra HTTP call only when at least one tracked note
            # has a non-null SyncedFile.vault_id.
            if any(
                (existing := state.get_file(n.relative_path)) is not None
                and existing.vault_id is not None
                for n in override_candidates
            ):
                vaults_by_id = await _fetch_vaults_by_id(api)

        # Compose effective per-note routing target (explicit override OR
        # implicit-from-stored-vault_id). All downstream code uses this map.
        effective_targets = _compute_effective_targets(
            override_candidates,
            overrides,
            state,
            vaults_by_id,
        )

        # Detect *pending* migrations: a note already tracked in SyncedFile
        # whose explicit frontmatter override differs from its previously-
        # recorded vault_id. We materialize them into a dict keyed by
        # relative_path; the post-ingest pass archives the old note ONLY for
        # paths whose new ingest succeeded. Archiving up front would lose the
        # user's note if the new ingest fails.
        pending_migrations: dict[str, tuple[str, VaultDTO]] = {}
        for note in changed:
            target = overrides.get(note.relative_path)
            if target is None:
                continue
            existing = state.get_file(note.relative_path)
            if existing is None or existing.note_id is None or existing.vault_id == str(target.id):
                continue
            pending_migrations[note.relative_path] = (existing.note_id, target)

        # 3. Build DTOs and ingest
        # 3a. Ingest changed notes
        if changed:
            if on_progress:
                on_progress('preparing', 0, len(changed), 'Preparing notes...')
            dtos: list[NoteCreateDTO] = []
            dto_notes: list[VaultNote] = []
            for i, note in enumerate(changed):
                try:
                    dto = _build_note_dto(
                        note,
                        vault_name,
                        vault_id,
                        note_key_prefix=sync_config.note_key_prefix,
                        tags=list(sync_config.default_tags),
                        override_vault=effective_targets.get(note.relative_path),
                    )
                    dtos.append(dto)
                    dto_notes.append(note)
                except Exception as e:
                    result.failed += 1
                    result.errors.append(f'{note.relative_path}: {e}')
                if on_progress:
                    on_progress('preparing', i + 1, len(changed), note.relative_path)

            if dtos:
                if on_progress:
                    on_progress('ingesting', 0, len(dtos), 'Submitting to Memex...')

                note_ids: dict[str, str] = {}
                # Track which notes actually succeeded — used for state
                # persistence AND deferred migration archival so we never
                # archive an old version when the new ingest failed.
                successful_notes: list[VaultNote] = []

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
                                note_ids[dto_notes[0].relative_path] = resp.note_id
                            successful_notes.append(dto_notes[0])
                        elif resp.status == 'skipped':
                            result.skipped = 1
                            # Skipped means server found a match by note_key
                            # — the target vault already has this note. Safe
                            # to persist state + archive any prior version.
                            successful_notes.append(dto_notes[0])
                        else:
                            result.failed = 1
                            if resp.reason:
                                result.errors.append(resp.reason)
                    except Exception as e:
                        result.failed = 1
                        result.errors.append(str(e))
                else:
                    # When any DTO carries a frontmatter override, suppress the
                    # batch-level vault_id so per-note vault_id is respected;
                    # otherwise server-side batch override would clobber it.
                    batch_vault_id = None if effective_targets else vault_id
                    try:
                        job_status = await api.ingest_batch(
                            dtos,
                            vault_id=batch_vault_id,
                            batch_size=sync_config.batch_size,
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
                            # The server's `note_ids` list is aligned with
                            # *processed* notes only — skipped entries are
                            # excluded (services/ingestion.py:967). So when
                            # the batch contains skips or partial failures,
                            # `note_ids[idx]` does NOT correspond to
                            # `dto_notes[idx]`. Trust positional mapping
                            # only when the alignment is unambiguous; in
                            # all other cases preserve existing state by
                            # leaving `note_ids` empty and (when no
                            # failures) treat all DTOs as ingested for
                            # state-mark purposes (server confirms the
                            # ingest succeeded for the totals).
                            n_returned = len(final.result.note_ids)
                            positional_safe = (
                                final.result.failed_count == 0
                                and final.result.skipped_count == 0
                                and n_returned == len(dto_notes)
                            )
                            if positional_safe:
                                for idx, nid in enumerate(final.result.note_ids):
                                    if nid:
                                        note_ids[dto_notes[idx].relative_path] = nid
                                successful_notes.extend(dto_notes)
                            elif final.result.failed_count == 0:
                                # Skips muddle positional mapping. State
                                # update will not overwrite note_id for
                                # the ambiguous positions — mark_synced
                                # only writes note_id when the key is in
                                # the note_ids dict, which we leave empty.
                                successful_notes.extend(dto_notes)
                            else:
                                # Partial failure WITH unreliable
                                # positional mapping — conservatively
                                # treat ALL as failed for state purposes.
                                # The user can re-sync; the server has
                                # already processed what it could, and
                                # idempotency on note_key prevents
                                # duplicates on retry.
                                pass
                        elif final is not None and final.status == 'failed':
                            result.failed = len(dtos)
                            result.errors.append(f'Batch job {job_status.job_id} failed')
                        elif final is not None:
                            # Non-terminal status (e.g. lost connection while processing)
                            result.job_id = job_status.job_id
                            result.errors.append(
                                f'Lost connection to server. '
                                f'Batch job {job_status.job_id} may still be running. '
                                f'Check status: memex note sync job {job_status.job_id}'
                            )
                        else:
                            # final is None — never got a response
                            result.job_id = job_status.job_id
                            result.errors.append(
                                f'Server unreachable. '
                                f'Batch job {job_status.job_id} may still be running. '
                                f'Check status: memex note sync job {job_status.job_id}'
                            )
                    except Exception as e:
                        result.failed = len(dtos)
                        result.errors.append(str(e))

                # Update state ONLY for notes whose ingest succeeded — and
                # only emit per_file_vault_ids/note_keys when we have an
                # effective target (explicit override or implicit stored).
                # mark_synced preserves prior values for notes omitted from
                # the dicts, keeping state aligned with the server when an
                # override is later removed.
                if successful_notes:
                    note_keys, per_file_vault_ids = _post_sync_state(
                        successful_notes,
                        effective_targets,
                        sync_config.note_key_prefix,
                    )
                    state.mark_synced(
                        successful_notes,
                        vault_id,
                        note_ids=note_ids,
                        note_keys=note_keys,
                        per_file_vault_ids=per_file_vault_ids,
                    )

                    # Deferred migration archive — only for notes whose
                    # new ingest succeeded. Archiving up front would leave
                    # the user with no active note on partial failures.
                    successful_paths = {n.relative_path for n in successful_notes}
                    for rel_path, (old_note_id, target) in pending_migrations.items():
                        if rel_path not in successful_paths:
                            continue
                        try:
                            old_uuid = UUID(old_note_id)
                        except ValueError:
                            result.errors.append(
                                f'{rel_path}: migrate archive skipped — stored '
                                f'note_id is not a valid UUID ({old_note_id!r})'
                            )
                            logger.warning(
                                'Stored note_id is not a valid UUID; '
                                'skipping migration archive for this path',
                                path=rel_path,
                                old_note_id=old_note_id,
                            )
                            continue
                        try:
                            await api.set_note_status(old_uuid, 'archived')
                            result.migrated += 1
                            logger.info(
                                'Archived prior-vault note after successful migration',
                                path=rel_path,
                                old_note_id=old_note_id,
                                target_vault=target.name,
                                target_vault_id=str(target.id),
                            )
                        except Exception as e:
                            result.errors.append(f'{rel_path}: migrate archive failed: {e}')
                            logger.error(
                                'Failed to archive prior version during vault migration',
                                path=rel_path,
                                old_note_id=old_note_id,
                                error=str(e),
                            )

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

        # 3c. Handle returning archived notes (unarchive + re-ingest)
        if returning:
            archived_map = state.get_archived_files()
            for note in returning:
                note_id_str = archived_map.get(note.relative_path)
                if not note_id_str:
                    # Archived without a note_id — can't unarchive, ingest as new
                    logger.warning(
                        'Returning archived file has no note_id, ingesting as new',
                        path=note.relative_path,
                    )
                    try:
                        dto = _build_note_dto(
                            note,
                            vault_name,
                            vault_id,
                            note_key_prefix=sync_config.note_key_prefix,
                            tags=list(sync_config.default_tags),
                            override_vault=effective_targets.get(note.relative_path),
                        )
                        resp = await api.ingest(dto, background=False)
                        nks, pvids = _post_sync_state(
                            [note],
                            effective_targets,
                            sync_config.note_key_prefix,
                        )
                        if resp.status == 'success':
                            result.ingested += 1
                            nids = {}
                            if resp.note_id:
                                nids[note.relative_path] = resp.note_id
                            state.mark_synced(
                                [note],
                                vault_id,
                                note_ids=nids,
                                note_keys=nks,
                                per_file_vault_ids=pvids,
                            )
                        elif resp.status == 'skipped':
                            result.skipped += 1
                            state.mark_synced(
                                [note],
                                vault_id,
                                note_keys=nks,
                                per_file_vault_ids=pvids,
                            )
                    except Exception as e:
                        result.errors.append(f'{note.relative_path}: ingest failed: {e}')
                    continue
                try:
                    note_uuid = UUID(note_id_str)
                    await api.set_note_status(note_uuid, 'active')
                    state.unarchive_file(note.relative_path, note.mtime)
                    result.unarchived += 1
                    logger.info(
                        'Unarchived note',
                        path=note.relative_path,
                        note_id=note_id_str,
                    )

                    # Re-ingest to pick up any content changes while skipped
                    dto = _build_note_dto(
                        note,
                        vault_name,
                        vault_id,
                        note_key_prefix=sync_config.note_key_prefix,
                        tags=list(sync_config.default_tags),
                        override_vault=effective_targets.get(note.relative_path),
                    )
                    resp = await api.ingest(dto, background=False)
                    if resp.status == 'success':
                        result.ingested += 1
                    elif resp.status == 'skipped':
                        result.skipped += 1
                    nks, pvids = _post_sync_state(
                        [note],
                        effective_targets,
                        sync_config.note_key_prefix,
                    )
                    state.mark_synced(
                        [note],
                        vault_id,
                        note_keys=nks,
                        per_file_vault_ids=pvids,
                    )
                except Exception as e:
                    result.errors.append(f'{note.relative_path}: unarchive failed: {e}')
                    logger.error(
                        'Failed to unarchive note',
                        path=note.relative_path,
                        error=str(e),
                    )

        if on_progress:
            total_handled = result.ingested + result.skipped + result.failed
            if result.ingested > 0 or result.skipped > 0:
                detail = f'Synced {result.ingested} ingested, {result.skipped} skipped'
                if result.failed:
                    detail += f', {result.failed} failed'
            elif result.failed > 0:
                detail = f'Failed: {result.failed} notes'
            elif result.errors:
                detail = result.errors[0][:80]
            else:
                detail = 'No changes'
            on_progress('done', total_handled, result.changed, detail)

    finally:
        state.close()

    return result
