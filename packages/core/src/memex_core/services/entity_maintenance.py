"""Cross-batch entity-cluster collapse — scan, cluster, propose.

Cross-batch entity resolution drifts: ingestion-path matcher (calibrated for
intra-batch dedup) leaves near-duplicate Entity rows ("ACME Corp" /
"acme corp" / "Acme Corp") in production data. This module periodically
scans the top-N most active entities, builds a candidate-pair similarity
graph, computes connected components, applies a min-pairwise-similarity
cohesion guard (rope-drift protection), and emits one
``MaintenanceProposal`` per surviving cluster.

The scan is a pure proposal — no entity rows are mutated here. Apply is
handled by ``EntityService.collapse_cluster`` after a human approves the
finding via ``memex lint resolve --winner …``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from itertools import combinations
from typing import TYPE_CHECKING, Any

from sqlalchemy import text

from memex_core.memory.entity_resolver import score_entity_pair
from memex_core.memory.sql_models import LintType

if TYPE_CHECKING:
    from memex_core.api import MemexAPI

try:
    from opentelemetry import trace as _otel_trace

    _tracer = _otel_trace.get_tracer('memex.entity_maintenance')
except Exception:  # pragma: no cover
    _tracer = None

logger = logging.getLogger('memex.core.services.entity_maintenance')


_RULE_NAME = 'entity_collapse_cluster'
_TARGET_TYPE = 'entity'
# Distinct 64-bit advisory-lock key for the cross-batch entity-cluster
# collapse scan. Held at transaction scope so two concurrent callers
# (scheduler tick + on-demand HTTP/CLI) cannot race on the
# composition_hash lookup and emit duplicate findings — the partial
# unique index on maintenance_proposals does not cover vault_id IS NULL
# rows, so concurrency control has to live here. Picked to not collide
# with MEMEX_LEADER_LOCK_ID in scheduler.py.
_MEMEX_ENTITY_MAINTENANCE_LOCK_ID = 0xE1141E5C
_SUGGESTED_ACTION = (
    'Cluster of near-duplicate entities detected across batches. '
    'Review the members in evidence.cluster_members and approve via '
    '`memex lint resolve <finding_id> --winner <member_id_or_name>` to '
    'merge them into one canonical entity. Cross-vault: the merge affects '
    'every vault listed in evidence.vaults_affected.'
)


def _composition_hash(member_ids: list[str]) -> str:
    """Order-independent fingerprint of a cluster's membership."""
    canonical = sorted(str(m) for m in member_ids)
    return hashlib.sha256('|'.join(canonical).encode('utf-8')).hexdigest()


def _connected_components(nodes: list[str], edges: list[tuple[str, str]]) -> list[list[str]]:
    """Plain union-find over a small node set; deterministic ordering."""
    parent = {n: n for n in nodes}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for a, b in edges:
        if a in parent and b in parent:
            union(a, b)

    groups: dict[str, list[str]] = defaultdict(list)
    for n in nodes:
        groups[find(n)].append(n)
    return [sorted(g) for g in groups.values() if len(g) >= 2]


def _min_pairwise_similarity(
    members: list[str],
    sims: dict[tuple[str, str], float],
) -> tuple[float, float]:
    """Return (min, max) pairwise similarity across all pairs in the cluster."""
    values: list[float] = []
    for a, b in combinations(sorted(members), 2):
        values.append(sims.get((a, b), 0.0))
    if not values:
        return (0.0, 0.0)
    return (min(values), max(values))


def _suggested_winner(members: list[dict[str, Any]]) -> str:
    """Pick a suggested winner from a cluster.

    Rule: highest ``mention_count``, ties broken by earliest ``first_seen``.
    """
    sorted_members = sorted(
        members,
        key=lambda m: (-int(m.get('mention_count') or 0), m.get('first_seen') or datetime.max),
    )
    return str(sorted_members[0]['id'])


