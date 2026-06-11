"""Add ``role`` column to ``notes`` for experiential plane classification.

Adds a nullable ``role`` column to ``notes`` — NULL for ordinary
declarative-plane notes, one of:

    * 'case'       — raw or derived experience record (parent of a procedure)
    * 'procedure'  — a how-to recipe synthesised from one or more cases
    * 'strategy'   — an opinionated play-book that picks a procedure for a context

The full experiential entity lifecycle (status, lineage, embedding,
trigger, body) lives in ``experiential_entries`` (migration 061); the
``notes.role`` column is a thin provenance tag that lets the declarative
plane surface experiential content without joining.

A CHECK constraint enforces the value set; a partial index covers the
non-NULL rows so the briefing-side scan is cheap.

Revision ID: 062_notes_role
Revises: 061_experiential_entries
Create Date: 2026-06-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '062_notes_role'
down_revision: str | None = '061_experiential_entries'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_exists(conn, table: str, column: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                'SELECT EXISTS ('
                '  SELECT 1 FROM information_schema.columns '
                '  WHERE table_schema = current_schema() '
                '    AND table_name = :table AND column_name = :column'
                ')'
            ),
            {'table': table, 'column': column},
        ).scalar()
    )


def _constraint_exists(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                'SELECT EXISTS ('
                '  SELECT 1 FROM information_schema.table_constraints '
                '  WHERE table_schema = current_schema() '
                '    AND constraint_name = :name'
                ')'
            ),
            {'name': name},
        ).scalar()
    )


def _index_exists(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text('SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = :name)'),
            {'name': name},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()

    if not _column_exists(conn, 'notes', 'role'):
        op.add_column(
            'notes',
            sa.Column('role', sa.Text(), nullable=True),
        )

    if not _constraint_exists(conn, 'ck_notes_role'):
        op.create_check_constraint(
            'ck_notes_role',
            'notes',
            "role IS NULL OR role IN ('case', 'procedure', 'strategy')",
        )

    if not _index_exists(conn, 'idx_notes_role'):
        op.execute(
            sa.text('CREATE INDEX idx_notes_role ON notes (vault_id, role) WHERE role IS NOT NULL')
        )


def downgrade() -> None:
    conn = op.get_bind()

    if _index_exists(conn, 'idx_notes_role'):
        op.drop_index('idx_notes_role', table_name='notes')
    if _constraint_exists(conn, 'ck_notes_role'):
        op.drop_constraint('ck_notes_role', 'notes', type_='check')
    if _column_exists(conn, 'notes', 'role'):
        op.drop_column('notes', 'role')
