from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest

from memex_cli.sync.scanner import VaultNote
from memex_cli.sync.state import SyncStateDB, diff


def _make_note(rel_path: str, mtime: float = 1000.0) -> VaultNote:
    return VaultNote(
        path=Path(f'/vault/{rel_path}'),
        relative_path=rel_path,
        mtime=mtime,
        size=100,
        assets=[],
    )


@pytest.fixture
def db(tmp_path: Path) -> Generator[SyncStateDB, None, None]:
    state = SyncStateDB(tmp_path / '.memex-sync.db')
    yield state
    state.close()


class TestSyncStateDB:
    def test_empty_state(self, db: SyncStateDB) -> None:
        assert db.last_sync is None
        assert db.vault_id is None
        assert db.get_all_files() == {}
        assert db.file_count() == 0

    def test_mark_synced(self, db: SyncStateDB) -> None:
        notes = [_make_note('a.md', 1000.0), _make_note('b.md', 2000.0)]
        db.mark_synced(notes, vault_id='v1')

        assert db.last_sync is not None
        assert db.vault_id == 'v1'
        assert db.file_count() == 2

        files = db.get_all_files()
        assert files['a.md'] == 1000.0
        assert files['b.md'] == 2000.0

    def test_mark_synced_updates_existing(self, db: SyncStateDB) -> None:
        db.mark_synced([_make_note('a.md', 1000.0)])
        db.mark_synced([_make_note('a.md', 2000.0)])

        files = db.get_all_files()
        assert files['a.md'] == 2000.0
        assert db.file_count() == 1

    def test_get_file(self, db: SyncStateDB) -> None:
        db.mark_synced([_make_note('a.md', 1000.0)])

        f = db.get_file('a.md')
        assert f is not None
        assert f.mtime == 1000.0
        assert f.synced_at is not None

        assert db.get_file('nonexistent.md') is None

    def test_remove_files(self, db: SyncStateDB) -> None:
        db.mark_synced([_make_note('a.md'), _make_note('b.md'), _make_note('c.md')])
        assert db.file_count() == 3

        db.remove_files(['a.md', 'c.md'])
        assert db.file_count() == 1
        assert db.get_file('a.md') is None
        assert db.get_file('b.md') is not None

    def test_remove_nonexistent_is_noop(self, db: SyncStateDB) -> None:
        db.remove_files(['does-not-exist.md'])
        assert db.file_count() == 0

    def test_vault_id_preserved(self, db: SyncStateDB) -> None:
        db.mark_synced([_make_note('a.md')], vault_id='original')
        assert db.vault_id == 'original'

        # Syncing without vault_id keeps original
        db.mark_synced([_make_note('b.md')])
        assert db.vault_id == 'original'

    def test_vault_id_updated(self, db: SyncStateDB) -> None:
        db.mark_synced([_make_note('a.md')], vault_id='old')
        db.mark_synced([_make_note('b.md')], vault_id='new')
        assert db.vault_id == 'new'

    def test_mark_synced_with_note_ids(self, db: SyncStateDB) -> None:
        notes = [_make_note('a.md', 1000.0), _make_note('b.md', 2000.0)]
        note_ids = {'a.md': 'note-uuid-aaa', 'b.md': 'note-uuid-bbb'}
        db.mark_synced(notes, vault_id='v1', note_ids=note_ids)

        f = db.get_file('a.md')
        assert f is not None
        assert f.note_id == 'note-uuid-aaa'

        f2 = db.get_file('b.md')
        assert f2 is not None
        assert f2.note_id == 'note-uuid-bbb'

    def test_mark_synced_updates_note_id(self, db: SyncStateDB) -> None:
        db.mark_synced([_make_note('a.md')], note_ids={'a.md': 'old-id'})
        db.mark_synced([_make_note('a.md', 2000.0)], note_ids={'a.md': 'new-id'})

        f = db.get_file('a.md')
        assert f is not None
        assert f.note_id == 'new-id'

    def test_mark_synced_preserves_note_id_if_not_provided(self, db: SyncStateDB) -> None:
        db.mark_synced([_make_note('a.md')], note_ids={'a.md': 'existing-id'})
        # Re-sync without note_ids — should preserve the existing note_id
        db.mark_synced([_make_note('a.md', 2000.0)])

        f = db.get_file('a.md')
        assert f is not None
        assert f.note_id == 'existing-id'

    def test_get_note_ids_for_paths(self, db: SyncStateDB) -> None:
        db.mark_synced(
            [_make_note('a.md'), _make_note('b.md'), _make_note('c.md')],
            note_ids={'a.md': 'id-a', 'c.md': 'id-c'},
        )

        result = db.get_note_ids_for_paths(['a.md', 'b.md', 'c.md', 'nonexistent.md'])
        assert result == {'a.md': 'id-a', 'c.md': 'id-c'}
        # b.md has no note_id, nonexistent.md not in DB

    def test_get_note_ids_for_paths_empty(self, db: SyncStateDB) -> None:
        assert db.get_note_ids_for_paths(['anything.md']) == {}

    def test_persistence(self, tmp_path: Path) -> None:
        db_path = tmp_path / 'test.db'

        state1 = SyncStateDB(db_path)
        state1.mark_synced([_make_note('a.md', 1000.0)], vault_id='v1')
        state1.close()

        state2 = SyncStateDB(db_path)
        assert state2.file_count() == 1
        assert state2.get_all_files()['a.md'] == 1000.0
        assert state2.vault_id == 'v1'
        assert state2.last_sync is not None
        state2.close()

    def test_mark_synced_clears_archived_flag(self, db: SyncStateDB) -> None:
        """mark_synced should clear the archived flag on existing entries."""
        db.mark_synced([_make_note('a.md', 1000.0)], note_ids={'a.md': 'id-a'})
        db.archive_files(['a.md'])
        assert 'a.md' not in db.get_all_files()

        # Re-syncing should clear the archived flag
        db.mark_synced([_make_note('a.md', 2000.0)], note_ids={'a.md': 'id-a-new'})
        assert 'a.md' in db.get_all_files()
        f = db.get_file('a.md')
        assert f is not None
        assert f.archived is False
        assert f.note_id == 'id-a-new'


