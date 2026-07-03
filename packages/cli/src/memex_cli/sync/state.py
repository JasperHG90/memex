from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text
from sqlmodel import Field, Session, SQLModel, create_engine, select

from .scanner import VaultNote

DEFAULT_STATE_DB = '.memex-sync.db'

# Tables owned by this module — used to filter create_all so we don't
# accidentally create memex_core's PostgreSQL-specific tables on SQLite.
_OUR_TABLES = {'synced_files', 'sync_meta'}


class SyncedFile(SQLModel, table=True):  # type: ignore[call-arg]
    """A file that has been synced to Memex."""

    __tablename__ = 'synced_files'

    relative_path: str = Field(primary_key=True, description='Path relative to the vault root.')
    mtime: float = Field(description='File modification time at last sync (Unix timestamp).')
    note_id: str | None = Field(
        default=None,
        description='Memex note ID returned after ingestion.',
    )
    synced_at: str = Field(description='ISO 8601 timestamp of when this file was last synced.')
    archived: bool = Field(
        default=False,
        description='Whether this file has been archived in Memex (e.g. due to skip tag).',
    )
    note_key: str | None = Field(
        default=None,
        description='note_key used at last ingest. Needed to archive the old key '
        'when a frontmatter vault override moves the note to a different vault.',
    )
    vault_id: str | None = Field(
        default=None,
        description='Vault ID the file was last ingested into. When this differs from '
        'the current target vault (resolved from frontmatter override), the sync '
        'engine archives the prior ingest and re-ingests into the new vault.',
    )


class SyncMeta(SQLModel, table=True):  # type: ignore[call-arg]
    """Global sync metadata (singleton row)."""

    __tablename__ = 'sync_meta'

    id: int = Field(default=1, primary_key=True)
    last_sync: str | None = Field(
        default=None,
        description='ISO 8601 timestamp of the last successful sync run.',
    )
    vault_id: str | None = Field(
        default=None,
        description='Memex vault ID used for the last sync.',
    )


def _create_sync_tables(engine) -> None:
    """Create only our sync tables, not the full SQLModel metadata."""
    tables = [t for t in SQLModel.metadata.sorted_tables if t.name in _OUR_TABLES]
    SQLModel.metadata.create_all(engine, tables=tables)


