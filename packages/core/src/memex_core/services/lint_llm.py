"""F10 surprise-gated LLM-assisted lint service.

Implements ``LintLLMService.maybe_run`` which orchestrates:

1. Surprise gate (skip below ``surprise_threshold``).
2. Rolling-24h cost cap atomic check + increment.
3. Defer-not-drop semantics for over-cap requests
   (``MaintenanceProposal(rule_name='llm_deferred')``).
4. Invocation of the caller-supplied ``run_llm_check`` to produce a
   :class:`LLMLintFinding`, written to ``maintenance_proposals`` with
   ``source='llm'``.

The DSPy signatures (``CheckSemanticContradiction``, ``CheckSchemaDrift``)
that produce findings live in subsequent commits (4-5); ``maybe_run`` accepts
the check as an injected callable so this module is independently testable.

The deferred queue is itself capped at ``deferred_queue_cap`` rows per
vault; oldest rows beyond the cap are evicted (``status='dismissed'``,
``resolved_at=now()``) with a warning span.

References: RFC-006 §"Cost-cap implementation", §"Trigger surface".
"""

from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID
from weakref import WeakKeyDictionary

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from memex_core.memory.lint_llm.polarity import (
    DEFAULT_POLARITY_THRESHOLD,
    PolarityClassifier,
    gate_passes,
)
from memex_core.memory.lint_llm.surprise import compute_unit_surprise
from memex_core.memory.lint_llm.types import (
    CheckContext,
    LLMLintFinding,
    PolarityResult,
    RunLLMCheck,
)
from memex_core.services.base import BaseService

__all__ = [
    'LLMLintFinding',
    'LintLLMService',
    'LintLLMTickSummary',
    'MaybeRunOutcome',
    'RunLLMCheck',
]

try:
    from opentelemetry import trace as _otel_trace

    _tracer = _otel_trace.get_tracer('memex.lint_llm')
except Exception:  # pragma: no cover — OTel optional at import time
    _tracer = None

logger = logging.getLogger('memex.core.services.lint_llm')


_RULE_LLM_DEFERRED = 'llm_deferred'
_DEFER_REASON_COST_CAP = 'cost_cap_exceeded'
_DEFER_REASON_QUEUE_CAP = 'deferred_queue_cap_exceeded'


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass
class MaybeRunOutcome:
    """Result of a single ``LintLLMService.maybe_run`` call.

    Useful for tests, metrics, and the scheduler tick summary.
    """

    skipped_below_threshold: bool = False
    skipped_disabled: bool = False
    skipped_confidence_gate: bool = False
    deferred: bool = False
    finding_emitted: bool = False
    surprise_score: float | None = None
    polarity_invoked: bool = False
    polarity_rate_limited: bool = False
    polarity_model_failed: bool = False
    polarity_contradiction_prob: float | None = None


@dataclass
class LintLLMTickSummary:
    """Per-vault summary of an F10 scheduler tick."""

    vault_id: UUID
    candidates_evaluated: int = 0
    findings_emitted: int = 0
    deferred: int = 0
    skipped_below_threshold: int = 0
    skipped_confidence_gate: int = 0
    deferred_processed: int = 0


# ---------------------------------------------------------------------------
# SQL fragments
# ---------------------------------------------------------------------------


_SUM_LAST_24H_SQL = text("""
    SELECT COALESCE(SUM(count), 0) AS used
    FROM lint_llm_quota
    WHERE vault_id = :vault_id
      AND hour_bucket >= :cutoff
""")


_UPSERT_HOUR_BUCKET_SQL = text("""
    INSERT INTO lint_llm_quota (id, vault_id, hour_bucket, count)
    VALUES (gen_random_uuid(), :vault_id, :hour_bucket, 1)
    ON CONFLICT (vault_id, hour_bucket)
    DO UPDATE SET count = lint_llm_quota.count + 1
""")


