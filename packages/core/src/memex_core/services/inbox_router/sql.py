"""Raw SQL for the inbox router.

All scoring, fitting, and feature computation runs in Postgres. These are the
production counterparts of the POC's parametrised query builders (see
``packages/eval/src/memex_eval/suites/inbox_router/poc/test_perf_v12_online.py``).
Tables are unqualified (public schema); parameters are SQLAlchemy named binds.

Feature set (per (note, vault) pair), matching the model the prior is seeded
for: ``sem_summary_sim``, ``sem_centroid_sim``, ``mm_centroid_sim``,
``entity_jaccard``, ``keyword_ts_rank``.
"""

from __future__ import annotations

# Per-vault anchor refresh. ``summary_embedding`` is computed in Python (the
# narrative must be embedded by the model) and passed in; everything else is
# derived from the DB. Vaults with no chunks produce no row (chunk_centroid is
# NOT NULL) and simply are not scoring candidates until they have content.
REFRESH_VAULT_ANCHOR_SQL = """
INSERT INTO inbox_router_vault_anchors
    (vault_id, chunk_centroid, summary_embedding, mm_centroid,
     tsvector_doc, entity_ids, n_notes, updated_at)
SELECT
    :vault_id ::uuid,
    sub.chunk_centroid,
    CAST(:summary_embedding AS vector),
    (SELECT AVG(mm.embedding)
       FROM mental_models mm
      WHERE mm.vault_id = :vault_id ::uuid
        AND mm.archived_at IS NULL
        AND mm.embedding IS NOT NULL),
    COALESCE(sub.tsvector_doc, ''::tsvector),
    COALESCE((
        SELECT array_agg(entity_id) FROM (
            SELECT ue.entity_id, COUNT(*) AS cnt
              FROM memory_units mu
              JOIN unit_entities ue ON ue.unit_id = mu.id
             WHERE mu.vault_id = :vault_id ::uuid
             GROUP BY ue.entity_id
             ORDER BY cnt DESC
             LIMIT :top_k
        ) t
    ), '{}'::uuid[]),
    sub.n_notes,
    now()
FROM (
    SELECT
        AVG(c.embedding) AS chunk_centroid,
        to_tsvector('english', left(COALESCE(string_agg(c.text, ' '), ''), 1000000))
            AS tsvector_doc,
        COUNT(DISTINCT c.note_id) AS n_notes
      FROM chunks c
      JOIN notes n ON n.id = c.note_id
     WHERE n.vault_id = :vault_id ::uuid
) sub
WHERE sub.chunk_centroid IS NOT NULL
ON CONFLICT (vault_id) DO UPDATE SET
    chunk_centroid    = EXCLUDED.chunk_centroid,
    summary_embedding = EXCLUDED.summary_embedding,
    mm_centroid       = EXCLUDED.mm_centroid,
    tsvector_doc      = EXCLUDED.tsvector_doc,
    entity_ids        = EXCLUDED.entity_ids,
    n_notes           = EXCLUDED.n_notes,
    updated_at        = now()
"""


# Per-note feature cache. tsq = top-50 lexemes of the note's tsvector, OR-joined
# (robust to long notes that AND-semantics plainto_tsquery would zero out).
POPULATE_NOTE_CACHE_SQL = """
INSERT INTO inbox_router_note_cache (note_id, chunk_centroid, tsq, entity_ids, updated_at)
SELECT
    :note_id ::uuid,
    (SELECT AVG(c.embedding) FROM chunks c WHERE c.note_id = :note_id ::uuid),
    (
        SELECT CASE WHEN q.s IS NULL OR q.s = '' THEN NULL::tsquery
                    ELSE to_tsquery('simple', q.s) END
        FROM (
            SELECT array_to_string(array(
                SELECT lex
                  FROM unnest(tsvector_to_array(to_tsvector('english',
                       left(COALESCE(
                           (SELECT string_agg(c.text, ' ')
                              FROM chunks c WHERE c.note_id = :note_id ::uuid),
                           ''), 1000000)))) AS lex
                 WHERE lex ~ '^[a-z][a-z0-9_]{1,}$'
                 LIMIT 50
            ), ' | ') AS s
        ) q
    ),
    COALESCE((
        SELECT array_agg(DISTINCT ue.entity_id)
          FROM memory_units mu
          JOIN unit_entities ue ON ue.unit_id = mu.id
         WHERE mu.note_id = :note_id ::uuid
    ), '{}'::uuid[]),
    now()
ON CONFLICT (note_id) DO UPDATE SET
    chunk_centroid = EXCLUDED.chunk_centroid,
    tsq            = EXCLUDED.tsq,
    entity_ids     = EXCLUDED.entity_ids,
    updated_at     = now()
"""


