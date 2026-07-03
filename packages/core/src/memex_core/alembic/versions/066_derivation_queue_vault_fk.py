"""Add the missing vault_id FK on ``procedural_derivation_queue``.

The ORM model declares ``vault_id`` via ``vault_id_field()`` →
``ForeignKey('vaults.id', ondelete='CASCADE')``, but migration 061 created
the column as a bare ``UUID NOT NULL`` with no FK. So an Alembic-built
database had no referential integrity / cascade on the queue's ``vault_id``
while a ``create_all``-built (test/eval) database did — deleting a vault
orphaned queue rows under Alembic but cascaded in CI, a divergence CI could
never catch. This migration closes the gap so both construction paths agree.

Orphan queue rows (vault already gone) are purged first so the constraint
add cannot fail — derivation-queue rows are transient work items, safe to drop.

Revision ID: 066_derivation_queue_vault_fk
Revises: 065_procedural_outcomes
Create Date: 2026-06-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '066_derivation_queue_vault_fk'
down_revision: str | None = '065_procedural_outcomes'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = 'procedural_derivation_queue'
_FK = 'fk_procedural_derivation_queue_vault_id'


def upgrade() -> None:
    # Drop rows whose vault no longer exists so the FK add succeeds.
    op.execute(
        sa.text(
            f'DELETE FROM {_TABLE} q '
            'WHERE NOT EXISTS (SELECT 1 FROM vaults v WHERE v.id = q.vault_id)'
        )
    )
    op.create_foreign_key(
        _FK,
        _TABLE,
        'vaults',
        ['vault_id'],
        ['id'],
        ondelete='CASCADE',
    )


def downgrade() -> None:
    op.drop_constraint(_FK, _TABLE, type_='foreignkey')
