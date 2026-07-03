"""Inbox router schema: per-vault GaussianNB anchors, note cache, online stats.

Creates the tables and views backing the inbox router (see
``services/inbox_router/``):

- ``inbox_router_nb_stats`` / ``inbox_router_nb_class_counts`` — sufficient
  statistics ``(n, Σx, Σx²)`` per (feature, label) and class counts. Online
  Bayesian conjugate updates land here; the model is "fit" purely in SQL.
- ``inbox_router_nb_params`` / ``inbox_router_nb_prior`` — VIEWs deriving
  ``(μ̂, σ̂²)`` and log-priors from the sufficient stats inline.
- ``inbox_router_vault_anchors`` — per-vault chunk/summary/mental-model
  centroids, tsvector doc, and top-K entity ids; refreshed each triage tick.
- ``inbox_router_note_cache`` — per-note features (centroid, tsquery, entity
  ids) populated on inbox-note ingest.

The stats table is SEEDED from the POC's measured per-feature (μ, σ²) at a
weak weight (n=5) so the router produces sensible rankings from the first
tick rather than the uniform-random output a flat prior would give. Online
updates (with EWMA decay) evolve it toward the live corpus.

Also extends the ``maintenance_proposals`` lint_type CHECK to allow
``'routing'`` (the router emits ``inbox_vault_route`` / ``inbox_vault_no_fit``
proposals).

Revision ID: 055_inbox_router
Revises: 054_nodes_vault_active
Create Date: 2026-05-29
"""

from alembic import op

revision: str = '055_inbox_router'
down_revision: str | None = '054_nodes_vault_active'
branch_labels: str | list[str] | None = None
depends_on: str | list[str] | None = None


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

-- (μ̂, σ̂²) derived from sufficient stats. Bessel-corrected sample variance
-- with a floor for numerical safety (avoids a degenerate zero-variance
-- Gaussian when only the seed prior is present).
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

# Seed sufficient stats from POC v12 empirics at n=5 (weak weight). Derived via
# sum_x = n*μ, sum_x_sq = σ²*(n-1) + n*μ². Without this, a flat prior yields a
# near-delta Gaussian likelihood that saturates the logits and produces
# uniform-random rankings until ~50 user decisions accumulate.
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

# Class prior ~1:6 (match:no_match) — the POC's measured base rate (P(match)≈0.14).
_SEED_CLASS = """
INSERT INTO inbox_router_nb_class_counts (label, n) VALUES (1, 1.0), (0, 6.0);
"""

_DROP = """
DROP TABLE IF EXISTS inbox_router_note_cache;
DROP TABLE IF EXISTS inbox_router_vault_anchors;
DROP VIEW IF EXISTS inbox_router_nb_prior;
DROP VIEW IF EXISTS inbox_router_nb_params;
DROP TABLE IF EXISTS inbox_router_nb_class_counts;
DROP TABLE IF EXISTS inbox_router_nb_stats;
"""

_CHECK_WITH_ROUTING = (
    'ALTER TABLE maintenance_proposals '
    'DROP CONSTRAINT IF EXISTS ck_maintenance_proposals_lint_type; '
    'ALTER TABLE maintenance_proposals '
    'ADD CONSTRAINT ck_maintenance_proposals_lint_type '
    "CHECK (lint_type IN ('structural', 'quality', 'governance', 'schema', 'routing'));"
)

_CHECK_WITHOUT_ROUTING = (
    'ALTER TABLE maintenance_proposals '
    'DROP CONSTRAINT IF EXISTS ck_maintenance_proposals_lint_type; '
    'ALTER TABLE maintenance_proposals '
    'ADD CONSTRAINT ck_maintenance_proposals_lint_type '
    "CHECK (lint_type IN ('structural', 'quality', 'governance', 'schema'));"
)


def _exec_each(sql: str) -> None:
    """Execute a ``;``-separated SQL block one statement at a time.

    The metastore's async driver (asyncpg) sends every statement over the
    extended query protocol as a prepared statement, and Postgres permits only
    one command per prepared statement — a multi-statement string raises
    ``cannot insert multiple commands into a prepared statement``. (psycopg2's
    simple-query path tolerated ``;``-joined batches; asyncpg does not.) So we
    split and run each statement individually. Relies on no statement containing
    a ``;`` inside a string literal or body, which holds for every block here.
    """
    for statement in (s.strip() for s in sql.split(';')):
        if statement:
            op.execute(statement)


def upgrade() -> None:
    _exec_each(_CHECK_WITH_ROUTING)
    _exec_each(_CREATE)
    _exec_each(_SEED_STATS)
    _exec_each(_SEED_CLASS)


def downgrade() -> None:
    _exec_each(_DROP)
    # Revert the CHECK; any 'routing' rows must be cleared first to satisfy it.
    op.execute("DELETE FROM maintenance_proposals WHERE lint_type = 'routing'")
    _exec_each(_CHECK_WITHOUT_ROUTING)
