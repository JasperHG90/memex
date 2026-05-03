"""F22 — confidence_evidence_count column on memory_units (Two-Factor edge confidence).

Adds the negative-evidence event count companion to ``memory_units.confidence``
so the closed-form Beta(1, 1) posterior at
``memex_core.memory.confidence.mean_and_variance`` can derive variance from
``(confidence, confidence_evidence_count)`` without storing variance separately.

Backfill semantics — symmetric with the forward path
====================================================

Backfill counts incoming ``MemoryLink`` rows of type ``contradicts`` /
``weakens`` only — ``reinforces`` is intentionally excluded. This mirrors the
forward path at ``packages/core/src/memex_core/memory/contradiction/engine.py``
(the existing ``α=0.1`` weaken / ``2α=0.2`` contradict steps), which is the
*only* place ``confidence`` itself is adjusted today. ``reinforces`` events
do NOT step the confidence mean, so coupling the count to those same two
events keeps ``confidence_evidence_count`` semantically aligned with the
``confidence`` mean — both track negative-evidence accumulation.

Including ``reinforces`` in the backfill would give pre-existing units a
*higher* evidence count than equivalent post-F22 units that had only
experienced reinforcement events (same age, same link profile, different
certainty). The known-v1 limitation here is that a well-reinforced unit
(say 20 ``reinforces``, 0 ``contradicts``/``weakens``) post-backfill has
``confidence_evidence_count = 0``, variance = 1/12, certainty = 0 — i.e.
characterised as high-variance even though it is well-supported. This is a
deliberate v1 trade-off, paired with the
``CONFIDENCE_VARIANCE_OBSERVED`` metric (segmented by ``reinforces``-link-count
category) so operators can decide whether to extend the symmetry to
reinforces in a follow-up.

Ship-time guard
===============

The column ships INERTLY: ``confidence_evidence_count`` is populated by the
backfill, but the F47 reranker boost stays at the existing
``1.0 + α × (confidence − 0.5)`` form until ``RetrievalConfig.certainty_modulation_enabled``
is flipped to ``True``. The migration alone is therefore zero-behaviour-change.

Revision ID: 033_confidence_evidence_count
Revises: 032_fsfm_decay_columns
Create Date: 2026-05-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '033_confidence_evidence_count'
down_revision: str | None = '032_fsfm_decay_columns'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _column_exists(conn, table: str, column: str) -> bool:
    result = conn.execute(
        sa.text(
            'SELECT EXISTS ('
            '  SELECT 1 FROM information_schema.columns'
            '  WHERE table_schema = current_schema()'
            '    AND table_name = :table AND column_name = :column'
            ')'
        ),
        {'table': table, 'column': column},
    )
    return bool(result.scalar())


def upgrade() -> None:
    conn = op.get_bind()

    if not _column_exists(conn, 'memory_units', 'confidence_evidence_count'):
        op.add_column(
            'memory_units',
            sa.Column(
                'confidence_evidence_count',
                sa.Integer(),
                nullable=False,
                server_default='0',
            ),
        )

    op.execute(
        sa.text(
            'UPDATE memory_units mu '
            'SET confidence_evidence_count = sub.cnt '
            'FROM ( '
            '    SELECT to_unit_id AS unit_id, COUNT(*) AS cnt '
            '    FROM memory_links '
            "    WHERE link_type IN ('contradicts', 'weakens') "
            '    GROUP BY to_unit_id '
            ') sub '
            'WHERE mu.id = sub.unit_id '
            '  AND mu.confidence_evidence_count = 0'
        )
    )


def downgrade() -> None:
    conn = op.get_bind()

    if _column_exists(conn, 'memory_units', 'confidence_evidence_count'):
        op.drop_column('memory_units', 'confidence_evidence_count')
