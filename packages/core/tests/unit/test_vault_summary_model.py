"""Tests for VaultSummary SQLModel definition."""

from datetime import datetime, timezone
from uuid import uuid4

from memex_core.memory.sql_models import VaultSummary


class TestVaultSummaryModel:
    """Verify VaultSummary schema fields and defaults."""

    def test_create_with_defaults(self):
        vault_id = uuid4()
        vs = VaultSummary(vault_id=vault_id)
        assert vs.id is not None
        assert vs.vault_id == vault_id
        assert vs.narrative == ''
        assert vs.themes == []
        assert vs.inventory == {}
        assert vs.version == 1
        assert vs.notes_incorporated == 0
        assert vs.patch_log == []
        assert vs.needs_regeneration is False

    def test_create_with_all_fields(self):
        vault_id = uuid4()
        note_id = uuid4()
        now = datetime.now(timezone.utc)
        themes = [
            {
                'name': 'AI',
                'description': 'AI research',
                'note_count': 5,
                'trend': 'growing',
                'last_addition': '2026-04-06',
                'representative_titles': ['Paper A'],
            }
        ]
        inventory = {'total_notes': 10, 'total_entities': 25}
        patch_log = [{'note_id': str(note_id), 'action': 'patch', 'timestamp': now.isoformat()}]

        vs = VaultSummary(
            vault_id=vault_id,
            narrative='This vault contains AI research notes.',
            themes=themes,
            inventory=inventory,
            version=3,
            notes_incorporated=10,
            patch_log=patch_log,
        )

        assert vs.narrative == 'This vault contains AI research notes.'
        assert vs.themes == themes
        assert vs.inventory == inventory
        assert vs.version == 3
        assert vs.notes_incorporated == 10
        assert vs.patch_log == patch_log

    def test_tablename(self):
        assert VaultSummary.__tablename__ == 'vault_summaries'

    def test_vault_id_not_nullable(self):
        """vault_id DB column is NOT NULL."""
        table = VaultSummary.__table__
        col = table.c.vault_id
        assert col.nullable is False

    def test_vault_id_unique_constraint(self):
        """vault_id column should have unique=True."""
        table = VaultSummary.__table__
        col = table.c.vault_id
        assert col.unique is True

    def test_vault_id_fk_cascade(self):
        """vault_id FK should cascade on delete."""
        table = VaultSummary.__table__
        col = table.c.vault_id
        fks = list(col.foreign_keys)
        assert len(fks) == 1
        assert fks[0].ondelete == 'CASCADE'
        assert fks[0].target_fullname == 'vaults.id'