# Batched pairwise-GaussianNB scoring with per-note softmax + uncertainty.
# Params: :note_ids (uuid[]), :excluded (text[] of vault names to skip).
# Returns one row per (note, candidate vault) ordered by (note_id, p_match DESC).
SCORE_NOTES_SQL = """
WITH note_data AS (
    SELECT note_id, chunk_centroid, tsq, entity_ids
      FROM inbox_router_note_cache
     WHERE note_id = ANY(:note_ids ::uuid[])
       AND chunk_centroid IS NOT NULL
),
candidates AS (
    SELECT nd.note_id, v.id AS vault_id, v.name AS vault_name,
        (1 - (va.summary_embedding <=> nd.chunk_centroid))::float8 AS f_sem_summary_sim,
        (1 - (va.chunk_centroid    <=> nd.chunk_centroid))::float8 AS f_sem_centroid_sim,
        COALESCE((1 - (va.mm_centroid <=> nd.chunk_centroid))::float8, 0.0) AS f_mm_centroid_sim,
        (CASE WHEN cardinality(nd.entity_ids) = 0 AND cardinality(va.entity_ids) = 0 THEN 0.0
              ELSE ((SELECT COUNT(*) FROM (SELECT unnest(nd.entity_ids) INTERSECT
                                             SELECT unnest(va.entity_ids)) i)::float8
                   / NULLIF((SELECT COUNT(*) FROM (SELECT unnest(nd.entity_ids) UNION
                                                    SELECT unnest(va.entity_ids)) u), 0))
         END) AS f_entity_jaccard,
        CASE WHEN nd.tsq IS NULL THEN 0.0
             ELSE COALESCE(ts_rank_cd(va.tsvector_doc, nd.tsq, 32)::float8, 0.0)
        END AS f_keyword_ts_rank
      FROM vaults v
      JOIN inbox_router_vault_anchors va ON va.vault_id = v.id
      CROSS JOIN note_data nd
     WHERE v.name <> ALL(:excluded ::text[])
       AND v.archived_at IS NULL
),
features_long AS (
    SELECT note_id, vault_id, vault_name, 'sem_summary_sim' AS feat,
           COALESCE(f_sem_summary_sim, 0.0) AS val FROM candidates
    UNION ALL SELECT note_id, vault_id, vault_name, 'sem_centroid_sim',
           COALESCE(f_sem_centroid_sim, 0.0) FROM candidates
    UNION ALL SELECT note_id, vault_id, vault_name, 'mm_centroid_sim',
           COALESCE(f_mm_centroid_sim, 0.0)  FROM candidates
    UNION ALL SELECT note_id, vault_id, vault_name, 'entity_jaccard',
           COALESCE(f_entity_jaccard, 0.0)   FROM candidates
    UNION ALL SELECT note_id, vault_id, vault_name, 'keyword_ts_rank',
           COALESCE(f_keyword_ts_rank, 0.0)  FROM candidates
),
per_pair_loglik AS (
    SELECT fl.note_id, fl.vault_id, fl.vault_name, p.label,
        SUM(-0.5 * ln(2 * pi() * p.sigma_sq)
            - power(fl.val - p.mu, 2) / (2 * p.sigma_sq)) AS log_lik
      FROM features_long fl
      JOIN inbox_router_nb_params p ON p.feature_name = fl.feat
     GROUP BY fl.note_id, fl.vault_id, fl.vault_name, p.label
),
param_uncertainty AS (
    SELECT SUM(1.0 / NULLIF(n, 0)) AS u_sum
      FROM inbox_router_nb_stats WHERE label = 1
),
per_pair_posterior AS (
    SELECT note_id, vault_id, vault_name,
        MAX(log_lik) FILTER (WHERE label = 1)
            + (SELECT log_prior FROM inbox_router_nb_prior WHERE label = 1) AS log_post_match,
        MAX(log_lik) FILTER (WHERE label = 0)
            + (SELECT log_prior FROM inbox_router_nb_prior WHERE label = 0) AS log_post_no_match
      FROM per_pair_loglik
     GROUP BY note_id, vault_id, vault_name
),
p_match_raw AS (
    SELECT note_id, vault_id, vault_name,
        1.0 / (1.0 + exp(LEAST(GREATEST(log_post_no_match - log_post_match, -700.0), 700.0)))
            AS p_match_raw,
        ln(GREATEST(
            1.0 / (1.0 + exp(LEAST(GREATEST(log_post_no_match - log_post_match, -700.0), 700.0))),
            1e-12)) AS log_p
      FROM per_pair_posterior
),
note_log_max AS (
    SELECT note_id, vault_id, vault_name, p_match_raw, log_p,
        MAX(log_p) OVER (PARTITION BY note_id) AS log_max FROM p_match_raw
),
note_exp_sum AS (
    SELECT note_id, vault_id, vault_name, p_match_raw,
        exp(log_p - log_max) AS num,
        SUM(exp(log_p - log_max)) OVER (PARTITION BY note_id) AS denom
      FROM note_log_max
)
SELECT
    note_id, vault_id, vault_name,
    (num / NULLIF(denom, 0))::float8 AS p_match,
    p_match_raw,
    sqrt((SELECT u_sum FROM param_uncertainty))::float8 AS param_uncertainty,
    (1.96 * (num / NULLIF(denom, 0)) * (1 - num / NULLIF(denom, 0))
          * sqrt((SELECT u_sum FROM param_uncertainty)))::float8 AS ci_half_width
  FROM note_exp_sum
 ORDER BY note_id, p_match DESC
"""


