"""Maintenance ledger — rule-based linter.

Implements ``LintService.run_rules(vault_id?)`` which iterates the v1 rule
registry and emits ``MaintenanceProposal`` rows. Rules are read-only against
the resources they inspect; the only writes ``run_rules`` performs are
``INSERT INTO maintenance_proposals`` (with ``ON CONFLICT DO NOTHING`` against
the partial unique index ``uq_maintenance_proposals_pending``).

v1 rule set (one per ``LintType`` enum value):

  - ``orphan_mental_model``         (structural)
  - ``cold_low_mw_unit``            (quality)
  - ``sensitive_unreviewed_unit``   (governance, governance acceptance criteria)
  - ``dangling_entity_ref_in_unit`` (schema)

The mw_score expression is the inline form of
``services.outcomes.compute_mw_score`` (Beta-Bernoulli posterior mean
α=β=1):  ``(success_co_count + 1.0) / (success_co_count + failure_co_count + 2)``
Drift between the two forms is guarded by a unit test.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from memex_core.memory._lint_utils import enum_value as _enum_value
from memex_core.memory.sql_models import LintType
from memex_core.services.base import BaseService
from memex_core.services.lint_confidence import (
    bulk_load_confidence_map,
    confidence_map_blocks,
)

try:
    from opentelemetry import trace as _otel_trace

    _tracer = _otel_trace.get_tracer('memex.lint')
except Exception:  # pragma: no cover — OTel optional at import time
    _tracer = None

logger = logging.getLogger('memex.core.services.lint')


# ---------------------------------------------------------------------------
# Read-side DTOs, cursor codec, and table-not-found signal
# ---------------------------------------------------------------------------


_MAX_LIMIT = 200


class LintSubsystemNotInitializedError(RuntimeError):
    """Signals acceptance criteria: ``maintenance_proposals`` table is missing.

    The MCP/HTTP layer translates this into the documented error envelope
    so the agent gets a clear, actionable message ("run alembic upgrade").
    """

    def __init__(self) -> None:
        super().__init__(
            'Maintenance proposals table is missing. '
            'Run `alembic upgrade head` to initialize the lint ledger.'
        )


class LintFindingDTO(BaseModel):
    """Shape-stable read view over a :class:`MaintenanceProposal` row."""

    finding_id: UUID
    target_id: str
    target_type: str
    lint_type: str
    rule_name: str
    evidence: dict[str, Any]
    suggested_action: str
    status: str
    source: str
    vault_id: UUID | None
    created_at: datetime
    resolved_at: datetime | None
    resolved_by: str | None = None

    @classmethod
    def from_row(cls, row: Any) -> LintFindingDTO:
        return cls(
            finding_id=row['id'],
            target_id=row['target_id'],
            target_type=row['target_type'],
            lint_type=_enum_value(row['lint_type']),
            rule_name=row['rule_name'],
            evidence=row['evidence'] or {},
            suggested_action=row['suggested_action'],
            status=_enum_value(row['status']),
            source=_enum_value(row['source']),
            vault_id=row['vault_id'],
            created_at=row['created_at'],
            resolved_at=row['resolved_at'],
            resolved_by=row['resolved_by'] if 'resolved_by' in row.keys() else None,
        )


class LintFindingsPage(BaseModel):
    """Shape-stable page envelope returned by :meth:`LintService.get_findings`.

    ``findings`` is always present (possibly empty); ``next_cursor`` is
    always present (string or None). The agent surface MUST round-trip
    these fields verbatim — never collapse to a bare list or null.
    """

    findings: list[LintFindingDTO]
    next_cursor: str | None


def _encode_cursor(created_at: datetime, finding_id: UUID) -> str:
    """Opaque base64 of ``{ts, id}`` — total-order across concurrent inserts."""
    payload = json.dumps({'ts': created_at.isoformat(), 'id': str(finding_id)})
    return base64.urlsafe_b64encode(payload.encode('utf-8')).decode('ascii')


def _decode_cursor(cursor: str) -> tuple[datetime, UUID] | None:
    """Decode an opaque cursor; return None on any malformed input.

    Returning None lets ``get_findings`` treat junk cursors as "page 1"
    rather than 500-ing — the cursor is opaque so agents can't reason
    about its shape, and a stale cursor (e.g. across redeploys)
    should degrade gracefully.
    """
    try:
        raw = base64.urlsafe_b64decode(cursor.encode('ascii')).decode('utf-8')
        data = json.loads(raw)
        return datetime.fromisoformat(data['ts']), UUID(data['id'])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return None


@dataclass(frozen=True)
class RuleSpec:
    """Static description of a lint rule.

    The ``select_sql`` is a SELECT statement that returns one row per finding
    with the columns ``target_id`` (text) and ``evidence`` (jsonb). Rules MUST
    include ``vault_id = :vault_id`` in their WHERE clause when their target
    is vault-scoped — guarded by ``test_lint_rule_sql_audits.py``.

    ``param_keys`` declares the named bind parameters the rule's SQL needs
    beyond ``vault_id``. ``LintService._run_one`` resolves them by reading
    ``self.config.server.memory.deprioritize_score`` and similar config
    blocks and passes the resulting values as a single mapping. Names must
    match the ``:placeholder`` form in ``select_sql``.
    """

    name: str
    lint_type: LintType
    target_type: str
    suggested_action: str
    select_sql: str
    param_keys: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# v1 rule definitions
# ---------------------------------------------------------------------------


_MW_SCORE_EXPR = '((success_co_count + 1.0) / (success_co_count + failure_co_count + 2))'


_ORPHAN_MENTAL_MODEL_SQL = """
    SELECT
        mm.id::text AS target_id,
        jsonb_build_object(
            'last_refreshed', mm.last_refreshed,
            'observation_count', jsonb_array_length(mm.observations),
            'linked_active_units', 0
        ) AS evidence
    FROM mental_models mm
    WHERE mm.vault_id = :vault_id
      AND mm.last_refreshed < (now() - interval '30 days')
      AND NOT EXISTS (
          SELECT 1
          FROM unit_entities ue
          JOIN memory_units mu ON mu.id = ue.unit_id
          WHERE ue.entity_id = mm.entity_id
            AND ue.vault_id = mm.vault_id
            AND mu.status = 'active'
      )
