"""F22 — confidence_evidence_count column on memory_units (Two-Factor edge confidence).

Adds the negative-evidence event count companion to ``memory_units.confidence``
so the closed-form Beta(1, 1) posterior at
``memex_core.memory.confidence.mean_and_variance`` can derive variance from
``(confidence, confidence_evidence_count)`` without storing variance separately.

Operational requirement (Hermes round-16 MED)
=============================================

This migration MUST run with the contradiction engine paused
(``server.memory.contradiction.enabled = False`` or the app process stopped).
The chunked backfill filters on ``mu.confidence_evidence_count = 0`` to skip
already-backfilled rows; if the forward path bumps a candidate from 0 → 1
between iterations, that unit's pre-existing link count is silently lost
and ``confidence_evidence_count`` permanently undercount (it lands at 1 —
just the new event — rather than the correct ``backfilled_count + 1``).
The undercount is *conservative* (the unit reads as higher-variance than
warranted), the window is narrow (one migration runtime), and the
forward path itself remains correct — but the column does not
self-correct on subsequent F22 reads.

Standard Memex deploy procedure (migrations run during the deploy
window with the application paused) avoids this race entirely. If a
hot-online migration is unavoidable, run a post-migration
verification UPDATE that compares ``confidence_evidence_count`` to
``COUNT(*)`` over the link table for affected units.

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

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

logger = logging.getLogger('alembic.runtime.migration')

revision: str = '033_confidence_evidence_count'
down_revision: str | None = '032_fsfm_decay_columns'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CHECK_CONSTRAINT_NAME = 'memory_units_confidence_evidence_count_check'

# Hermes round-11 MED: composite index on ``(link_type, to_unit_id)`` so the
# backfill subquery and any future "negative-evidence count for this unit"
# query can range-scan instead of seq-scanning ``memory_links`` on every
# batch. The existing ``idx_memory_links_to`` and ``idx_memory_links_type``
# are single-column and don't combine for this access pattern.
_BACKFILL_INDEX_NAME = 'idx_memory_links_link_type_to_unit'

# Hermes round-10 LOW: chunk the backfill so a vault with millions of
# memory_units doesn't hold a long-running row-level lock on the entire
# table during the single-shot ``UPDATE … FROM (subquery)``. 5000 strikes
# the standard balance between per-statement overhead and lock duration —
# matches typical pgvector / memex backfill batch sizes.
_BACKFILL_BATCH_SIZE = 5000


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


def _constraint_exists(conn, name: str) -> bool:
    result = conn.execute(
        sa.text(
            'SELECT EXISTS ('
            '  SELECT 1 FROM information_schema.table_constraints'
            '  WHERE table_schema = current_schema()'
            '    AND constraint_name = :name'
            ')'
        ),
        {'name': name},
    )
    return bool(result.scalar())


def _index_exists(conn, name: str) -> bool:
    # Hermes round-17 LOW: ``pg_indexes`` is queried here while the
    # column / constraint helpers above use ``information_schema``.
    # ``information_schema`` lacks a portable view on Postgres indexes
    # (its ``statistics`` view is MySQL-shaped; Postgres exposes index
    # metadata only via ``pg_indexes`` / ``pg_class``). The inconsistency
    # is intentional, not a refactor opportunity.
    result = conn.execute(
        sa.text(
            'SELECT EXISTS ('
            '  SELECT 1 FROM pg_indexes'
            '  WHERE schemaname = current_schema()'
            '    AND indexname = :name'
            ')'
        ),
        {'name': name},
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

    # Composite index for the backfill access pattern (Hermes round-11
    # MED). Created BEFORE the loop so each batch range-scans instead of
    # seq-scanning ``memory_links``.
    if not _index_exists(conn, _BACKFILL_INDEX_NAME):
        op.create_index(
            _BACKFILL_INDEX_NAME,
            'memory_links',
            ['link_type', 'to_unit_id'],
        )

    # Chunked backfill (Hermes round-10 LOW). Each batch processes at most
    # ``_BACKFILL_BATCH_SIZE`` rows so the per-statement work — and therefore
    # the per-statement lock-acquisition window — is bounded regardless of
    # the total candidate count in the vault. The inner SELECT picks the
    # next ``_BACKFILL_BATCH_SIZE`` actual candidates (units that both have
    # incoming contradicts/weakens links AND still carry the default 0),
    # so the outer UPDATE always touches exactly the rows we just chose.
    # The loop terminates when a batch updates 0 rows.
    #
    # Note (Hermes round-13 LOW): the prior implementation wrapped each
    # batch in ``op.get_context().autocommit_block()`` so locks would be
    # released between batches. That broke against the project's env.py,
    # which runs migrations inside the implicit ``connect()`` transaction
    # via ``connection.run_sync(do_run_migrations)`` — alembic's
    # ``begin_transaction()`` returns a SAVEPOINT in that mode, which
    # ``autocommit_block`` cannot commit/re-open. Removed; row locks
    # are now held until the migration commits, which is the standard
    # transactional-DDL convention every other migration in this project
    # follows. The chunked LIMIT still bounds per-statement work.
    # The inner ``JOIN memory_units mu_inner ... AND
    # mu_inner.confidence_evidence_count = 0`` filter is REQUIRED for
    # loop progress, not a redundant idempotency check (Hermes round-14
    # MED — flagged as removable; verified-incorrect): the inner SELECT
    # picks the next ``_BACKFILL_BATCH_SIZE`` unit_ids ordered by
    # ``to_unit_id``, and without the inner filter every iteration would
    # re-pick the SAME first ``batch_size`` unit_ids. The outer
    # ``mu.confidence_evidence_count = 0`` would then reject all updates
    # on the second iteration, ``rowcount=0`` would terminate the loop,
    # and only the first batch would ever land. The inner filter
    # advances the candidate window through the link target set as each
    # batch flips ``confidence_evidence_count`` non-zero.
    backfill_sql = sa.text(
        'UPDATE memory_units mu '
        'SET confidence_evidence_count = sub.cnt '
        'FROM ( '
        '    SELECT ml.to_unit_id AS unit_id, COUNT(*) AS cnt '
        '    FROM memory_links ml '
        '    JOIN memory_units mu_inner ON mu_inner.id = ml.to_unit_id '
        "    WHERE ml.link_type IN ('contradicts', 'weakens') "
        '      AND mu_inner.confidence_evidence_count = 0 '
        '    GROUP BY ml.to_unit_id '
        # ORDER BY (Hermes round-12 MED) makes batch selection deterministic
        # so partial-progress logging is reproducible. The composite index
        # added above already orders on ``(link_type, to_unit_id)`` so the
        # ORDER BY is index-served — no extra sort cost.
        '    ORDER BY ml.to_unit_id '
        '    LIMIT :batch_size '
        ') sub '
        'WHERE mu.id = sub.unit_id '
        '  AND mu.confidence_evidence_count = 0'
    )
    # Hermes round-16 LOW: per-batch progress logging so operators
    # running the migration on a vault with millions of qualifying
    # units can see forward progress instead of staring at a hung
    # process. The cumulative total is also logged on the terminal
    # (zero-rowcount) iteration so the final tally is unambiguous.
    backfilled_total = 0
    batch_index = 0
    while True:
        result = conn.execute(backfill_sql, {'batch_size': _BACKFILL_BATCH_SIZE})
        if not result.rowcount:
            logger.info(
                'F22 backfill complete: %d batches, %d units backfilled total.',
                batch_index,
                backfilled_total,
            )
            break
        batch_index += 1
        backfilled_total += result.rowcount
        logger.info(
            'F22 backfill batch %d: updated %d units (%d total).',
            batch_index,
            result.rowcount,
            backfilled_total,
        )

    # Hermes round-6 MED: production runs migrations only (not
    # ``Base.metadata.create_all``), so the SQLModel-level
    # ``CheckConstraint('confidence_evidence_count >= 0', ...)`` would
    # otherwise be missing in the live DB. Idempotent — guarded by the
    # information_schema lookup so re-running this migration is a no-op.
    if not _constraint_exists(conn, _CHECK_CONSTRAINT_NAME):
        op.create_check_constraint(
            _CHECK_CONSTRAINT_NAME,
            'memory_units',
            'confidence_evidence_count >= 0',
        )


def downgrade() -> None:
    conn = op.get_bind()

    if _constraint_exists(conn, _CHECK_CONSTRAINT_NAME):
        op.drop_constraint(_CHECK_CONSTRAINT_NAME, 'memory_units', type_='check')

    if _index_exists(conn, _BACKFILL_INDEX_NAME):
        op.drop_index(_BACKFILL_INDEX_NAME, table_name='memory_links')

    if _column_exists(conn, 'memory_units', 'confidence_evidence_count'):
        op.drop_column('memory_units', 'confidence_evidence_count')