class TestArchiveFiles:
    def test_archive_files(self, db: SyncStateDB) -> None:
        db.mark_synced(
            [_make_note('a.md'), _make_note('b.md')],
            note_ids={'a.md': 'id-a', 'b.md': 'id-b'},
        )
        db.archive_files(['a.md'])

        # Archived file excluded from get_all_files
        assert 'a.md' not in db.get_all_files()
        assert 'b.md' in db.get_all_files()

        # Archived file appears in get_archived_files
        archived = db.get_archived_files()
        assert archived == {'a.md': 'id-a'}

    def test_get_archived_files_excludes_no_note_id(self, db: SyncStateDB) -> None:
        db.mark_synced([_make_note('no-id.md')])
        db.archive_files(['no-id.md'])

        assert db.get_archived_files() == {}

    def test_unarchive_file(self, db: SyncStateDB) -> None:
        db.mark_synced([_make_note('a.md', 1000.0)], note_ids={'a.md': 'id-a'})
        db.archive_files(['a.md'])
        assert 'a.md' not in db.get_all_files()

        db.unarchive_file('a.md', 2000.0)
        assert 'a.md' in db.get_all_files()
        assert db.get_all_files()['a.md'] == 2000.0
        assert db.get_archived_files() == {}

        # note_id preserved
        f = db.get_file('a.md')
        assert f is not None
        assert f.note_id == 'id-a'

    def test_archive_nonexistent_is_noop(self, db: SyncStateDB) -> None:
        db.archive_files(['does-not-exist.md'])
        assert db.get_archived_files() == {}


class TestDiff:
    def test_all_new(self, db: SyncStateDB) -> None:
        notes = [_make_note('a.md'), _make_note('b.md')]
        changed, deleted, returning = diff(db, notes)
        assert len(changed) == 2
        assert deleted == []
        assert returning == []

    def test_unchanged(self, db: SyncStateDB) -> None:
        db.mark_synced([_make_note('a.md', 1000.0)])
        notes = [_make_note('a.md', mtime=1000.0)]
        changed, deleted, returning = diff(db, notes)
        assert changed == []
        assert deleted == []
        assert returning == []

    def test_modified(self, db: SyncStateDB) -> None:
        db.mark_synced([_make_note('a.md', 1000.0)])
        notes = [_make_note('a.md', mtime=2000.0)]
        changed, deleted, returning = diff(db, notes)
        assert len(changed) == 1
        assert changed[0].relative_path == 'a.md'
        assert returning == []

    def test_deleted(self, db: SyncStateDB) -> None:
        db.mark_synced([_make_note('gone.md', 1000.0)])
        changed, deleted, returning = diff(db, [])
        assert changed == []
        assert deleted == ['gone.md']
        assert returning == []

    def test_mixed(self, db: SyncStateDB) -> None:
        db.mark_synced(
            [
                _make_note('unchanged.md', 1000.0),
                _make_note('modified.md', 1000.0),
                _make_note('deleted.md', 1000.0),
            ]
        )
        notes = [
            _make_note('unchanged.md', mtime=1000.0),
            _make_note('modified.md', mtime=2000.0),
            _make_note('new.md', mtime=3000.0),
        ]
        changed, deleted, returning = diff(db, notes)
        changed_paths = {n.relative_path for n in changed}
        assert changed_paths == {'modified.md', 'new.md'}
        assert deleted == ['deleted.md']
        assert returning == []

    def test_returning_archived(self, db: SyncStateDB) -> None:
        """Archived notes reappearing in the vault appear in returning, not changed."""
        db.mark_synced(
            [_make_note('skipped.md', 1000.0)],
            note_ids={'skipped.md': 'note-id-123'},
        )
        db.archive_files(['skipped.md'])

        notes = [_make_note('skipped.md', mtime=2000.0)]
        changed, deleted, returning = diff(db, notes)

        assert changed == []
        assert deleted == []
        assert len(returning) == 1
        assert returning[0].relative_path == 'skipped.md'

    def test_archived_not_in_deleted(self, db: SyncStateDB) -> None:
        """Archived files should not appear as deleted (they're already handled)."""
        db.mark_synced(
            [_make_note('archived.md', 1000.0)],
            note_ids={'archived.md': 'note-id'},
        )
        db.archive_files(['archived.md'])

        # Note is NOT in vault scan
        changed, deleted, returning = diff(db, [])
        assert deleted == []
        assert returning == []