"""


# Safety invariant for S608: the only interpolations in this f-string are
# ``_MW_SCORE_EXPR`` (a module-private literal at line 166 — a fixed
# arithmetic expression with no user input). ``vault_id`` is bound via the
# :name placeholder and never interpolated. No user-controlled value is ever
# spliced into the SQL text.
#
# noqa placement: ruff 0.14.x anchors S608 on the FIRST physical line of a
# multi-line concatenated string, but for triple-quoted strings the
# diagnostic span covers BOTH the opening and closing ``"""`` lines, and an
# inline comment on the opening line would become part of the string body.
# The noqa therefore sits on the closing ``"""`` line — verified empirically
# by stripping the marker (ruff reports the diagnostic anchored at the
# opening line and underlines through the closing line, and the
# closing-line noqa successfully suppresses).
_COLD_LOW_MW_UNIT_SQL = f"""
    SELECT
        mu.id::text AS target_id,
        jsonb_build_object(
            'mw_score', {_MW_SCORE_EXPR},
            'success_co_count', mu.success_co_count,
            'failure_co_count', mu.failure_co_count,
            'last_outcome_age_days', extract(epoch FROM (now() - mu.updated_at)) / 86400
        ) AS evidence
    FROM memory_units mu
    WHERE mu.vault_id = :vault_id
      AND mu.status = 'active'
      AND mu.is_deprioritized = false
      AND (mu.success_co_count + mu.failure_co_count) >= 5
      AND {_MW_SCORE_EXPR} < 0.3
      AND mu.updated_at < (now() - interval '30 days')
"""  # noqa: S608 — see invariant above (anchor verified at this line)


_SENSITIVE_UNREVIEWED_UNIT_SQL = """
    SELECT
        mu.id::text AS target_id,
        jsonb_build_object(
            'risk_class', mu.risk_class,
            'created_at', mu.created_at,
            'last_governance_action_at', NULL
        ) AS evidence
    FROM memory_units mu
    WHERE mu.vault_id = :vault_id
      AND mu.status = 'active'
      AND mu.risk_class = 'sensitive'
      AND mu.is_deprioritized = false
      AND NOT EXISTS (
          SELECT 1
          FROM audit_logs al
          WHERE al.action IN ('memory_deprioritize', 'memory_restore', 'lint_finding_resolved')
            AND al.resource_type = 'memory_unit'
            AND al.resource_id = mu.id::text
            AND al.timestamp > (now() - interval '30 days')
      )
"""


_DANGLING_ENTITY_REF_IN_UNIT_SQL = """
    SELECT
        ue.unit_id::text AS target_id,
        jsonb_build_object(
            'entity_id', ue.entity_id::text
        ) AS evidence
    FROM unit_entities ue
    LEFT JOIN entities e ON e.id = ue.entity_id
    WHERE ue.vault_id = :vault_id
      AND e.id IS NULL
"""


# Safety: vault_id is parameter-bound; no user-controlled string interpolation.
# See S608 invariant block above ``_COLD_LOW_MW_UNIT_SQL``.
#
# Aggregates orphan ``contradicts`` outbound links per source unit so the
# partial unique index ``(rule_name, target_type, target_id, vault_id) WHERE
# status='pending'`` collapses multiple stale targets into one proposal per
# source unit. The evidence carries the full list of stale target ids and
# their stale-since timestamp so the operator can navigate to the source
# unit, inspect each orphan edge, and reap.
_ORPHAN_CONTRADICTS_LINKS_POST_STALE_SQL = """
    SELECT
        ml.from_unit_id::text AS target_id,
        jsonb_build_object(
            'orphan_link_count', COUNT(*),
            'stale_target_unit_ids',
                jsonb_agg(ml.to_unit_id::text ORDER BY ml.to_unit_id),
            'stale_targets_oldest_updated_at',
                MIN(target.updated_at)
        ) AS evidence
    FROM memory_links ml
    JOIN memory_units target ON target.id = ml.to_unit_id
    JOIN memory_units source ON source.id = ml.from_unit_id
    WHERE ml.link_type = 'contradicts'
      AND ml.vault_id = :vault_id
      AND source.vault_id = :vault_id
      AND target.vault_id = :vault_id
      AND target.status = 'stale'
      AND source.status = 'active'
    GROUP BY ml.from_unit_id
"""


