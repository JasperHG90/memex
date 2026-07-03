"""Merge the two Alembic heads into one.

Revision 045 (``045_drop_procedure_outcomes``) has two children, so the DAG
forks into two heads:

- ``046_procedure_to_global`` — procedure-namespace migration that arrived via
  ``release/tier-a-cognitive-memory``.
- ``046_mental_models_archived_at`` → ``047`` … → ``052`` — the cockpit / lint
  auto-learning chain plus the entity-cooccurrence per-vault PK fix (052).

Both legitimately branch off 045. This revision unifies them so the project has
a single head again. It makes NO schema change — it only adds the merge edge to
the migration graph.

Revision ID: 053_merge_heads
Revises: 046_procedure_to_global, 052_entity_cooccurrence_vault_pk
Create Date: 2026-05-29
"""

revision: str = '053_merge_heads'
down_revision: tuple[str, ...] = (
    '046_procedure_to_global',
    '052_entity_cooccurrence_vault_pk',
)
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