class SyncStateDB:
    """SQLite-backed sync state store."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._engine = create_engine(f'sqlite:///{db_path}', echo=False)
        _create_sync_tables(self._engine)
        self._migrate_add_archived()
        self._migrate_add_note_key()
        self._migrate_add_vault_id()

    def _migrate_add_archived(self) -> None:
        """Add 'archived' column to synced_files if it doesn't exist (schema migration)."""
        with self._engine.connect() as conn:
            try:
                conn.execute(
                    text('ALTER TABLE synced_files ADD COLUMN archived BOOLEAN NOT NULL DEFAULT 0')
                )
                conn.commit()
            except Exception:
                pass  # Column already exists

    def _migrate_add_note_key(self) -> None:
        """Add 'note_key' column to synced_files if it doesn't exist (schema migration)."""
        with self._engine.connect() as conn:
            try:
                conn.execute(text('ALTER TABLE synced_files ADD COLUMN note_key VARCHAR'))
                conn.commit()
            except Exception:
                pass  # Column already exists

    def _migrate_add_vault_id(self) -> None:
        """Add 'vault_id' column to synced_files if it doesn't exist (schema migration)."""
        with self._engine.connect() as conn:
            try:
                conn.execute(text('ALTER TABLE synced_files ADD COLUMN vault_id VARCHAR'))
                conn.commit()
            except Exception:
                pass  # Column already exists

    def close(self) -> None:
        self._engine.dispose()

    @property
    def last_sync(self) -> str | None:
        with Session(self._engine) as session:
            meta = session.get(SyncMeta, 1)
            return meta.last_sync if meta else None

    @property
    def vault_id(self) -> str | None:
        with Session(self._engine) as session:
            meta = session.get(SyncMeta, 1)
            return meta.vault_id if meta else None

    def get_file(self, relative_path: str) -> SyncedFile | None:
        with Session(self._engine) as session:
            return session.get(SyncedFile, relative_path)

    def get_all_files(self) -> dict[str, float]:
        """Return {relative_path: mtime} for all tracked (non-archived) files."""
        with Session(self._engine) as session:
            stmt = select(SyncedFile).where(SyncedFile.archived == False)  # noqa: E712
            files = session.exec(stmt).all()
            return {f.relative_path: f.mtime for f in files}

    def file_count(self) -> int:
        with Session(self._engine) as session:
            return len(session.exec(select(SyncedFile)).all())

    def mark_synced(
        self,
        notes: list[VaultNote],
        vault_id: str | None = None,
        note_ids: dict[str, str] | None = None,
        note_keys: dict[str, str] | None = None,
        per_file_vault_ids: dict[str, str] | None = None,
    ) -> None:
        """Record successfully synced notes and update metadata.

        Args:
            notes: Notes that were synced.
            vault_id: Memex vault ID used for sync_meta (fallback / active vault).
            note_ids: Optional mapping of relative_path -> Memex note_id.
            note_keys: Optional mapping of relative_path -> note_key used at ingest.
                Stored on SyncedFile so a later frontmatter vault override can
                archive the prior key in its original vault.
            per_file_vault_ids: Optional mapping of relative_path -> vault_id the
                file was ingested into. When this differs from the target vault on
                a subsequent sync, the engine migrates the note.
        """
        now = datetime.now(timezone.utc).isoformat()
        note_ids = note_ids or {}
        note_keys = note_keys or {}
        per_file_vault_ids = per_file_vault_ids or {}
        with Session(self._engine) as session:
            for note in notes:
                existing = session.get(SyncedFile, note.relative_path)
                if existing:
                    existing.mtime = note.mtime
                    existing.synced_at = now
                    existing.archived = False
                    if note.relative_path in note_ids:
                        existing.note_id = note_ids[note.relative_path]
                    if note.relative_path in note_keys:
                        existing.note_key = note_keys[note.relative_path]
                    if note.relative_path in per_file_vault_ids:
                        existing.vault_id = per_file_vault_ids[note.relative_path]
                    session.add(existing)
                else:
                    session.add(
                        SyncedFile(
                            relative_path=note.relative_path,
                            mtime=note.mtime,
                            note_id=note_ids.get(note.relative_path),
                            synced_at=now,
                            note_key=note_keys.get(note.relative_path),
                            vault_id=per_file_vault_ids.get(note.relative_path),
                        )
                    )

            meta = session.get(SyncMeta, 1)
            if meta:
                meta.last_sync = now
                if vault_id:
                    meta.vault_id = vault_id
            else:
                session.add(SyncMeta(last_sync=now, vault_id=vault_id))

            session.commit()

    def get_note_ids_for_paths(self, relative_paths: list[str]) -> dict[str, str]:
        """Return {relative_path: note_id} for paths that have a stored note_id."""
        with Session(self._engine) as session:
            result: dict[str, str] = {}
            for path in relative_paths:
                f = session.get(SyncedFile, path)
                if f and f.note_id:
                    result[path] = f.note_id
            return result

    def archive_files(self, relative_paths: list[str]) -> None:
        """Mark tracked files as archived (soft delete, preserves note_id for unarchiving)."""
        with Session(self._engine) as session:
            for path in relative_paths:
                existing = session.get(SyncedFile, path)
                if existing:
                    existing.archived = True
                    session.add(existing)
            session.commit()

    def get_archived_files(self) -> dict[str, str]:
        """Return {relative_path: note_id} for all archived files that have a note_id."""
        with Session(self._engine) as session:
            stmt = select(SyncedFile).where(
                SyncedFile.archived == True,  # noqa: E712
                SyncedFile.note_id.isnot(None),  # type: ignore[union-attr]
            )
            files = session.exec(stmt).all()
            return {f.relative_path: f.note_id for f in files if f.note_id}

    def unarchive_file(self, relative_path: str, mtime: float) -> None:
        """Restore an archived file to active status and update its mtime."""
        now = datetime.now(timezone.utc).isoformat()
        with Session(self._engine) as session:
            existing = session.get(SyncedFile, relative_path)
            if existing:
                existing.archived = False
                existing.mtime = mtime
                existing.synced_at = now
                session.add(existing)
                session.commit()

    def remove_files(self, relative_paths: list[str]) -> None:
        """Remove tracked files (e.g., after deletion from vault)."""
        with Session(self._engine) as session:
            for path in relative_paths:
                existing = session.get(SyncedFile, path)
                if existing:
                    session.delete(existing)
            session.commit()


def diff(
    state: SyncStateDB, notes: list[VaultNote]
) -> tuple[list[VaultNote], list[str], list[VaultNote]]:
    """Compare current vault notes against sync state.

    Returns:
        (changed_or_new, deleted_paths, returning_archived) where:
        - changed_or_new: notes that are new or have a newer mtime (excludes archived returnees)
        - deleted_paths: relative paths in state but no longer on disk (non-archived only)
        - returning_archived: notes that were archived but are now back in the vault
    """
    tracked = state.get_all_files()
    archived = state.get_archived_files()
    current_paths = {n.relative_path for n in notes}

    changed: list[VaultNote] = []
    returning: list[VaultNote] = []
    for note in notes:
        if note.relative_path in archived:
            returning.append(note)
        else:
            prev_mtime = tracked.get(note.relative_path)
            if prev_mtime is None or note.mtime > prev_mtime:
                changed.append(note)

    deleted = [p for p in tracked if p not in current_paths]

    return changed, deleted, returning