# Safety: vault_id and ``claim_too_aggressive_max_links`` are bound via
# named placeholders; no user-controlled string interpolation. See S608
# invariant block above ``_COLD_LOW_MW_UNIT_SQL``.
_CLAIM_TOO_AGGRESSIVE_SQL = """
    SELECT
        mu.id::text AS target_id,
        jsonb_build_object(
            'claim_type', mu.claim_type,
            'link_count', COUNT(ml.to_unit_id)
        ) AS evidence
    FROM memory_units mu
    JOIN memory_links ml ON ml.from_unit_id = mu.id
    WHERE mu.vault_id = :vault_id
      AND mu.claim_type IS NOT NULL
      AND ml.link_type IN ('contradicts', 'weakens')
    GROUP BY mu.id, mu.claim_type
    HAVING COUNT(*) > CAST(:claim_too_aggressive_max_links AS integer)
"""


# ---------------------------------------------------------------------------
# FSFM-inspired graph-aware deprioritization scoring rules.
#
# All four rules share one SQL CTE pipeline (``unit_signals`` →
# ``unit_components`` → ``unit_scores``) that mirrors the canonical Python
# implementation in ``services/deprioritize_score.py``.  The constants
# embedded in the SQL match the Pydantic defaults in
# ``DeprioritizeScoreConfig`` (weights, lambda_link=0.01, mu_entity=0.005,
# propose threshold 0.30).  Drift between SQL and Python is guarded by
# ``test_fsfm_sql_python_parity.py``; if the user overrides the config
# weights at runtime, the lint rules continue to use the defaults — this
# is intentional (predictable behaviour for the maintenance ledger).
# ---------------------------------------------------------------------------

