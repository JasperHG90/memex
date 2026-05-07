"""Backfill semantic_contradiction maintenance_proposals from existing contradicts links.

The contradiction engine started writing one ``semantic_contradiction``
``maintenance_proposals`` row per ``contradicts`` link in this branch.
Pre-existing links (created before this code was deployed) had no
corresponding finding, so ``memex lint findings`` only surfaces
contradictions detected after the deploy. This migration backfills
findings for every existing ``contradicts`` link.

Idempotency: the INSERT uses ``ON CONFLICT DO NOTHING`` against the
existing partial unique index from migration 025
(``rule_name, target_type, target_id, vault_id) WHERE status = 'pending'``),
so re-running this migration after a partial backfill is a no-op.

Dedup: ``SELECT DISTINCT ON (vault_id, to_unit_id)`` collapses multiple
``contradicts`` links targeting the same superseded unit into one row.
Postgres would otherwise reject the second duplicate row inside a single
INSERT with ``cardinality_violation``.

Sanitisation: the ``reasoning`` and ``superseding_note_title`` text fields
are stripped of ASCII C0/C1 control chars (other than ``\\n`` / ``\\t``)
and capped to the same 1000 / 200 char limits the runtime engine enforces.

The ``backfilled: true`` flag in the resulting evidence JSONB lets
operators distinguish migration-emitted rows from runtime-emitted rows
(useful for audits but not for filtering — the lint UI treats both the
same).

Downgrade: no-op. The backfilled rows are indistinguishable from runtime
emissions at the schema level except via the ``backfilled`` flag, and a
naïve DELETE would remove resolved/dismissed transitions operators may
have made. Operators rolling back must clean up manually if needed.

Revision ID: 035_backfill_findings
Revises: 034_add_mw_mode
Create Date: 2026-05-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '035_backfill_findings'
down_revision: str | None = '034_add_mw_mode'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# SELECT DISTINCT ON collapses multiple ``contradicts`` links that share the
# same (vault_id, to_unit_id) into one finding row — Postgres rejects
# duplicate target_ids inside one ``ON CONFLICT DO NOTHING`` statement
# (cardinality_violation), so the runtime engine pre-dedupes too. The
# ORDER BY makes the chosen row deterministic across migration re-runs.
#
# regexp_replace strips ASCII C0 controls (0x00-0x1F except \n, \t),
# DEL (0x7F), and the C1 control range (0x80-0x9F) so the backfilled
# evidence matches the runtime sanitisation contract enforced by
# _sanitise_evidence_text() in the contradiction engine.
_CONTROL_CHAR_REGEX = r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F]'

_BACKFILL_SQL = sa.text(f"""
    INSERT INTO maintenance_proposals (
        vault_id, lint_type, target_type, target_id,
        rule_name, evidence, suggested_action, status, source
    )
    SELECT
        deduped.vault_id,
        'quality',
        'memory_unit',
        deduped.target_id,
        'semantic_contradiction',
        jsonb_build_object(
            'authoritative_unit_id',
                COALESCE(deduped.authoritative_unit_id, deduped.from_unit_id),
            'superseded_unit_id', deduped.target_id,
            'reasoning',
                LEFT(regexp_replace(COALESCE(deduped.reasoning, ''),
                                    '{_CONTROL_CHAR_REGEX}', '', 'g'),
                     1000),
            'superseding_note_title',
                LEFT(regexp_replace(COALESCE(deduped.title, ''),
                                    '{_CONTROL_CHAR_REGEX}', '', 'g'),
                     200),
            'backfilled', true
        ),
        'Review the contradiction and decide whether the superseded unit should be revised or removed.',
        'pending',
        'llm'
    FROM (
        SELECT DISTINCT ON (ml.vault_id, ml.to_unit_id)
            ml.vault_id AS vault_id,
            ml.to_unit_id::text AS target_id,
            ml.from_unit_id::text AS from_unit_id,
            ml.link_metadata ->> 'authoritative_unit_id' AS authoritative_unit_id,
            ml.link_metadata ->> 'reasoning' AS reasoning,
            ml.link_metadata ->> 'superseding_note_title' AS title
        FROM memory_links ml
        WHERE ml.link_type = 'contradicts'
        ORDER BY ml.vault_id, ml.to_unit_id, ml.from_unit_id
    ) AS deduped
    ON CONFLICT (rule_name, target_type, target_id, vault_id)
    WHERE status = 'pending'
    DO NOTHING
""")


def upgrade() -> None:
    op.get_bind().execute(_BACKFILL_SQL)


def downgrade() -> None:
    # The backfill is purely additive and shares the partial unique index
    # with run-time emissions, so identifying which rows came from the
    # backfill vs the engine is impossible without an extra column. Mark
    # this migration as one-way: downgrade is a no-op.
    pass