# Online conjugate update for one (note, vault, label) observation.
# :gamma is the EWMA decay (1.0 = no decay). Updates the 5 feature stats rows
# and the class-count row in a single statement.
ONLINE_UPDATE_SQL = """
WITH new_features AS (
    SELECT
        (1 - (va.summary_embedding <=> nc.chunk_centroid))::float8 AS sem_summary_sim,
        (1 - (va.chunk_centroid    <=> nc.chunk_centroid))::float8 AS sem_centroid_sim,
        COALESCE((1 - (va.mm_centroid <=> nc.chunk_centroid))::float8, 0.0) AS mm_centroid_sim,
        (CASE WHEN cardinality(nc.entity_ids) = 0 AND cardinality(va.entity_ids) = 0 THEN 0.0
              ELSE ((SELECT COUNT(*) FROM (SELECT unnest(nc.entity_ids) INTERSECT
                                             SELECT unnest(va.entity_ids)) i)::float8
                   / NULLIF((SELECT COUNT(*) FROM (SELECT unnest(nc.entity_ids) UNION
                                                    SELECT unnest(va.entity_ids)) u), 0))
         END) AS entity_jaccard,
        CASE WHEN nc.tsq IS NULL THEN 0.0
             ELSE COALESCE(ts_rank_cd(va.tsvector_doc, nc.tsq, 32)::float8, 0.0)
        END AS keyword_ts_rank
      FROM inbox_router_note_cache nc
      JOIN inbox_router_vault_anchors va ON va.vault_id = :vault_id ::uuid
     WHERE nc.note_id = :note_id ::uuid
),
new_feature_rows AS (
    SELECT 'sem_summary_sim'  AS feat, sem_summary_sim  AS x FROM new_features
    UNION ALL SELECT 'sem_centroid_sim', sem_centroid_sim FROM new_features
    UNION ALL SELECT 'mm_centroid_sim',  mm_centroid_sim  FROM new_features
    UNION ALL SELECT 'entity_jaccard',   entity_jaccard   FROM new_features
    UNION ALL SELECT 'keyword_ts_rank',  keyword_ts_rank  FROM new_features
),
stats_update AS (
    UPDATE inbox_router_nb_stats s
       SET n        = :gamma ::float8 * s.n        + 1,
           sum_x    = :gamma ::float8 * s.sum_x    + nfr.x,
           sum_x_sq = :gamma ::float8 * s.sum_x_sq + nfr.x * nfr.x,
           updated_at = now()
      FROM new_feature_rows nfr
     WHERE s.feature_name = nfr.feat AND s.label = :label ::smallint
    RETURNING 1
)
UPDATE inbox_router_nb_class_counts c
   SET n = :gamma ::float8 * c.n + 1, updated_at = now()
 WHERE c.label = :label ::smallint
"""


# Count auto-applies recorded today for a (vault, rule) — for the per-tick /
# daily safety budget. Counts resolved routing proposals stamped by the router.
COUNT_AUTO_APPLIED_TODAY_SQL = """
SELECT COUNT(*) FROM maintenance_proposals
 WHERE rule_name = 'inbox_vault_route'
   AND vault_id = :vault_id ::uuid
   AND status = 'resolved'
   AND resolved_by = 'system:inbox-router'
   AND resolved_at >= :today_start
"""