# Safety: vault_id is parameter-bound; every numeric knob is bound via
# named placeholder (``:weight_graph`` etc.) so the SQL text contains no
# user-controlled string interpolation. See the S608 invariant comment
# block above ``_COLD_LOW_MW_UNIT_SQL``.
_FSFM_COMPOSITE_DEPRIORITIZE_SQL = """
    WITH unit_signals AS (
        SELECT
            mu.id AS unit_id,
            mu.vault_id AS vault_id,
            mu.success_co_count AS success_co_count,
            mu.failure_co_count AS failure_co_count,
            mu.last_outcome_at AS last_outcome_at,
            mu.stability AS stability,
            mu.importance AS importance,
            mu.intent_class AS intent_class,
            mu.risk_class AS risk_class,
            mu.confidence AS confidence,
            ((mu.success_co_count + 1.0) / (mu.success_co_count + mu.failure_co_count + 2)) AS mw_score,
            (
                SELECT SUM(
                    CASE ml.link_type
                        WHEN 'contradicts' THEN 1.0
                        WHEN 'weakens' THEN 0.5
                        WHEN 'reinforces' THEN -1.0
                        WHEN 'causes' THEN -0.1
                        WHEN 'caused_by' THEN -0.1
                        WHEN 'enables' THEN -0.1
                        WHEN 'prevents' THEN -0.1
                        WHEN 'refines' THEN 0.0
                        ELSE 0.0
                    END
                    * ml.weight
                    * src.confidence
                    * ((src.success_co_count + 1.0) / (src.success_co_count + src.failure_co_count + 2))
                    * exp(-CAST(:lambda_link AS double precision) * GREATEST(0.0, EXTRACT(EPOCH FROM (now() - ml.created_at)) / 86400.0))
                )
                FROM memory_links ml
                JOIN memory_units src ON src.id = ml.from_unit_id
                WHERE ml.to_unit_id = mu.id
                  AND ml.vault_id = mu.vault_id
                  AND src.vault_id = mu.vault_id
                  AND ml.link_type IN (
                    'contradicts', 'weakens', 'reinforces',
                    'causes', 'caused_by', 'enables', 'prevents', 'refines'
                  )
            ) AS graph_pressure_raw,
            (
                SELECT COUNT(*)
                FROM memory_links ml
                JOIN memory_units src ON src.id = ml.from_unit_id
                WHERE ml.to_unit_id = mu.id
                  AND ml.link_type = 'contradicts'
                  AND ml.vault_id = mu.vault_id
                  AND src.vault_id = mu.vault_id
            ) AS contradicts_count,
            (
                SELECT COALESCE(SUM(
                    src.confidence * ((src.success_co_count + 1.0) /
                                      (src.success_co_count + src.failure_co_count + 2))
                ), 0.0)
                FROM memory_links ml
                JOIN memory_units src ON src.id = ml.from_unit_id
                WHERE ml.to_unit_id = mu.id
                  AND ml.link_type = 'contradicts'
                  AND ml.vault_id = mu.vault_id
                  AND src.vault_id = mu.vault_id
            ) AS contradicts_credibility_sum,
            (
                SELECT MAX(e.last_seen)
                FROM unit_entities ue
                JOIN entities e ON e.id = ue.entity_id
                WHERE ue.unit_id = mu.id AND ue.vault_id = mu.vault_id
            ) AS freshest_entity_last_seen
        FROM memory_units mu
        WHERE mu.vault_id = :vault_id
          AND mu.status = 'active'
          AND mu.is_deprioritized = false
          AND COALESCE(mu.intent_class, '') != 'permanent'
          AND COALESCE(mu.risk_class, 'none') NOT IN ('sensitive', 'private', 'safety')
    ),
    unit_components AS (
        SELECT
            s.*,
            CASE
                WHEN s.graph_pressure_raw IS NULL THEN 0.5
                ELSE 1.0 / (1.0 + exp(-s.graph_pressure_raw))
            END AS graph_pressure,
            (1.0 - s.mw_score) AS mw_complement,
            CASE
                WHEN s.last_outcome_at IS NULL OR s.stability IS NULL OR s.stability <= 0
                    THEN 0.0
                ELSE 1.0 - exp(
                    -GREATEST(0.0, EXTRACT(EPOCH FROM (now() - s.last_outcome_at)) / 86400.0)
                    / s.stability
                )
            END AS temporal_staleness,
            CASE
                WHEN s.freshest_entity_last_seen IS NULL THEN 0.0
                ELSE 1.0 - exp(
                    -CAST(:mu_entity AS double precision) * GREATEST(0.0,
                        EXTRACT(EPOCH FROM (now() - s.freshest_entity_last_seen)) / 86400.0
                    )
                )
            END AS entity_dormancy
        FROM unit_signals s
    ),
    unit_scores AS (
        SELECT
            c.*,
            (
                CAST(:weight_graph AS double precision) * c.graph_pressure
              + CAST(:weight_mw AS double precision) * c.mw_complement
              + CAST(:weight_temporal AS double precision) * c.temporal_staleness
              + CAST(:weight_entity AS double precision) * c.entity_dormancy
            ) * (1.0 - COALESCE(c.importance, 0.5)) AS composite_score,
            -- component_range is a cheap proxy for "components disagree":
            -- a high range means signals point in different directions.
            (
                GREATEST(
                    CASE WHEN c.graph_pressure_raw IS NULL THEN 0.5
                         ELSE 1.0 / (1.0 + exp(-c.graph_pressure_raw)) END,
                    1.0 - c.mw_score,
                    CASE WHEN c.last_outcome_at IS NULL OR c.stability IS NULL OR c.stability <= 0
                         THEN 0.0
                         ELSE 1.0 - exp(
                             -GREATEST(0.0, EXTRACT(EPOCH FROM (now() - c.last_outcome_at)) / 86400.0)
                             / c.stability
                         ) END,
                    CASE WHEN c.freshest_entity_last_seen IS NULL THEN 0.0
                         ELSE 1.0 - exp(
                             -CAST(:mu_entity AS double precision) * GREATEST(0.0,
                                 EXTRACT(EPOCH FROM (now() - c.freshest_entity_last_seen)) / 86400.0
                             )
                         ) END
                )
              - LEAST(
                    CASE WHEN c.graph_pressure_raw IS NULL THEN 0.5
                         ELSE 1.0 / (1.0 + exp(-c.graph_pressure_raw)) END,
                    1.0 - c.mw_score,
                    CASE WHEN c.last_outcome_at IS NULL OR c.stability IS NULL OR c.stability <= 0
                         THEN 0.0
                         ELSE 1.0 - exp(
                             -GREATEST(0.0, EXTRACT(EPOCH FROM (now() - c.last_outcome_at)) / 86400.0)
                             / c.stability
                         ) END,
                    CASE WHEN c.freshest_entity_last_seen IS NULL THEN 0.0
                         ELSE 1.0 - exp(
                             -CAST(:mu_entity AS double precision) * GREATEST(0.0,
                                 EXTRACT(EPOCH FROM (now() - c.freshest_entity_last_seen)) / 86400.0
                             )
                         ) END
                )
            ) AS component_range
        FROM unit_components c
    )
    -- One row per qualifying unit. ``flag_reason`` distinguishes the
    -- escalation patterns from the vanilla ``composite`` reason that the
    -- auto-band consumes. Order of precedence (most-specific first):
    --   1. low_credibility_contradiction_only
    --   2. components_disagree (only when all four components have data)
    --   3. high_mw_with_nonmw_pressure
    --   4. composite                          ← auto-band eligible
    SELECT
        s.unit_id::text AS target_id,
        jsonb_build_object(
            'composite_score', s.composite_score,
            'components', jsonb_build_object(
                'graph_pressure', s.graph_pressure,
                'mw_complement', s.mw_complement,
                'temporal_staleness', s.temporal_staleness,
                'entity_dormancy', s.entity_dormancy
            ),
            'component_range', s.component_range,
            'mw_score', s.mw_score,
            'importance', s.importance,
            'intent_class', s.intent_class,
            'success_co_count', s.success_co_count,
            'failure_co_count', s.failure_co_count,
            'contradicts_count', s.contradicts_count,
            'contradicts_credibility_sum', s.contradicts_credibility_sum,
            'flag_reason',
            CASE
                WHEN s.contradicts_count > 0
                     AND s.contradicts_credibility_sum < CAST(:contradicted_low_credibility_max AS double precision)
                    THEN 'low_credibility_contradiction_only'
                WHEN s.last_outcome_at IS NOT NULL
                     AND s.stability IS NOT NULL AND s.stability > 0
                     AND s.freshest_entity_last_seen IS NOT NULL
                     AND s.component_range > CAST(:disagreement_range AS double precision)
                    THEN 'components_disagree'
                WHEN s.mw_score > CAST(:high_mw_threshold AS double precision)
                     AND (s.success_co_count + s.failure_co_count) >= CAST(:high_mw_min_outcomes AS integer)
                    THEN 'high_mw_with_nonmw_pressure'
                ELSE 'composite'
            END
        ) AS evidence
    FROM unit_scores s
    WHERE s.composite_score > CAST(:propose_threshold AS double precision)
"""  # noqa: S608 — every interpolation is a named bind parameter