# Atomic check-and-increment for the rolling 24h cost cap. The CTE first
# computes the current rolling sum (over the partial-index range) and then
# attempts an INSERT ... ON CONFLICT keyed on (vault_id, hour_bucket) ONLY
# when that sum is strictly below the cap. Because Postgres serialises
# ON CONFLICT resolution on `uq_lint_llm_quota_vault_hour`, two writers
# observing the same `used` value cannot both increment past the cap: the
# loser's UPDATE branch sees the post-increment row and the WHERE predicate
# `lint_llm_quota.count < (cap - other_buckets)` rejects the second write.
# The RETURNING clause is empty when neither INSERT nor UPDATE fires, which
# the caller treats as "over cap".
_ATOMIC_CHECK_AND_INCREMENT_SQL = text("""
    WITH other_buckets AS (
        SELECT COALESCE(SUM(count), 0) AS used
        FROM lint_llm_quota
        WHERE vault_id = :vault_id
          AND hour_bucket >= :cutoff
          AND hour_bucket < :hour_bucket
    ),
    ins AS (
        INSERT INTO lint_llm_quota (id, vault_id, hour_bucket, count)
        SELECT gen_random_uuid(), :vault_id, :hour_bucket, 1
        FROM other_buckets
        WHERE other_buckets.used < :cap
        ON CONFLICT (vault_id, hour_bucket)
        DO UPDATE SET count = lint_llm_quota.count + 1
        WHERE lint_llm_quota.count + (
            SELECT used FROM other_buckets
        ) < :cap
        RETURNING count
    )
    SELECT count FROM ins
""")


# NOTE: The ON CONFLICT clauses below rely on the partial unique index
# `uq_maintenance_proposals_pending` on
# `maintenance_proposals (rule_name, target_type, target_id, vault_id)
#  WHERE status = 'pending'`,
# created in alembic migration 025_maintenance_proposals.py. The predicate
# in `WHERE status = 'pending'` here MUST match the index predicate exactly
# for Postgres to use it as the conflict arbiter; if migration 025 is ever
# changed, these clauses must be updated in lockstep.
_INSERT_LLM_FINDING_SQL = text("""
    INSERT INTO maintenance_proposals (
        vault_id, lint_type, target_type, target_id,
        rule_name, evidence, suggested_action, status, source
    )
    VALUES (
        :vault_id, :lint_type, :target_type, :target_id,
        :rule_name, CAST(:evidence AS jsonb), :suggested_action, 'pending', 'llm'
    )
    ON CONFLICT (rule_name, target_type, target_id, vault_id)
    WHERE status = 'pending'
    DO NOTHING
""")


# Same partial-unique-index dependency as _INSERT_LLM_FINDING_SQL above —
# see note there. Index lives in migration 025_maintenance_proposals.py.
_INSERT_DEFERRED_SQL = text("""
    INSERT INTO maintenance_proposals (
        vault_id, lint_type, target_type, target_id,
        rule_name, evidence, suggested_action, status, source
    )
    VALUES (
        :vault_id, 'quality', 'memory_unit', :target_id,
        :rule_name, CAST(:evidence AS jsonb), :suggested_action, 'pending', 'llm'
    )
    ON CONFLICT (rule_name, target_type, target_id, vault_id)
    WHERE status = 'pending'
    DO NOTHING
""")


_COUNT_DEFERRED_SQL = text("""
    SELECT count(*) AS n
    FROM maintenance_proposals
    WHERE vault_id = :vault_id
      AND rule_name = :rule_name
      AND status = 'pending'
""")


_SELECT_DEFERRED_FIFO_SQL = text("""
    SELECT id, target_id, evidence
    FROM maintenance_proposals
    WHERE vault_id = :vault_id
      AND rule_name = :rule_name
      AND status = 'pending'
    ORDER BY created_at ASC, id ASC
    LIMIT :limit
""")


_DISMISS_DEFERRED_SQL = text("""
    UPDATE maintenance_proposals
    SET status = 'dismissed', resolved_at = now(), resolved_by = 'lint_llm'
    WHERE id = :id
""")


_EVICT_OLDEST_DEFERRED_SQL = text("""
    UPDATE maintenance_proposals
    SET status = 'dismissed', resolved_at = now(), resolved_by = 'lint_llm'
    WHERE id IN (
        SELECT id FROM maintenance_proposals
        WHERE vault_id = :vault_id
          AND rule_name = :rule_name
          AND status = 'pending'
        ORDER BY created_at ASC, id ASC
        LIMIT :n
    )
""")


