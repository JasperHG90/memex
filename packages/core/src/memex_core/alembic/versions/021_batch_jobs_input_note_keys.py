"""Add input_note_keys JSONB column + GIN(jsonb_path_ops) index to batch_jobs.

The column stores the sorted, deduped list of NoteInput.calculate_idempotency_key_from_dto
values for the incoming notes of a batch job, so JobManager.create_job can detect
overlap with concurrent pending/processing jobs and return HTTP 409 instead of
spawning a duplicate.

Default literal `'[]'::jsonb` is a non-volatile constant; on Postgres >= 11 the
ALTER is metadata-only (the default is recorded in pg_attribute and existing
rows transparently report `[]` until updated). The upgrade asserts PG >= 11
*before* the ALTER — on PG 10 the literal-default optimisation does not exist
and the table would be rewritten under an ACCESS EXCLUSIVE lock.

The index uses `jsonb_path_ops`: smaller and faster than `jsonb_ops` for `@>`
containment queries (the only access pattern used by the overlap check).

Revision ID: 021_batch_jobs_input_note_keys
Revises: 020_temporal_cooccurrences
Create Date: 2026-04-25
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = '021_batch_jobs_input_note_keys'
down_revision: Union[str, None] = '020_temporal_cooccurrences'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(conn, table: str, column: str) -> bool:
    result = conn.execute(
        sa.text(
            'SELECT EXISTS (SELECT 1 FROM information_schema.columns '
            'WHERE table_name = :table AND column_name = :column)'
        ),
        {'table': table, 'column': column},
    )
    return bool(result.scalar())


def _index_exists(conn, index: str) -> bool:
    result = conn.execute(
        sa.text('SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = :index)'),
        {'index': index},
    )
    return bool(result.scalar())


def _assert_pg_version_at_least(conn, major: int) -> None:
    """Fail loudly on Postgres < major.x — the literal-default ALTER would lock the table.

    `server_version_num` is an integer like `110000` for 11.0; integer comparison
    is unambiguous against any patch level. Supported back to PG 8.1.
    """
    result = conn.execute(sa.text('SHOW server_version_num'))
    version_num = int(result.scalar() or 0)
    if version_num < major * 10000:
        raise RuntimeError(
            f'batch_jobs.input_note_keys migration requires Postgres >= {major}.x '
            f'(observed server_version_num={version_num}). On older versions the '
            f'literal-default ALTER TABLE rewrites the entire table.'
        )


def upgrade() -> None:
    conn = op.get_bind()

    _assert_pg_version_at_least(conn, 11)

    if not _column_exists(conn, 'batch_jobs', 'input_note_keys'):
        op.add_column(
            'batch_jobs',
            sa.Column(
                'input_note_keys',
                JSONB(),
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
        )

    if not _index_exists(conn, 'idx_batch_jobs_input_note_keys'):
        op.execute(
            'CREATE INDEX idx_batch_jobs_input_note_keys '
            'ON batch_jobs USING gin (input_note_keys jsonb_path_ops)'
        )


def downgrade() -> None:
    conn = op.get_bind()

    if _index_exists(conn, 'idx_batch_jobs_input_note_keys'):
        op.drop_index('idx_batch_jobs_input_note_keys', table_name='batch_jobs')

    if _column_exists(conn, 'batch_jobs', 'input_note_keys'):
        op.drop_column('batch_jobs', 'input_note_keys')
