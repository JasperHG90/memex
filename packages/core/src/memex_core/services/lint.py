"""F6 maintenance ledger — rule-based linter.

Implements ``LintService.run_rules(vault_id?)`` which iterates the v1 rule
registry and emits ``MaintenanceProposal`` rows. Rules are read-only against
the resources they inspect; the only writes ``run_rules`` performs are
``INSERT INTO maintenance_proposals`` (with ``ON CONFLICT DO NOTHING`` against
the partial unique index ``uq_maintenance_proposals_pending``).

v1 rule set (one per ``LintType`` enum value, per AC-F6-6):

  - ``orphan_mental_model``         (structural)
  - ``cold_low_mw_unit``            (quality)
  - ``sensitive_unreviewed_unit``   (governance, AC-F6-G1)
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

try:
    from opentelemetry import trace as _otel_trace

    _tracer = _otel_trace.get_tracer('memex.lint')
except Exception:  # pragma: no cover — OTel optional at import time
    _tracer = None

logger = logging.getLogger('memex.core.services.lint')


# ---------------------------------------------------------------------------
# F8 — Read-side DTOs, cursor codec, and table-not-found signal
# ---------------------------------------------------------------------------


_MAX_LIMIT = 200


class LintSubsystemNotInitializedError(RuntimeError):
    """Signals AC-F8-5: ``maintenance_proposals`` table is missing.

    The MCP/HTTP layer translates this into the documented error envelope
    so the agent gets a clear, actionable message ("run alembic upgrade").
    """

    def __init__(self) -> None:
        super().__init__(
            'F6 maintenance_proposals table is missing. '
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
        )


class LintFindingsPage(BaseModel):
    """Shape-stable page envelope returned by :meth:`LintService.get_findings`.

    ``findings`` is always present (possibly empty); ``next_cursor`` is
    always present (string or None). The agent surface (F8) MUST round-trip
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
    about its shape, and a stale cursor (e.g. across F8 redeploys)
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
    """

    name: str
    lint_type: LintType
    target_type: str
    suggested_action: str
    select_sql: str


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
"""


_SENSITIVE_UNREVIEWED_UNIT_SQL = """
    SELECT
        mu.id::text AS target_id,
        jsonb_build_object(
            'risk_class', mu.risk_class,
            'created_at', mu.created_at,
            'last_review_at', NULL
        ) AS evidence
    FROM memory_units mu
    WHERE mu.vault_id = :vault_id
      AND mu.status = 'active'
      AND mu.risk_class = 'sensitive'
      AND mu.is_deprioritized = false
      AND NOT EXISTS (
          SELECT 1
          FROM audit_logs al
          WHERE al.action = 'memory_review'
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
            'Unit has 5+ outcomes, low MW score, and 30+ days of inactivity. '
            "Call memex_memory_deprioritize with reason='low MW after 5+ outcomes'."
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
            for spec in rules:
                results.append(await self._run_one(session, spec, vault_id))
            await session.commit()
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
        ctx = (
            _tracer.start_as_current_span('memex.lint.run_rule', attributes=attrs)
            if _tracer
            else None
        )
        try:
            if ctx is not None:
                ctx.__enter__()
            result = await session.execute(text(spec.select_sql), {'vault_id': str(vault_id)})
            for row in result.mappings().all():
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

    async def set_status(
        self,
        finding_id: UUID,
        new_status: str,
    ) -> bool:
        """Flip a finding's status to ``resolved`` or ``dismissed``.

        Returns True iff one row was updated; the finding must currently be
        ``pending``. Idempotent: hitting an already-resolved/dismissed row
        returns False without raising.
        """
        if new_status not in ('resolved', 'dismissed'):
            raise ValueError(f"new_status must be 'resolved' or 'dismissed', got {new_status!r}")

        async with self.metastore.session() as session:
            result = await session.execute(
                text(
                    'UPDATE maintenance_proposals SET status = :new, resolved_at = now() '
                    "WHERE id = :id AND status = 'pending'"
                ),
                {'new': new_status, 'id': str(finding_id)},
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

        AC-F8-5 path — when the ``maintenance_proposals`` table is missing
        (e.g. F8 ran before F6's migration applied) raises
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

        where_sql = ' AND '.join(clauses)
        stmt = text(
            f'SELECT id, vault_id, lint_type, target_type, target_id, rule_name, '
            f'evidence, suggested_action, status, source, created_at, resolved_at '
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
    import json
    from datetime import date, datetime
    from uuid import UUID as _UUID

    def default(o: Any) -> Any:
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        if isinstance(o, _UUID):
            return str(o)
        raise TypeError(f'unhandled type {type(o).__name__}')

    return json.dumps(value, default=default)