async def scan_collapse_clusters(
    api: 'MemexAPI',
    *,
    top_n: int | None = None,
    scan_cooldown_days: int | None = None,
    pair_threshold: float | None = None,
    cluster_min_threshold: float | None = None,
) -> dict[str, Any]:
    """Scan top-N active entities, cluster duplicates, emit cluster proposals.

    Returns a summary dict ``{'clusters_emitted': int,
    'clusters_rejected_cohesion': int, 'rescan_updated': int, 'scanned': int}``.

    Idempotent under rescan: cluster identity is the order-independent
    ``composition_hash`` of its members, not the (possibly-shifting) suggested
    winner. An existing pending ``entity_collapse_cluster`` finding with the
    same composition_hash is UPDATEd in place; a membership shift writes a
    new row. The partial unique index ``uq_maintenance_proposals_pending``
    only fires on non-NULL ``vault_id`` rows; our findings carry ``vault_id
    IS NULL`` (cross-vault by construction), so we enforce the no-duplicate
    invariant in Python before INSERT.
    """
    from memex_core import metrics

    cfg = api.config.server.memory.entity_maintenance
    top_n = top_n if top_n is not None else cfg.top_n
    scan_cooldown_days = (
        scan_cooldown_days if scan_cooldown_days is not None else cfg.scan_cooldown_days
    )
    pair_threshold = pair_threshold if pair_threshold is not None else cfg.pair_threshold
    cluster_min_threshold = (
        cluster_min_threshold if cluster_min_threshold is not None else cfg.cluster_min_threshold
    )

    span_cm = (
        _tracer.start_as_current_span(
            'memex.entity_maintenance.scan',
            attributes={
                'top_n': top_n,
                'scan_cooldown_days': scan_cooldown_days,
                'pair_threshold': pair_threshold,
                'cluster_min_threshold': cluster_min_threshold,
            },
        )
        if _tracer
        else None
    )
    if span_cm is not None:
        span_cm.__enter__()

    summary = {
        'clusters_emitted': 0,
        'clusters_rejected_cohesion': 0,
        'rescan_updated': 0,
        'scanned': 0,
    }

    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=scan_cooldown_days)
        async with api.metastore.session() as session:
            lock_acquired = (
                await session.execute(
                    text('SELECT pg_try_advisory_xact_lock(:lock_id)'),
                    {'lock_id': _MEMEX_ENTITY_MAINTENANCE_LOCK_ID},
                )
            ).scalar()
            if not lock_acquired:
                metrics.ENTITY_COLLAPSE_SCAN_EMITTED_TOTAL.labels(result='concurrent_skipped').inc()
                logger.info('entity_collapse_scan: concurrent scan in progress, skipping')
                return summary

            cand_rows = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT id::text AS id,
                               canonical_name,
                               phonetic_code,
                               mention_count,
                               first_seen,
                               last_merge_scan_at
                        FROM entities
                        WHERE last_merge_scan_at IS NULL
                           OR last_merge_scan_at < :cutoff
                        ORDER BY mention_count DESC, first_seen ASC
                        LIMIT :limit
                        """
                        ),
                        {'cutoff': cutoff, 'limit': top_n},
                    )
                )
                .mappings()
                .all()
            )

            candidates: list[dict[str, Any]] = [dict(r) for r in cand_rows]
            summary['scanned'] = len(candidates)
            if len(candidates) < 2:
                metrics.ENTITY_COLLAPSE_SCAN_EMITTED_TOTAL.labels(result='no_candidates').inc()
                logger.info('entity_collapse_scan: no_candidates scanned=%d', len(candidates))
                return summary

            ids = [c['id'] for c in candidates]
            neighbor_rows = (
                (
                    await session.execute(
                        text(
                            """
                        WITH combined AS (
                            SELECT entity_id_1 AS source_id,
                                   entity_id_2 AS neighbor_id,
                                   cooccurrence_count
                            FROM entity_cooccurrences
                            WHERE entity_id_1 = ANY(CAST(:ids AS uuid[]))

                            UNION ALL

                            SELECT entity_id_2 AS source_id,
                                   entity_id_1 AS neighbor_id,
                                   cooccurrence_count
                            FROM entity_cooccurrences
                            WHERE entity_id_2 = ANY(CAST(:ids AS uuid[]))
                        )
                        SELECT c.source_id::text AS source_id,
                               e.id::text AS neighbor_id,
                               e.mention_count AS neighbor_mention_count
                        FROM combined c
                        JOIN entities e ON e.id = c.neighbor_id
                        """
                        ),
                        {'ids': ids},
                    )
                )
                .mappings()
                .all()
            )

            neighbors_by_entity: dict[str, dict[str, int]] = defaultdict(dict)
            for r in neighbor_rows:
                neighbors_by_entity[r['source_id']][r['neighbor_id']] = int(
                    r['neighbor_mention_count']
                )

            # Identify which vaults each candidate appears in via unit_entities
            vault_rows = (
                (
                    await session.execute(
                        text(
                            """
                        SELECT entity_id::text AS entity_id,
                               vault_id::text AS vault_id
                        FROM unit_entities
                        WHERE entity_id = ANY(CAST(:ids AS uuid[]))
                        GROUP BY entity_id, vault_id
                        """
                        ),
                        {'ids': ids},
                    )
                )
                .mappings()
                .all()
            )
            vaults_by_entity: dict[str, set[str]] = defaultdict(set)
            for r in vault_rows:
                vaults_by_entity[r['entity_id']].add(r['vault_id'])

            sims: dict[tuple[str, str], float] = {}
            edges: list[tuple[str, str]] = []
            for a, b in combinations(candidates, 2):
                sim = score_entity_pair(
                    a['canonical_name'] or '',
                    b['canonical_name'] or '',
                    a.get('phonetic_code'),
                    b.get('phonetic_code'),
                    neighbors_by_entity.get(a['id'], {}),
                    neighbors_by_entity.get(b['id'], {}),
                )
                key = tuple(sorted([a['id'], b['id']]))
                sims[key] = sim
                if sim >= pair_threshold:
                    edges.append(key)  # type: ignore[arg-type]

            clusters = _connected_components([c['id'] for c in candidates], edges)
            by_id = {c['id']: c for c in candidates}

            for member_ids in clusters:
                pair_min, pair_max = _min_pairwise_similarity(member_ids, sims)
                if pair_min < cluster_min_threshold:
                    metrics.ENTITY_COLLAPSE_SCAN_EMITTED_TOTAL.labels(
                        result='rejected_cohesion'
                    ).inc()
                    summary['clusters_rejected_cohesion'] += 1
                    logger.info(
                        'entity_collapse_scan: rejected_cohesion cluster_id=%s '
                        'size=%d pair_min=%.3f pair_max=%.3f',
                        _composition_hash(member_ids),
                        len(member_ids),
                        pair_min,
                        pair_max,
                    )
                    continue

                cluster_members = [by_id[m] for m in member_ids]
                winner_id = _suggested_winner(cluster_members)
                vaults_affected: set[str] = set()
                for m in member_ids:
                    vaults_affected.update(vaults_by_entity.get(m, set()))

                composition_hash = _composition_hash(member_ids)
                evidence = {
                    'cluster_members': sorted(member_ids),
                    'suggested_winner_id': winner_id,
                    'pair_min_similarity': pair_min,
                    'pair_max_similarity': pair_max,
                    'composition_hash': composition_hash,
                    'vaults_affected': sorted(vaults_affected),
                    'member_canonical_names': {m: by_id[m]['canonical_name'] for m in member_ids},
                }

                # Rescan-collision policy: identity is cluster membership, not
                # the (possibly-shifting) suggested winner. We key on
                # composition_hash so a winner reorder between scans UPDATEs
                # the existing row, and a membership change INSERTs a new one.
                existing_row = (
                    (
                        await session.execute(
                            text(
                                """
                            SELECT id::text AS id, evidence
                            FROM maintenance_proposals
                            WHERE rule_name = :rule_name
                              AND target_type = :target_type
                              AND status = 'pending'
                              AND vault_id IS NULL
                              AND evidence ->> 'composition_hash' = :composition_hash
                            ORDER BY created_at ASC
                            LIMIT 1
                            """
                            ),
                            {
                                'rule_name': _RULE_NAME,
                                'target_type': _TARGET_TYPE,
                                'composition_hash': composition_hash,
                            },
                        )
                    )
                    .mappings()
                    .first()
                )

                if existing_row is not None:
                    await session.execute(
                        text(
                            """
                            UPDATE maintenance_proposals
                            SET evidence = CAST(:evidence AS jsonb),
                                target_id = :target_id,
                                suggested_action = :suggested_action
                            WHERE id = CAST(:id AS uuid)
                            """
                        ),
                        {
                            'evidence': json.dumps(evidence),
                            'target_id': winner_id,
                            'suggested_action': _SUGGESTED_ACTION,
                            'id': existing_row['id'],
                        },
                    )
                    summary['rescan_updated'] += 1
                    metrics.ENTITY_COLLAPSE_SCAN_EMITTED_TOTAL.labels(result='rescan_updated').inc()
                    logger.warning(
                        'entity_collapse_scan: rescan_updated cluster_id=%s size=%d existing_id=%s',
                        composition_hash,
                        len(member_ids),
                        existing_row['id'],
                    )
                else:
                    await session.execute(
                        text(
                            """
                            INSERT INTO maintenance_proposals (
                                vault_id, lint_type, target_type, target_id,
                                rule_name, evidence, suggested_action,
                                status, source
                            )
                            VALUES (
                                NULL,
                                :lint_type,
                                :target_type,
                                :target_id,
                                :rule_name,
                                CAST(:evidence AS jsonb),
                                :suggested_action,
                                'pending',
                                'rule'
                            )
                            """
                        ),
                        {
                            'lint_type': LintType.QUALITY.value,
                            'target_type': _TARGET_TYPE,
                            'target_id': winner_id,
                            'rule_name': _RULE_NAME,
                            'evidence': json.dumps(evidence),
                            'suggested_action': _SUGGESTED_ACTION,
                        },
                    )
                    summary['clusters_emitted'] += 1
                    metrics.ENTITY_COLLAPSE_SCAN_EMITTED_TOTAL.labels(result='proposed').inc()
                    logger.info(
                        'entity_collapse_scan: proposed cluster_id=%s size=%d '
                        'pair_min=%.3f pair_max=%.3f',
                        composition_hash,
                        len(member_ids),
                        pair_min,
                        pair_max,
                    )

            # Mark every scanned entity with the current timestamp so the
            # cooldown filter excludes them on the next pass.
            now = datetime.now(timezone.utc)
            await session.execute(
                text(
                    'UPDATE entities SET last_merge_scan_at = :now '
                    'WHERE id = ANY(CAST(:ids AS uuid[]))'
                ),
                {'now': now, 'ids': ids},
            )

            await session.commit()

        return summary
    finally:
        if span_cm is not None:
            try:
                span_cm.__exit__(None, None, None)
            except Exception:  # pragma: no cover
                pass
