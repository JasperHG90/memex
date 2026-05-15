"""Blocking GIN index on ``mental_models.observations`` for V21 JSONB scans.

The ``_flip_deprioritized`` and ``flush_deferred_observation_refresh`` paths
need to scan ``mental_models.observations`` JSONB for observations citing a
specific memory unit (the deprio'd MU) and for observations whose ``id``
matches an incoming unit_id (the read-only-observation 400 path). Both
predicates use the ``jsonb_path_ops`` operator class with ``@>`` containment,
which GIN supports directly.

This migration uses a **blocking** ``CREATE INDEX`` (no ``CONCURRENTLY``).
The reasoning, per the V21 design memo:

  * Alembic's env.py wraps every revision in an implicit transaction; per-
    revision ``transactional_ddl = False`` is not a clean escape and there
    is no precedent ``CREATE INDEX CONCURRENTLY`` migration in this
    codebase to copy.
  * ``mental_models`` is small (one row per ``(entity_id, vault_id)``); the
    GIN build typically completes sub-second on table sizes seen in eval
    and staging.
  * **Pre-flight measurement**: before deploy, run
    ``SELECT count(*), avg(jsonb_array_length(observations))
    FROM mental_models;``. The build cost scales with
    ``rows × avg_observations × avg_evidence_per_observation``. For a
    total key count ≤ 100K, expect sub-second; if ``count(*) > 50K`` or
    the total key count > 500K, budget 5–30s and schedule a maintenance
    window.
  * **Lock type**: a non-concurrent ``CREATE INDEX`` on ``mental_models``
    acquires a ``ShareLock`` on the table — concurrent reads continue,
    but writes (INSERT/UPDATE/DELETE on ``mental_models``, including
    Phase 5 reflection commits) block until the build completes. Pause
    or rate-limit the Hermes write path during the maintenance window
    above; reflection workers will queue their CAS commits and drain
    once the index lands.

If a future deployment needs zero-downtime, ship a follow-up migration
that re-creates the index via ``CREATE INDEX CONCURRENTLY`` outside of
alembic's transaction wrapper.

Revision ID: 044_mm_observations_gin_index
Revises: 043_reflection_queue_refresh_task
Create Date: 2026-05-15
"""

from typing import Sequence, Union

from alembic import op


revision: str = '044_mm_observations_gin_index'
down_revision: Union[str, None] = '043_reflection_queue_refresh_task'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        'CREATE INDEX IF NOT EXISTS idx_mental_models_observations_gin '
        'ON mental_models USING GIN (observations jsonb_path_ops)'
    )


def downgrade() -> None:
    op.execute('DROP INDEX IF EXISTS idx_mental_models_observations_gin')
