"""Drop the inbox-router schema (4 tables + 2 views + GIN index).

DESTRUCTIVE, user-approved. The in-core GaussianNB inbox router was removed in
V6; routing now lives in the external triage-inbox skill, which submits
``lint_type='routing'`` proposals through the lint ingress. With no in-core
emitter left, the router's tables and derived views are dropped.

What this migration deliberately does NOT do:

- It does NOT touch ``ck_maintenance_proposals_lint_type`` in EITHER direction.
  ``'routing'`` stays a legal ``lint_type`` on upgrade AND downgrade because the
  triage-inbox skill still emits routing proposals. (Migration 055 first widened
  that CHECK; nothing here narrows it.)
- It does NOT read or delete any ``maintenance_proposals`` rows. Historical
  routing findings stay resolvable via ``route_note_to_vault``. This differs from
  055's own downgrade, which deletes ``lint_type='routing'`` rows before
  re-narrowing the CHECK — 059 must not replicate that.

Downgrade recreates the tables, views, and POC seed by replaying 055's
``_CREATE`` / ``_SEED_STATS`` / ``_SEED_CLASS`` blocks (``_SEED_*`` byte-identical;
``_CREATE`` is DDL-equivalent with 055's inline SQL comments omitted) so this
migration stays self-contained; 055 is historical and will not change.

Revision ID: 059_drop_inbox_router
Revises: 058_vault_summary_embedding
Create Date: 2026-06-07
"""

from alembic import op

revision: str = '059_drop_inbox_router'
down_revision: str | None = '058_vault_summary_embedding'
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


# FK-safe drop order (child tables first, then views, then stats tables).
# Copied verbatim from 055's ``_DROP``. The GIN index on tsvector_doc drops with
# its table.
_DROP = """
DROP TABLE IF EXISTS inbox_router_note_cache;
DROP TABLE IF EXISTS inbox_router_vault_anchors;
DROP VIEW IF EXISTS inbox_router_nb_prior;
DROP VIEW IF EXISTS inbox_router_nb_params;
DROP TABLE IF EXISTS inbox_router_nb_class_counts;
DROP TABLE IF EXISTS inbox_router_nb_stats;
"""

# Verbatim copies of 055's creation blocks, used only by downgrade() to restore
# the schema. Kept in sync with 055 by being identical; 055 is frozen history.
_CREATE = """
CREATE TABLE inbox_router_nb_stats (
    feature_name TEXT             NOT NULL,
    label        SMALLINT         NOT NULL,
    n            DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    sum_x        DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    sum_x_sq     DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    updated_at   TIMESTAMPTZ      NOT NULL DEFAULT now(),
    PRIMARY KEY (feature_name, label)
);

CREATE TABLE inbox_router_nb_class_counts (
    label      SMALLINT         PRIMARY KEY,
    n          DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    updated_at TIMESTAMPTZ      NOT NULL DEFAULT now()
);

CREATE VIEW inbox_router_nb_params AS
SELECT feature_name, label,
       sum_x / NULLIF(n, 0) AS mu,
       GREATEST(
           (sum_x_sq - sum_x * sum_x / NULLIF(n, 0)) / NULLIF(GREATEST(n - 1, 1e-6), 0),
           1e-9
       ) AS sigma_sq
FROM inbox_router_nb_stats;

CREATE VIEW inbox_router_nb_prior AS
SELECT label,
       ln(GREATEST(n / NULLIF((SELECT SUM(n) FROM inbox_router_nb_class_counts), 0),
                   1e-12)) AS log_prior
FROM inbox_router_nb_class_counts;

CREATE TABLE inbox_router_vault_anchors (
    vault_id          UUID PRIMARY KEY REFERENCES vaults(id) ON DELETE CASCADE,
    chunk_centroid    vector(384) NOT NULL,
    summary_embedding vector(384),
    mm_centroid       vector(384),
    tsvector_doc      TSVECTOR    NOT NULL DEFAULT ''::tsvector,
    entity_ids        UUID[]      NOT NULL DEFAULT '{}'::uuid[],
    n_notes           INTEGER     NOT NULL DEFAULT 0,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE inbox_router_note_cache (
    note_id        UUID PRIMARY KEY REFERENCES notes(id) ON DELETE CASCADE,
    chunk_centroid vector(384),
    tsq            TSQUERY,
    entity_ids     UUID[]      NOT NULL DEFAULT '{}'::uuid[],
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_inbox_router_va_tsv
    ON inbox_router_vault_anchors USING gin(tsvector_doc);
"""

_SEED_STATS = """
INSERT INTO inbox_router_nb_stats (feature_name, label, n, sum_x, sum_x_sq) VALUES
    ('sem_summary_sim',  0, 5.0, 1.159000, 0.319136),
    ('sem_summary_sim',  1, 5.0, 1.873000, 0.748826),
    ('sem_centroid_sim', 0, 5.0, 1.693000, 0.635090),
    ('sem_centroid_sim', 1, 5.0, 3.278500, 2.256592),
    ('mm_centroid_sim',  0, 5.0, 1.358500, 0.464984),
    ('mm_centroid_sim',  1, 5.0, 1.970500, 0.979054),
    ('entity_jaccard',   0, 5.0, 0.054000, 0.001223),
    ('entity_jaccard',   1, 5.0, 0.307000, 0.024650),
    ('keyword_ts_rank',  0, 5.0, 4.737500, 4.500701),
    ('keyword_ts_rank',  1, 5.0, 4.919000, 4.839872);
"""

_SEED_CLASS = """
INSERT INTO inbox_router_nb_class_counts (label, n) VALUES (1, 1.0), (0, 6.0);
"""


def _exec_each(sql: str) -> None:
    """Execute a ``;``-separated SQL block one statement at a time.

    asyncpg sends each statement as a prepared statement and Postgres permits
    only one command per prepared statement, so a multi-statement string would
    raise ``cannot insert multiple commands into a prepared statement``. Split
    and run individually. (Copied from 055.)
    """
    for statement in (s.strip() for s in sql.split(';')):
        if statement:
            op.execute(statement)


def upgrade() -> None:
    # Drop the router schema only. The lint_type CHECK and all maintenance_proposals
    # rows (including historical 'routing' findings) are left untouched.
    _exec_each(_DROP)


def downgrade() -> None:
    # Recreate the schema + POC seed. Do NOT touch the CHECK (routing already
    # legal) and do NOT delete any maintenance_proposals rows.
    _exec_each(_CREATE)
    _exec_each(_SEED_STATS)
    _exec_each(_SEED_CLASS)
