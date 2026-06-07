"""Tests for migration 058_vault_summary_embedding.

Asserts the nullable narrative-embedding column lands on both the SQLModel
metadata side and as an alembic migration that chains correctly off the
prior head.
"""

from __future__ import annotations

import importlib.util
import pathlib as plb
from unittest.mock import patch

from pgvector.sqlalchemy import Vector

from memex_core.memory.sql_models import EMBEDDING_DIMENSION, VaultSummary

_VERSIONS_DIR = (
    plb.Path(__file__).resolve().parents[2] / 'src' / 'memex_core' / 'alembic' / 'versions'
)


def _load_migration(name: str):
    src = _VERSIONS_DIR / f'{name}.py'
    spec = importlib.util.spec_from_file_location(name, src)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestMigration058Metadata:
    def test_revision_id(self) -> None:
        mod = _load_migration('058_vault_summary_embedding')
        assert mod.revision == '058_vault_summary_embedding'

    def test_chains_off_057(self) -> None:
        mod = _load_migration('058_vault_summary_embedding')
        assert mod.down_revision == '057_lint_source_external'

    def test_revision_id_fits_in_alembic_version_column(self) -> None:
        """alembic_version.version_num is VARCHAR(32) post-migration 050."""
        mod = _load_migration('058_vault_summary_embedding')
        assert len(mod.revision) <= 32


class TestMigration058Operations:
    def test_upgrade_adds_nullable_vector_column(self) -> None:
        mod = _load_migration('058_vault_summary_embedding')
        with patch.object(mod, 'op') as mock_op:
            mod.upgrade()

        mock_op.add_column.assert_called_once()
        args, _ = mock_op.add_column.call_args
        assert args[0] == 'vault_summaries'
        column = args[1]
        assert column.name == 'embedding'
        assert column.nullable is True
        assert isinstance(column.type, Vector)
        assert column.type.dim == EMBEDDING_DIMENSION

    def test_downgrade_drops_column(self) -> None:
        mod = _load_migration('058_vault_summary_embedding')
        with patch.object(mod, 'op') as mock_op:
            mod.downgrade()

        mock_op.drop_column.assert_called_once_with('vault_summaries', 'embedding')


class TestSqlModelSide:
    def test_vault_summary_model_has_nullable_vector_column(self) -> None:
        column = VaultSummary.__table__.columns['embedding']
        assert column.nullable is True
        assert isinstance(column.type, Vector)
        assert column.type.dim == EMBEDDING_DIMENSION
