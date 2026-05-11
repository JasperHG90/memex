"""Outcome per-unit schema: add `unused_co_count` columns + `outcome_audit_log`.

Adds an engagement metric counter (`unused_co_count`) to the three Memory
Worth tables (`memory_units`, `unit_entities`, `mental_models`) and creates
the `outcome_audit_log` table that records one row per `record_outcome`
call. The engagement counter is bumped when a unit was retrieved but the
caller marked it as `not_used` (caller verb taxonomy: `helpful` /
`not_helpful` / `not_used`).

Additive only. New columns use a NOT NULL server default of 0 so existing
rows backfill implicitly; the new table is empty at upgrade time. No data
migration required.

Revision ID: 037_outcome_per_unit_schema
Revises: 036_fsfm_cooldown_index
Create Date: 2026-05-11
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB


revision: str = '037_outcome_per_unit_schema'
down_revision: Union[str, None] = '036_fsfm_cooldown_index'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_AUDIT_TABLE = 'outcome_audit_log'
_AUDIT_VAULT_TS_INDEX = 'idx_outcome_audit_log_vault_ts'
_AUDIT_CALLER_INDEX = 'idx_outcome_audit_log_caller'
_AUDIT_UNITS_IS_ARRAY = 'outcome_audit_log_units_is_array'


def upgrade() -> None:
    # 1. unused_co_count on the three Memory Worth counter tables.
    for table in ('memory_units', 'unit_entities', 'mental_models'):
        op.add_column(
            table,
            sa.Column(
                'unused_co_count',
                sa.Integer(),
                nullable=False,
                server_default='0',
            ),
        )

    # 2. outcome_audit_log table — one row per record_outcome call.
    op.create_table(
        _AUDIT_TABLE,
        sa.Column(
            'id',
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text('gen_random_uuid()'),
        ),
        sa.Column(
            'vault_id',
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey('vaults.id', ondelete='CASCADE'),
            nullable=False,
        ),
        sa.Column('caller_id', sa.String(length=128), nullable=True),
        sa.Column('units', JSONB(), nullable=False),
        sa.CheckConstraint(
            "jsonb_typeof(units) = 'array'",
            name=_AUDIT_UNITS_IS_ARRAY,
        ),
        sa.Column('turn_outcome', sa.Text(), nullable=True),
        sa.Column('retrieved_set_size', sa.Integer(), nullable=True),
        sa.Column('coverage_ratio', sa.Float(), nullable=True),
        sa.Column(
            'exploration_tagged',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('false'),
        ),
        sa.Column(
            'created_at',
            sa.dialects.postgresql.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        _AUDIT_VAULT_TS_INDEX,
        _AUDIT_TABLE,
        ['vault_id', sa.text('created_at DESC')],
    )
    op.create_index(_AUDIT_CALLER_INDEX, _AUDIT_TABLE, ['caller_id'])


def downgrade() -> None:
    op.drop_index(_AUDIT_CALLER_INDEX, table_name=_AUDIT_TABLE)
    op.drop_index(_AUDIT_VAULT_TS_INDEX, table_name=_AUDIT_TABLE)
    op.drop_table(_AUDIT_TABLE)
    for table in ('memory_units', 'unit_entities', 'mental_models'):
        op.drop_column(table, 'unused_co_count')
