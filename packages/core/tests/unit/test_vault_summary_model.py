"""Tests for VaultSummary SQLModel definition."""

from datetime import datetime, timezone
from uuid import uuid4

from memex_core.memory.sql_models import VaultSummary, EMBEDDING_DIMENSION


class TestVaultSummaryModel:
    """Verify VaultSummary schema fields and defaults."""

    def test_create_with_defaults(self):
        vault_id = uuid4()
        vs = VaultSummary(vault_id=vault_id)
        assert vs.id is not None
        assert vs.vault_id == vault_id
        assert vs.summary == ''
        assert vs.topics == []
        assert vs.stats == {}
        assert vs.version == 1
        assert vs.notes_incorporated == 0
        assert vs.last_note_id is None
        assert vs.patch_log == []
        assert vs.embedding is None

    def test_create_with_all_fields(self):
        vault_id = uuid4()
        note_id = uuid4()
        now = datetime.now(timezone.utc)
        topics = [{'name': 'AI', 'note_count': 5, 'description': 'AI topics'}]
        stats = {'total_notes': 10, 'total_entities': 25}
        patch_log = [{'note_id': str(note_id), 'action': 'patch', 'timestamp': now.isoformat()}]
        embedding = [0.1] * EMBEDDING_DIMENSION

        vs = VaultSummary(
            vault_id=vault_id,
            summary='This vault contains AI research notes.',
            topics=topics,
            stats=stats,
            version=3,
            notes_incorporated=10,
            last_note_id=note_id,
            patch_log=patch_log,
            embedding=embedding,
        )

        assert vs.summary == 'This vault contains AI research notes.'
        assert vs.topics == topics
        assert vs.stats == stats
        assert vs.version == 3
        assert vs.notes_incorporated == 10
        assert vs.last_note_id == note_id
        assert vs.patch_log == patch_log
        assert len(vs.embedding) == EMBEDDING_DIMENSION

    def test_tablename(self):
        assert VaultSummary.__tablename__ == 'vault_summaries'

    def test_vault_id_not_nullable(self):
        """vault_id DB column is NOT NULL."""
        table = VaultSummary.__table__
        col = table.c.vault_id
        assert col.nullable is False

    def test_embedding_dimension_matches_constant(self):
        """Embedding uses EMBEDDING_DIMENSION, not a hardcoded value."""
        # Check via the SA column definition
        table = VaultSummary.__table__
        col = table.c.embedding
        assert col.type.dim == EMBEDDING_DIMENSION

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