V1_RULES: tuple[RuleSpec, ...] = (
    RuleSpec(
        name='orphan_mental_model',
        lint_type=LintType.STRUCTURAL,
        target_type='mental_model',
        suggested_action=(
            'Mental model has no active linked memory units for >30 days. '
            'Archive it or restore observations.'
        ),
        select_sql=_ORPHAN_MENTAL_MODEL_SQL,
    ),
    RuleSpec(
        name='cold_low_mw_unit',
        lint_type=LintType.QUALITY,
        target_type='memory_unit',
        suggested_action=(
            'Unit has 5+ outcomes, low Memory Worth score, and 30+ days of inactivity. '
            "Call memex_memory_deprioritize with reason='low Memory Worth after 5+ outcomes'."
        ),
        select_sql=_COLD_LOW_MW_UNIT_SQL,
    ),
    RuleSpec(
        name='sensitive_unreviewed_unit',
        lint_type=LintType.GOVERNANCE,
        target_type='memory_unit',
        suggested_action=(
            'Sensitive unit has not been reviewed in 30 days. '
            'Review and either confirm or call memex_memory_deprioritize.'
        ),
        select_sql=_SENSITIVE_UNREVIEWED_UNIT_SQL,
    ),
    RuleSpec(
        name='dangling_entity_ref_in_unit',
        lint_type=LintType.SCHEMA,
        target_type='unit_entity',
        suggested_action=(
            'UnitEntity row references an entity that no longer exists '
            '(data integrity issue). Remove the dangling reference.'
        ),
        select_sql=_DANGLING_ENTITY_REF_IN_UNIT_SQL,
    ),
    RuleSpec(
        name='orphan_contradicts_links_post_stale',
        lint_type=LintType.QUALITY,
        target_type='memory_unit',
        suggested_action=(
            "Active source unit holds outbound 'contradicts' edge(s) to "
            'stale target unit(s). Retrieval already filters stale targets, '
            'so the edges are audit history but accumulate over time. '
            'Inspect the source unit and reap the orphan edges through the '
            'lint dashboard (manual review required; no automated reaping).'
        ),
        select_sql=_ORPHAN_CONTRADICTS_LINKS_POST_STALE_SQL,
    ),
    RuleSpec(
        name='composite_deprioritize_candidate',
        lint_type=LintType.QUALITY,
        target_type='memory_unit',
        suggested_action=(
            'FSFM composite score above the propose threshold. Inspect evidence '
            "(see ``flag_reason``) and call memex_memory_deprioritize with reason='fsfm composite'. "
            'Findings with ``flag_reason`` ∈ {high_mw_with_nonmw_pressure, components_disagree, '
            'low_credibility_contradiction_only} are escalation-only — auto-band skips them.'
        ),
        select_sql=_FSFM_COMPOSITE_DEPRIORITIZE_SQL,
        param_keys=(
            'lambda_link',
            'mu_entity',
            'weight_graph',
            'weight_mw',
            'weight_temporal',
            'weight_entity',
            'propose_threshold',
            'high_mw_threshold',
            'high_mw_min_outcomes',
            'disagreement_range',
            'contradicted_low_credibility_max',
        ),
    ),
    RuleSpec(
        name='claim_too_aggressive',
        lint_type=LintType.QUALITY,
        target_type='memory_unit',
        suggested_action=(
            'Explicit-claim unit produced more contradiction/weaken links '
            'in one pass than the configured ceiling. Review for over-matching '
            '(too-broad target_topic or stale-prior bleed) and consider tightening '
            'the explicit-claim similarity threshold.'
        ),
        select_sql=_CLAIM_TOO_AGGRESSIVE_SQL,
        param_keys=('claim_too_aggressive_max_links',),
    ),
)


_INSERT_FINDING_SQL = text("""
    INSERT INTO maintenance_proposals (
        vault_id, lint_type, target_type, target_id,
        rule_name, evidence, suggested_action, status, source
    )
    VALUES (
        :vault_id, :lint_type, :target_type, :target_id,
        :rule_name, CAST(:evidence AS jsonb), :suggested_action, 'pending', 'rule'
    )
    ON CONFLICT (rule_name, target_type, target_id, vault_id)
    WHERE status = 'pending'
    DO NOTHING
""")


@dataclass
class RuleRunResult:
    rule_name: str
    lint_type: LintType
    findings_emitted: int
    duration_seconds: float
    error: str | None = None


@dataclass
class LintRunSummary:
    vault_id: UUID
    rules: list[RuleRunResult]

    @property
    def total_findings(self) -> int:
        return sum(r.findings_emitted for r in self.rules)