_LOAD_UNIT_AND_TOP_PEER_TEXT_SQL = text("""
    WITH self AS (
        SELECT id, text, embedding
        FROM memory_units
        WHERE id = :unit_id
          AND status = 'active'
    )
    SELECT
        (SELECT text FROM self) AS unit_text,
        (
            SELECT m.text
            FROM memory_units m, self
            WHERE m.vault_id = :vault_id
              AND m.id != :unit_id
              AND m.status = 'active'
              AND m.embedding IS NOT NULL
              AND self.embedding IS NOT NULL
            ORDER BY (m.embedding <=> self.embedding)
            LIMIT 1
        ) AS peer_text
""")


_SELECT_TICK_CANDIDATES_SQL = text("""
    SELECT m.id
    FROM memory_units m
    WHERE m.vault_id = :vault_id
      AND m.status = 'active'
      AND m.embedding IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM maintenance_proposals p
          WHERE p.target_type = 'memory_unit'
            AND p.target_id = m.id::text
            AND p.vault_id = :vault_id
            AND p.source = 'llm'
            AND p.status = 'pending'
      )
    ORDER BY m.created_at DESC, m.id DESC
    LIMIT :limit
""")


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


def _truncate_to_hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


_CONTEXT_AWARE_CHECK_CACHE: 'WeakKeyDictionary[Any, bool]' = WeakKeyDictionary()


def _check_accepts_context(run_llm_check: RunLLMCheck) -> bool:
    """Return True iff ``run_llm_check`` accepts a ``context`` kwarg.

    Result is cached per-callable (weakly) so signature introspection only
    runs once per check. Falls back to ``False`` when the signature cannot be
    introspected (e.g. C-implemented callables): degrade safely to the legacy
    3-arg call shape rather than risk an opaque ``TypeError: got an unexpected
    keyword argument 'context'`` that is indistinguishable from a genuine bug
    inside the check body.
    """
    cached = _CONTEXT_AWARE_CHECK_CACHE.get(run_llm_check)
    if cached is not None:
        return cached
    try:
        sig = inspect.signature(run_llm_check)
    except (TypeError, ValueError):
        accepts = False
    else:
        params = sig.parameters
        accepts = 'context' in params or any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
    try:
        _CONTEXT_AWARE_CHECK_CACHE[run_llm_check] = accepts
    except TypeError:
        pass
    return accepts


async def _invoke_check(
    run_llm_check: RunLLMCheck,
    unit_id: UUID,
    vault_id: UUID,
    session: AsyncSession,
    context: CheckContext,
) -> LLMLintFinding | None:
    """Invoke ``run_llm_check`` with backward-compatible context plumbing.

    F10's original check signature was ``(unit_id, vault_id, session)``; F10b
    adds an optional ``context`` kwarg so the cosine-OR-polarity gate can pass
    the precomputed :class:`PolarityResult` through to the DSPy signature
    without re-invoking the NLI model. Existing 3-arg checks (and existing
    tests that mock 3-arg lambdas) continue to work unchanged.

    Dispatch is decided up front by ``inspect.signature`` (cached per-callable)
    rather than by catching ``TypeError`` from the call site, so genuine
    ``TypeError``s raised inside the check body are not silently swallowed.
    """
    if _check_accepts_context(run_llm_check):
        return await run_llm_check(unit_id, vault_id, session, context=context)
    return await run_llm_check(unit_id, vault_id, session)


