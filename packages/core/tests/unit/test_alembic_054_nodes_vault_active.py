"""Tests for migration 054_nodes_vault_active (B.2).

Asserts the partial covering index on ``nodes(vault_id)`` is present on
both the SQLModel metadata side and as an alembic migration that chains
correctly off the prior head.
"""

from __future__ import annotations

import importlib.util
import pathlib as plb
from unittest.mock import patch

from sqlalchemy import Index

from memex_core.memory.sql_models import Node


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


class TestMigration054Metadata:
    def test_revision_id(self) -> None:
        mod = _load_migration('054_nodes_vault_active_index')
        assert mod.revision == '054_nodes_vault_active'

    def test_chains_off_053(self) -> None:
        mod = _load_migration('054_nodes_vault_active_index')
        assert mod.down_revision == '053_merge_heads'

    def test_revision_id_fits_in_alembic_version_column(self) -> None:
        """alembic_version.version_num is VARCHAR(32) post-migration 050."""
        mod = _load_migration('054_nodes_vault_active_index')
        assert len(mod.revision) <= 32


class TestMigration054Operations:
    def test_upgrade_creates_partial_covering_index(self) -> None:
        mod = _load_migration('054_nodes_vault_active_index')
        with patch.object(mod, 'op') as mock_op:
            mod.upgrade()

        mock_op.create_index.assert_called_once()
        args, kwargs = mock_op.create_index.call_args
        assert args[0] == 'idx_nodes_vault_active'
        assert args[1] == 'nodes'
        assert args[2] == ['vault_id']
        assert kwargs.get('unique') is False
        # Partial predicate is on status + block_id.
        where = str(kwargs['postgresql_where'])
        assert "status = 'active'" in where
        assert 'block_id IS NOT NULL' in where

    def test_downgrade_drops_the_index(self) -> None:
        mod = _load_migration('054_nodes_vault_active_index')
        with patch.object(mod, 'op') as mock_op:
            mod.downgrade()

        mock_op.drop_index.assert_called_once_with('idx_nodes_vault_active', table_name='nodes')


class TestNodeSqlModelMirror:
    """The SQLModel side must declare the same partial index so
    `SQLModel.metadata.create_all()` (used on fresh-DB bootstrap) emits
    the same DDL as the alembic chain.
    """

    def test_partial_index_present_on_node_table_args(self) -> None:
        # Locate the Index in Node.__table_args__ by name.
        indexes = [
            arg
            for arg in Node.__table_args__
            if isinstance(arg, Index) and arg.name == 'idx_nodes_vault_active'
        ]
        assert len(indexes) == 1, (
            f'Expected exactly one idx_nodes_vault_active in Node.__table_args__, '
            f'found {len(indexes)}'
        )
        idx = indexes[0]
        # Indexed columns: vault_id only.
        assert [c.name for c in idx.expressions] == ['vault_id']
        # Partial predicate must reference both status and block_id.
        where_sql = str(idx.dialect_options['postgresql'].get('where'))
        assert "status = 'active'" in where_sql
        assert 'block_id IS NOT NULL' in where_sql