class LintService(BaseService):
    """Runs the v1 rule set and writes findings to ``maintenance_proposals``."""

    async def run_rules(
        self,
        vault_id: UUID,
        *,
        rules: tuple[RuleSpec, ...] = V1_RULES,
    ) -> LintRunSummary:
        """Execute every registered rule against ``vault_id`` and persist findings.

        Returns a :class:`LintRunSummary` with per-rule counts. Idempotent on
        reruns thanks to the partial unique index — already-pending findings
        for the same ``(rule_name, target_type, target_id, vault_id)`` are
        silently skipped.
        """
        results: list[RuleRunResult] = []
        async with self.metastore.session() as session:
            # SAVEPOINT loop: per-rule ``begin_nested`` isolates a failing rule's
            # rollback from previously-emitted findings. Caveat: an asyncpg
            # *protocol* error (vs. a logical SQL error like a constraint
            # violation) can leave the underlying connection in an unusable
            # state — the SAVEPOINT rollback succeeds logically, but the outer
            # ``commit()`` below may still fail and lose findings from earlier
            # successful rules. The outer commit is therefore wrapped in a
            # try/except that warns (with the count of successful rules) and
            # re-raises so the caller still sees the tick failed.
            for spec in rules:
                # Per-rule SAVEPOINT so a failing rule rolls back only its own
                # findings — successful rules still persist on the outer commit.
                # Fallback timer for the error path; happy-path uses the
                # ``duration_seconds`` value computed inside ``_run_one``.
                start = time.perf_counter()
                try:
                    async with session.begin_nested():
                        results.append(await self._run_one(session, spec, vault_id))
                except Exception as exc:
                    logger.exception(
                        'Lint rule %s failed for vault %s; continuing with remaining rules',
                        spec.name,
                        vault_id,
                    )
                    # Record the failure in the summary so callers can
                    # distinguish "rule ran with no findings" from "rule did
                    # not complete". The SAVEPOINT has already rolled back
                    # any partial inserts for this rule.
                    results.append(
                        RuleRunResult(
                            rule_name=spec.name,
                            lint_type=spec.lint_type,
                            findings_emitted=0,
                            duration_seconds=time.perf_counter() - start,
                            error=str(exc),
                        )
                    )
                    continue
            try:
                await session.commit()
            except Exception as exc:
                # The outer commit failed — findings from successful rules
                # were NOT persisted. Name the variables accordingly:
                # ``findings_at_risk`` is the count of findings that were
                # emitted by successful rules but lost when the commit
                # failed. ``successful_rule_count`` counts every rule that
                # completed without raising, including those that emitted
                # zero findings (still a meaningful "ran cleanly" signal).
                findings_at_risk = sum(r.findings_emitted for r in results if r.error is None)
                successful_rule_count = sum(1 for r in results if r.error is None)
                logger.warning(
                    'lint tick: outer commit failed after per-rule SAVEPOINTs; '
                    'findings from successful rules may have been lost '
                    '(findings_at_risk=%d, successful_rules=%d, error=%s)',
                    findings_at_risk,
                    successful_rule_count,
                    exc,
                    exc_info=True,
                )
                raise
        return LintRunSummary(vault_id=vault_id, rules=results)

    async def _run_one(
        self,
        session: AsyncSession,
        spec: RuleSpec,
        vault_id: UUID,
    ) -> RuleRunResult:
        from memex_core import metrics

        start = time.perf_counter()
        attrs = {
            'lint.rule_name': spec.name,
            'lint.lint_type': spec.lint_type.value,
            'lint.vault_id': str(vault_id),
        }

        emitted = 0
        gate = self.config.server.memory.lint.confidence_gate
        gate_active = spec.target_type == 'memory_unit' and gate.is_active()
        ctx = (
            _tracer.start_as_current_span('memex.lint.run_rule', attributes=attrs)
            if _tracer
            else None
        )
        try:
            if ctx is not None:
                ctx.__enter__()
            params: dict[str, Any] = {'vault_id': str(vault_id)}
            if spec.param_keys:
                params.update(self._resolve_rule_params(spec.param_keys))
            result = await session.execute(text(spec.select_sql), params)
            rows = result.mappings().all()
            # Bulk-fetch (confidence, evidence_count) for every candidate
            # so the gate predicate runs against an in-memory map rather than
            # firing one SELECT per row (former N+1; now bulk-loaded).
            confidence_map: dict[str, tuple[float, int]] = {}
            if gate_active and rows:
                # Cast through ``str()`` even though
                # every rule SQL uses ``id::text AS target_id`` — asyncpg
                # may surface ``uuid.UUID`` objects under some text() row
                # adapters, and ``UUID(...) not in {str: ...}`` returns
                # False, silently disabling the gate. The cast is a
                # one-line guard against that drift class. Bulk-load keys
                # are ``id::text`` (always str), so consumer-side normalise
                # to str at the boundary.
                target_ids = [str(row['target_id']) for row in rows]
                confidence_map = await bulk_load_confidence_map(session, target_ids)
                # Format-mismatch defence. The bulk map
                # keys are `id::text` from PostgreSQL (canonical lowercase,
                # hyphenated UUID). If any rule's `select_sql` somewhere
                # ever returned a different string form, the per-id lookup
                # in ``confidence_map_blocks`` would silently miss and the
                # unit would surface (the documented "missing row → do not
                # block" fallback). Surface the mismatch loudly so it
                # doesn't manifest as a phantom finding.
                missing = [tid for tid in target_ids if tid not in confidence_map]
                if missing:
                    logger.warning(
                        'lint rule %s: confidence-gate bulk map missed %d/%d '
                        'target_ids — these will fall through the gate '
                        '(possibly stale or rule SQL emitting a non-canonical '
                        'UUID string). Sample: %s',
                        spec.name,
                        len(missing),
                        len(target_ids),
                        missing[:5],
                    )
            for row in rows:
                if gate_active and confidence_map_blocks(
                    confidence_map,
                    str(row['target_id']),
                    gate.confidence_min,
                    gate.variance_max,
                ):
                    continue
                ins = await session.execute(
                    _INSERT_FINDING_SQL,
                    {
                        'vault_id': str(vault_id),
                        'lint_type': spec.lint_type.value,
                        'target_type': spec.target_type,
                        'target_id': row['target_id'],
                        'rule_name': spec.name,
                        'evidence': _json_dumps(row['evidence']),
                        'suggested_action': spec.suggested_action,
                    },
                )
                if ins.rowcount:
                    emitted += 1
            metrics.LINT_FINDINGS_TOTAL.labels(
                rule_name=spec.name,
                lint_type=spec.lint_type.value,
                vault_id=str(vault_id),
            ).inc(emitted)
        except Exception:
            logger.exception('Lint rule %s failed', spec.name)
            raise
        finally:
            duration = time.perf_counter() - start
            metrics.LINT_RUN_DURATION_SECONDS.labels(rule_name=spec.name).observe(duration)
            if ctx is not None:
                ctx.__exit__(None, None, None)

        logger.info(
            'lint rule %s emitted %d findings in vault %s (%.3fs)',
            spec.name,
            emitted,
            vault_id,
            duration,
        )
        return RuleRunResult(
            rule_name=spec.name,
            lint_type=spec.lint_type,
            findings_emitted=emitted,
            duration_seconds=duration,
        )

    def _resolve_rule_params(self, keys: tuple[str, ...]) -> dict[str, Any]:
        """Resolve a rule's named bind parameters from the live config.

        Centralises the mapping from ``RuleSpec.param_keys`` to live config
        values so the rule SQL stays declarative. Adding a new rule knob
        requires (1) adding the field to the relevant pydantic config
        block and (2) extending the ``known`` dict below — drift between
        the declared key and the config field will fail loudly via
        :class:`KeyError` instead of silently falling back.
        """
        cfg = self.config.server.memory.deprioritize_score
        contradiction_cfg = self.config.server.contradiction
        known: dict[str, Any] = {
            'lambda_link': cfg.lambda_link,
            'mu_entity': cfg.mu_entity,
            'weight_graph': cfg.weights.graph,
            'weight_mw': cfg.weights.mw,
            'weight_temporal': cfg.weights.temporal,
            'weight_entity': cfg.weights.entity,
            'propose_threshold': cfg.thresholds.propose,
            'high_mw_threshold': cfg.thresholds.high_mw_threshold,
            'high_mw_min_outcomes': cfg.thresholds.high_mw_min_outcomes,
            'disagreement_range': cfg.thresholds.disagreement_range,
            'contradicted_low_credibility_max': cfg.thresholds.contradicted_low_credibility_max,
            'claim_too_aggressive_max_links': contradiction_cfg.claim_too_aggressive_max_links,
        }
        try:
            return {k: known[k] for k in keys}
        except KeyError as exc:
            raise KeyError(
                f'Unknown lint-rule param {exc.args[0]!r}; extend '
                f'LintService._resolve_rule_params (known keys: {sorted(known)}).'
            ) from None

    async def count_pending(self, vault_id: UUID | None = None) -> int:
        """Count pending findings.

        - ``vault_id is None`` → global scope (only ``vault_id IS NULL`` rows).
        - ``vault_id is not None`` → that vault only.

        Total-across-everything is intentionally not exposed here; the server
        handles ``scope='all'`` with its own SQL.
        """
        if vault_id is None:
            stmt = text(
                'SELECT count(*) FROM maintenance_proposals '
                "WHERE status = 'pending' AND vault_id IS NULL"
            )
            params: dict[str, Any] = {}
        else:
            stmt = text(
                'SELECT count(*) FROM maintenance_proposals '
                "WHERE status = 'pending' AND vault_id = :v"
            )
            params = {'v': str(vault_id)}

        async with self.metastore.session() as session:
            result = await session.execute(stmt, params)
            return int(result.scalar() or 0)

    async def get_finding_vault_id(self, finding_id: UUID) -> tuple[bool, UUID | None]:
        """Return the vault_id (or ``None`` for global findings) for a finding.

        Returns ``(found, vault_id)``. ``found=False`` means no row exists with
        that id — the route layer maps that to 404. ``found=True`` with
        ``vault_id=None`` means a global finding (no vault scope), which the
        per-vault gate treats as unrestricted.
        """
        async with self.metastore.session() as session:
            result = await session.execute(
                text('SELECT vault_id FROM maintenance_proposals WHERE id = :id'),
                {'id': str(finding_id)},
            )
            row = result.first()
            if row is None:
                return (False, None)
            return (True, row[0])

    async def set_status(
        self,
        finding_id: UUID,
        new_status: str,
        *,
        actor: str | None = None,
        vault_id: UUID | None = None,
    ) -> bool:
        """Flip a finding's status to ``resolved`` or ``dismissed``.

        Returns True iff one row was updated; the finding must currently be
        ``pending``. Idempotent: hitting an already-resolved/dismissed row
        returns False without raising. ``actor`` is recorded in
        ``resolved_by`` for traceability — pass the agent name or operator
        id; ``None`` keeps the column NULL.

        When ``vault_id`` is supplied the UPDATE constrains by that vault as
        well — defense-in-depth so a route bypass cannot mutate a finding
        owned by a different vault (cross-vault check). Pass ``vault_id=None`` to
        preserve legacy in-process callers (e.g. background jobs that have
        already authenticated higher up the stack).
        """
        if new_status not in ('resolved', 'dismissed'):
            raise ValueError(f"new_status must be 'resolved' or 'dismissed', got {new_status!r}")

        params: dict[str, Any] = {
            'new': new_status,
            'id': str(finding_id),
            'actor': actor,
        }
        where_extra = ''
        if vault_id is not None:
            where_extra = ' AND vault_id = :vault_id'
            params['vault_id'] = str(vault_id)

        async with self.metastore.session() as session:
            # Safety invariant for S608: ``where_extra`` is either '' or the
            # literal string ' AND vault_id = :vault_id' (set on lines 517-520).
            # No user-controlled value is ever interpolated into the SQL
            # string — ``vault_id``, ``new_status``, ``finding_id``, and
            # ``actor`` all flow through the bound ``params`` dict via :name
            # placeholders. ``new_status`` is allowlist-validated on L509.
            #
            # noqa placement: ruff anchors S608 on the FIRST physical line of
            # the multi-line concatenated string (line 531), so the noqa below
            # is on the correct line. Verified by stripping the marker — ruff
            # reports `lint.py:531:21` and `--^` underlines through L533.
            result = await session.execute(
                text(
                    'UPDATE maintenance_proposals '  # noqa: S608
                    'SET status = :new, resolved_at = now(), resolved_by = :actor '
                    f"WHERE id = :id AND status = 'pending'{where_extra}"
                ),
                params,
            )
            await session.commit()
            return bool(result.rowcount)

    async def get_findings(
        self,
        *,
        vault_id: UUID | None = None,
        lint_type: str | None = None,
        target_type: str | None = None,
        status: str = 'pending',
        limit: int = 20,
        cursor: str | None = None,
    ) -> LintFindingsPage:
        """Query the maintenance ledger with filters + cursor pagination.

        Read-only. Returns a :class:`LintFindingsPage` with shape-stable
        fields: ``findings`` is always a list (possibly empty) and
        ``next_cursor`` is always a string-or-None (never missing). Ordered
        by ``(created_at DESC, id DESC)`` so the cursor is total-order under
        concurrent inserts.

        Acceptance criteria path — when the ``maintenance_proposals`` table is missing
        (e.g. the endpoint ran before the migration applied) raises
        :class:`LintSubsystemNotInitializedError`; the MCP/HTTP layer maps
        that to the documented error envelope.
        """
        if status not in ('pending', 'resolved', 'dismissed'):
            raise ValueError(f'status must be one of pending|resolved|dismissed, got {status!r}')
        if limit < 1:
            raise ValueError(f'limit must be >= 1, got {limit}')
        capped_limit = min(limit, _MAX_LIMIT)

        cursor_ts: datetime | None = None
        cursor_id: UUID | None = None
        if cursor:
            decoded = _decode_cursor(cursor)
            if decoded is not None:
                cursor_ts, cursor_id = decoded

        # Fetch limit+1 so we can tell whether another page exists without a
        # second round-trip count. Pre-compute the value rather than doing
        # arithmetic on a bound parameter — `:limit + 1` inside SQL leaves the
        # `+ 1` as a literal addition the driver may reject or interpret oddly.
        fetch_limit = capped_limit + 1
        clauses: list[str] = ['status = :status']
        params: dict[str, Any] = {'status': status, 'fetch_limit': fetch_limit}
        if vault_id is not None:
            clauses.append('vault_id = CAST(:vault_id AS uuid)')
            params['vault_id'] = str(vault_id)
        if lint_type is not None:
            clauses.append('lint_type = :lint_type')
            params['lint_type'] = lint_type
        if target_type is not None:
            clauses.append('target_type = :target_type')
            params['target_type'] = target_type
        if cursor_ts is not None and cursor_id is not None:
            clauses.append('(created_at, id) < (:cursor_ts, CAST(:cursor_id AS uuid))')
            params['cursor_ts'] = cursor_ts
            params['cursor_id'] = str(cursor_id)

        # Safety invariant for S608: every entry appended to ``clauses`` (lines
        # 575-589) is a hard-coded literal SQL fragment — no f-string contains
        # a value derived from ``status``, ``vault_id``, ``lint_type``,
        # ``target_type``, or ``cursor``. All user-controlled values flow
        # exclusively through the ``params`` dict via :name bind parameters.
        # ``status`` is allowlist-validated on L557 and ``limit`` is bounded on
        # L559-561, so ``where_sql`` is provably constructed from a closed set
        # of literal strings.
        #
        # noqa placement: ruff anchors S608 on the FIRST physical line of the
        # multi-line concatenated string (the line below), so the noqa is on
        # the correct line. Verified by stripping the marker — ruff reports
        # `lint.py:611:13` (the `f'SELECT ...'` line) and `--^` underlines
        # through the closing string.
        where_sql = ' AND '.join(clauses)
        stmt = text(
            f'SELECT id, vault_id, lint_type, target_type, target_id, rule_name, '  # noqa: S608
            f'evidence, suggested_action, status, source, created_at, resolved_at, '
            f'resolved_by '
            f'FROM maintenance_proposals WHERE {where_sql} '
            'ORDER BY created_at DESC, id DESC LIMIT :fetch_limit'
        )

        async with self.metastore.session() as session:
            try:
                result = await session.execute(stmt, params)
                rows = result.mappings().all()
            except ProgrammingError as exc:
                if 'maintenance_proposals' in str(exc).lower():
                    raise LintSubsystemNotInitializedError() from exc
                raise

        findings = [LintFindingDTO.from_row(r) for r in rows[:capped_limit]]
        has_more = len(rows) > capped_limit
        next_cursor = (
            _encode_cursor(findings[-1].created_at, findings[-1].finding_id)
            if has_more and findings
            else None
        )
        return LintFindingsPage(findings=findings, next_cursor=next_cursor)


def _json_dumps(value: Any) -> str:
    """Serialise rule-emitted evidence to a JSON string for the insert.

    pgvector's asyncpg adapter emits JSONB rows as Python dicts; we round-trip
    through ``json.dumps`` so the INSERT can ``CAST(... AS jsonb)`` cleanly.
    """
    from datetime import date, datetime
    from uuid import UUID as _UUID

    def default(o: Any) -> Any:
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        if isinstance(o, _UUID):
            return str(o)
        raise TypeError(f'unhandled type {type(o).__name__}')

    return json.dumps(value, default=default)