class LintLLMService(BaseService):
    """Surprise-gated LLM lint service for F10.

    Dependency-injects ``run_llm_check`` so DSPy signatures (commits 4-5) can
    plug in without touching this module. Every public coroutine is
    idempotent at the SQL level: re-running ``maybe_run`` for the same unit
    is safe (the partial-unique index on ``maintenance_proposals`` prevents
    duplicate pending findings; the quota UPSERT is keyed on
    ``(vault_id, hour_bucket)``).
    """

    @property
    def _settings(self):
        return self.config.server.memory.lint_llm

    # -- quota -----------------------------------------------------------

    async def quota_used(self, vault_id: UUID, *, session: AsyncSession) -> int:
        """Return the rolling-24h LLM lint call count for ``vault_id``."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        result = await session.execute(
            _SUM_LAST_24H_SQL,
            {'vault_id': str(vault_id), 'cutoff': cutoff},
        )
        row = result.one()
        return int(row.used)

    async def check_and_increment_quota(
        self,
        vault_id: UUID,
        *,
        session: AsyncSession,
    ) -> bool:
        """Atomic 24h-rolling quota check + increment.

        Returns True if the call was admitted (count incremented), False if
        the cap was exhausted. Caller is responsible for committing the
        session — ``maybe_run`` does so, the integration tests do too.

        Atomicity: implemented as a single SQL statement that combines the
        rolling-window sum and the ON CONFLICT increment. Two concurrent
        writers cannot both push the rolling count past ``cap`` because
        Postgres serialises ON CONFLICT resolution on
        ``uq_lint_llm_quota_vault_hour`` and the UPDATE branch's WHERE
        predicate re-checks the (now post-increment) total against the cap.
        Eliminates the read-then-write TOCTOU window present in the prior
        implementation (PR #101 c4 HIGH-1).
        """
        cap = self._settings.cost_cap_per_24h
        if cap <= 0:
            return False

        now = datetime.now(timezone.utc)
        hour_bucket = _truncate_to_hour(now)
        cutoff = now - timedelta(hours=24)

        result = await session.execute(
            _ATOMIC_CHECK_AND_INCREMENT_SQL,
            {
                'vault_id': str(vault_id),
                'hour_bucket': hour_bucket,
                'cutoff': cutoff,
                'cap': cap,
            },
        )
        return result.first() is not None

    # -- defer queue -----------------------------------------------------

    async def _evict_excess_deferred(
        self,
        vault_id: UUID,
        *,
        session: AsyncSession,
    ) -> int:
        """Cap the deferred queue at ``deferred_queue_cap`` per vault.

        Marks excess oldest rows as ``dismissed`` (non-destructive, audit-
        preserving) and returns how many were evicted.
        """
        cap = self._settings.deferred_queue_cap
        result = await session.execute(
            _COUNT_DEFERRED_SQL,
            {'vault_id': str(vault_id), 'rule_name': _RULE_LLM_DEFERRED},
        )
        n = int(result.scalar() or 0)
        if n <= cap:
            return 0
        excess = n - cap
        await session.execute(
            _EVICT_OLDEST_DEFERRED_SQL,
            {
                'vault_id': str(vault_id),
                'rule_name': _RULE_LLM_DEFERRED,
                'n': excess,
            },
        )
        logger.warning(
            'F10 deferred queue evicted %d oldest entries for vault %s (cap=%d, was=%d)',
            excess,
            vault_id,
            cap,
            n,
        )
        return excess

    async def defer(
        self,
        unit_id: UUID,
        vault_id: UUID,
        *,
        reason: str,
        surprise_score: float | None,
        session: AsyncSession,
    ) -> None:
        """Persist a deferred-unit row and cap the queue if necessary."""
        evidence: dict[str, Any] = {
            'reason': reason,
            'unit_id': str(unit_id),
        }
        if surprise_score is not None:
            evidence['surprise_score'] = surprise_score

        await session.execute(
            _INSERT_DEFERRED_SQL,
            {
                'vault_id': str(vault_id),
                'target_id': str(unit_id),
                'rule_name': _RULE_LLM_DEFERRED,
                'evidence': json.dumps(evidence),
                'suggested_action': (
                    'Re-run F10 LLM lint on next tick when 24h quota has capacity.'
                ),
            },
        )
        await self._evict_excess_deferred(vault_id, session=session)

    async def list_deferred(
        self,
        vault_id: UUID,
        *,
        limit: int,
        session: AsyncSession,
    ) -> list[tuple[UUID, UUID, dict[str, Any]]]:
        """Return up to ``limit`` deferred rows in FIFO order.

        Each tuple is ``(proposal_id, unit_id, evidence)``.
        """
        if limit <= 0:
            return []
        result = await session.execute(
            _SELECT_DEFERRED_FIFO_SQL,
            {
                'vault_id': str(vault_id),
                'rule_name': _RULE_LLM_DEFERRED,
                'limit': limit,
            },
        )
        out: list[tuple[UUID, UUID, dict[str, Any]]] = []
        for row in result.mappings().all():
            evidence = row['evidence']
            if isinstance(evidence, str):
                evidence = json.loads(evidence)
            out.append((row['id'], UUID(evidence['unit_id']), evidence))
        return out

    async def dismiss_deferred(
        self,
        proposal_id: UUID,
        *,
        session: AsyncSession,
    ) -> None:
        """Mark a deferred row as ``dismissed`` once it has been processed."""
        await session.execute(_DISMISS_DEFERRED_SQL, {'id': proposal_id})

    # -- write finding ---------------------------------------------------

    async def write_finding(
        self,
        finding: LLMLintFinding,
        vault_id: UUID,
        *,
        session: AsyncSession,
    ) -> bool:
        """Insert an ``LLMLintFinding`` as a MaintenanceProposal.

        Returns True if a new row was inserted, False if a duplicate pending
        finding already existed for the same target + rule + vault.
        """
        evidence = {
            'check_type': finding.check_type,
            'surprise_score': finding.surprise_score,
            'explanation': finding.explanation,
            'related_unit_ids': finding.related_unit_ids,
            **finding.extra_evidence,
        }
        result = await session.execute(
            _INSERT_LLM_FINDING_SQL,
            {
                'vault_id': str(vault_id),
                'lint_type': finding.lint_type.value,
                'target_type': finding.target_type,
                'target_id': finding.target_id,
                'rule_name': finding.rule_name,
                'evidence': json.dumps(evidence),
                'suggested_action': finding.suggested_action,
            },
        )
        return bool(result.rowcount)

    # -- orchestration ---------------------------------------------------

    async def maybe_run(
        self,
        unit_id: UUID,
        vault_id: UUID,
        *,
        run_llm_check: RunLLMCheck,
        session: AsyncSession,
        polarity_classifier: PolarityClassifier | None = None,
        confidence_map: dict[str, tuple[float, int]] | None = None,
    ) -> MaybeRunOutcome:
        """Surprise-gate → quota → LLM check → write finding (or defer).

        Single-unit orchestration. Caller (the F10 scheduler tick) commits the
        session after each unit so quota increments are durable even if a
        later unit fails.

        F10b: when ``polarity_classifier`` is supplied AND cosine surprise is
        below the threshold, the service runs an NLI invocation against the
        unit's top-1 nearest peer. If the contradiction-probability crosses
        ``polarity_classifier.polarity_threshold`` the OR'd gate clears and
        the LLM check fires. The NLI invocation is skipped when cosine
        surprise is already at/above the threshold (cheap pre-filter).

        ``confidence_map`` (Hermes round-5 MED): when supplied, the F22
        confidence/variance gate runs against this prefetched
        ``{unit_id_str: (confidence, evidence_count)}`` map instead of
        firing one SELECT per unit. ``tick()`` builds the map once for all
        candidates so the gate scales O(1) extra queries instead of O(N).
        ``None`` falls back to the per-row SELECT for one-off callers.
        """
        outcome = MaybeRunOutcome()
        settings = self._settings

        if not settings.enabled or settings.cost_cap_per_24h <= 0:
            outcome.skipped_disabled = True
            return outcome

        gate = settings.confidence_gate
        gate_active = gate.confidence_min > 0.0 or gate.variance_max < (1.0 / 12.0)
        if gate_active:
            if confidence_map is not None:
                from memex_core.services.lint_confidence import confidence_map_blocks

                if confidence_map_blocks(
                    confidence_map, str(unit_id), gate.confidence_min, gate.variance_max
                ):
                    outcome.skipped_confidence_gate = True
                    return outcome
            else:
                from memex_core.services.lint_confidence import gate_blocks_finding

                if await gate_blocks_finding(
                    session, str(unit_id), gate.confidence_min, gate.variance_max
                ):
                    outcome.skipped_confidence_gate = True
                    return outcome

        score = await compute_unit_surprise(unit_id, vault_id, session, k=settings.surprise_k)
        outcome.surprise_score = score

        polarity_result: PolarityResult | None = None
        polarity_contra_prob: float | None = None
        if polarity_classifier is not None and score < settings.surprise_threshold:
            row = (
                await session.execute(
                    _LOAD_UNIT_AND_TOP_PEER_TEXT_SQL,
                    {'unit_id': str(unit_id), 'vault_id': str(vault_id)},
                )
            ).first()
            if row is not None and row.unit_text and row.peer_text:
                classify_outcome = await polarity_classifier.classify_pair(
                    premise=str(row.unit_text),
                    hypothesis=str(row.peer_text),
                    vault_id=vault_id,
                )
                if classify_outcome.result is not None:
                    polarity_result = classify_outcome.result
                    outcome.polarity_invoked = True
                    polarity_contra_prob = polarity_result.contradiction_prob
                    outcome.polarity_contradiction_prob = polarity_contra_prob
                elif classify_outcome.rate_limited:
                    outcome.polarity_rate_limited = True
                elif classify_outcome.model_failed:
                    outcome.polarity_model_failed = True

        cleared = gate_passes(
            score,
            polarity_contra_prob,
            surprise_threshold=settings.surprise_threshold,
            polarity_threshold=(
                polarity_classifier.polarity_threshold
                if polarity_classifier is not None
                else DEFAULT_POLARITY_THRESHOLD
            ),
        )
        if not cleared:
            outcome.skipped_below_threshold = True
            return outcome

        admitted = await self.check_and_increment_quota(vault_id, session=session)
        if not admitted:
            await self.defer(
                unit_id,
                vault_id,
                reason=_DEFER_REASON_COST_CAP,
                surprise_score=score,
                session=session,
            )
            outcome.deferred = True
            return outcome

        context = CheckContext(polarity=polarity_result)
        finding = await _invoke_check(run_llm_check, unit_id, vault_id, session, context)
        if finding is not None:
            inserted = await self.write_finding(finding, vault_id, session=session)
            outcome.finding_emitted = inserted
        return outcome

    async def process_deferred(
        self,
        vault_id: UUID,
        *,
        run_llm_check: RunLLMCheck,
        session: AsyncSession,
    ) -> int:
        """Drain the deferred queue up to remaining quota. Returns rows processed.

        FIFO over ``created_at, id``. Each successfully processed row is
        dismissed (non-destructive); over-cap rows from a prior tick stay
        deferred until the quota has capacity.

        F10b note: deferred rows are invoked with an empty ``CheckContext``
        (no ``polarity_hint``, no ``polarity_*_prob`` in ``extra_evidence``).
        The polarity result that originally cleared the gate is not persisted
        on the deferred ``MaintenanceProposal``, and re-running NLI on every
        drained row would defeat the per-vault rate-limit (PolarityRateLimiter
        already accounted for the original call). Callers comparing immediate
        vs deferred findings should expect this evidence asymmetry; surfacing
        polarity to deferred findings requires persisting the result on the
        proposal row, which is out of F10b's scope.
        """
        settings = self._settings
        if not settings.enabled or settings.cost_cap_per_24h <= 0:
            return 0

        used = await self.quota_used(vault_id, session=session)
        budget = max(0, settings.cost_cap_per_24h - used)
        if budget == 0:
            return 0

        rows = await self.list_deferred(vault_id, limit=budget, session=session)
        processed = 0
        for proposal_id, unit_id, _evidence in rows:
            # Per-row try/except so a single LLM/storage failure does not
            # poison the rest of the queue (PR #101 c4 HIGH-2). Use a
            # SAVEPOINT so an exception leaves the outer transaction usable
            # for subsequent rows; otherwise the failed statement would
            # abort the surrounding tx and every later iteration would no-op.
            try:
                async with session.begin_nested():
                    admitted = await self.check_and_increment_quota(vault_id, session=session)
                    if not admitted:
                        break
                    finding = await _invoke_check(
                        run_llm_check, unit_id, vault_id, session, CheckContext()
                    )
                    if finding is not None:
                        await self.write_finding(finding, vault_id, session=session)
                    await self.dismiss_deferred(proposal_id, session=session)
            except Exception:
                logger.exception(
                    'F10 process_deferred: LLM check failed for deferred unit %s '
                    '(proposal %s) — skipping and continuing',
                    unit_id,
                    proposal_id,
                )
                continue
            processed += 1
        return processed

    # -- tick (scheduler entry-point) -----------------------------------

    async def list_tick_candidates(
        self,
        vault_id: UUID,
        *,
        limit: int,
        session: AsyncSession,
    ) -> list[UUID]:
        """Return up to ``limit`` candidate units for this tick.

        Selects active units in the vault that have an embedding and do not
        already have a pending ``source='llm'`` MaintenanceProposal. Ordered
        most-recent-first so the freshest content gets audited under the cap.
        """
        if limit <= 0:
            return []
        result = await session.execute(
            _SELECT_TICK_CANDIDATES_SQL,
            {'vault_id': str(vault_id), 'limit': limit},
        )
        return [row.id for row in result]

    async def tick(
        self,
        vault_id: UUID,
        *,
        run_llm_check: RunLLMCheck,
        polarity_classifier: PolarityClassifier | None = None,
    ) -> LintLLMTickSummary:
        """Single F10 scheduler-tick for ``vault_id``.

        Order of operations matches RFC-006 §"Cost-cap implementation":

        1. Process the deferred queue first (so over-cap units from a prior
           tick get budget priority once it's free).
        2. Pick fresh candidate units up to ``units_per_tick``.
        3. For each, ``maybe_run`` (gate → quota → check → write OR defer).

        Each unit's work is wrapped in its own session+commit so partial
        progress is durable across LLM failures. The 24h cost cap is
        enforced atomically via ``check_and_increment_quota``.
        """
        summary = LintLLMTickSummary(vault_id=vault_id)
        settings = self._settings

        if not settings.enabled or settings.cost_cap_per_24h <= 0:
            return summary

        # 1. Drain deferred queue first.
        async with self.metastore.session() as session:
            try:
                summary.deferred_processed = await self.process_deferred(
                    vault_id, run_llm_check=run_llm_check, session=session
                )
                await session.commit()
            except Exception:
                await session.rollback()
                logger.exception('F10 tick: process_deferred failed for vault %s', vault_id)

        # 2. Fresh candidates.
        async with self.metastore.session() as session:
            candidates = await self.list_tick_candidates(
                vault_id, limit=settings.units_per_tick, session=session
            )

        # F22 confidence-gate bulk pre-fetch (Hermes round-5 MED): when the
        # gate is active, hydrate ``(confidence, confidence_evidence_count)``
        # for every candidate up front so per-unit ``maybe_run`` does NOT
        # fire one SELECT per unit. Skipped entirely when the gate is off
        # (the ship default), so the extra query is paid only when the
        # operator opts into the gate.
        gate = self._settings.confidence_gate
        gate_active = gate.confidence_min > 0.0 or gate.variance_max < (1.0 / 12.0)
        confidence_map: dict[str, tuple[float, int]] | None = None
        if gate_active and candidates:
            from memex_core.services.lint_confidence import bulk_load_confidence_map

            async with self.metastore.session() as session:
                confidence_map = await bulk_load_confidence_map(
                    session, [str(uid) for uid in candidates]
                )

        for unit_id in candidates:
            async with self.metastore.session() as session:
                try:
                    outcome = await self.maybe_run(
                        unit_id,
                        vault_id,
                        run_llm_check=run_llm_check,
                        session=session,
                        polarity_classifier=polarity_classifier,
                        confidence_map=confidence_map,
                    )
                    await session.commit()
                except Exception:
                    await session.rollback()
                    logger.exception(
                        'F10 tick: maybe_run failed for unit %s in vault %s',
                        unit_id,
                        vault_id,
                    )
                    continue
            summary.candidates_evaluated += 1
            if outcome.skipped_below_threshold:
                summary.skipped_below_threshold += 1
            if outcome.skipped_confidence_gate:
                summary.skipped_confidence_gate += 1
            if outcome.deferred:
                summary.deferred += 1
            if outcome.finding_emitted:
                summary.findings_emitted += 1
        return summary
