"""Lint auto-learning loop — Layer 5: auto-solve.

When explicitly opted in per-rule via config, the auto-apply hook scans
pending proposals for rules whose:
- historical accept_rate >= ``accept_rate_threshold`` (from telemetry)
- LLM-emitted surprise_score >= ``confidence_threshold``

If both conditions hold AND the daily cap hasn't been hit, the hook
resolves the finding via the proposal-action registry with
``actor='system:auto-learn'``. Every auto-resolution is reversible (the
registry enforces ``action.reversible == True``).

Default state: **OFF**. Must be explicitly enabled per rule in config.

TODO: add unit test coverage for LintAutoApplyService — the sweep logic,
  cap enforcement, and FOR UPDATE lock guard are currently exercised only
  via integration-level paths. Known gap tracked in PR #177 review.
"""

from __future__ import annotations

import json as _json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text

from memex_core.services.base import BaseService

if TYPE_CHECKING:
    from memex_core.api import MemexAPI

logger = logging.getLogger('memex.core.services.lint_auto_apply')


@dataclass(frozen=True)
class AutoApplyRuleConfig:
    """Per-rule auto-apply settings — read from config at startup."""

    enabled: bool = False
    confidence_threshold: float = 0.95
    accept_rate_threshold: float = 0.85
    daily_cap: int = 10
    action: str = 'deprioritize_unit'


@dataclass
class AutoApplyResult:
    """Outcome of one ``auto_apply_sweep`` invocation."""

    vault_id: UUID
    proposals_scanned: int = 0
    auto_applied: int = 0
    skipped_low_confidence: int = 0
    skipped_low_accept_rate: int = 0
    skipped_cap_reached: int = 0
    skipped_not_reversible: int = 0
    errors: int = 0
    details: list[dict[str, Any]] = field(default_factory=list)


_PENDING_FOR_AUTO_APPLY_SQL = """
    SELECT
        id::text AS id,
        rule_name,
        target_type,
        target_id,
        vault_id::text AS vault_id,
        evidence,
        (evidence ->> 'surprise_score')::float AS surprise_score,
        source
    FROM maintenance_proposals
    WHERE vault_id = CAST(:vault_id AS uuid)
      AND status = 'pending'
      AND source = 'llm'
      AND rule_name = :rule_name
    ORDER BY created_at ASC
    LIMIT :limit
"""

_COUNT_AUTO_APPLIED_TODAY_SQL = """
    SELECT count(*)
    FROM maintenance_proposals
    WHERE vault_id = CAST(:vault_id AS uuid)
      AND status = 'resolved'
      AND resolved_by = 'system:auto-learn'
      AND resolved_at >= :today_start
"""


class LintAutoApplyService(BaseService):
    """Auto-resolve high-confidence lint findings when opted in.

    Entry point: ``auto_apply_sweep(vault_id, rule_configs)`` — called by
    the scheduler after each lint tick. ``rule_configs`` is a dict of
    ``{rule_name: AutoApplyRuleConfig}`` built from the operator's config
    at startup.
    """

    async def auto_apply_sweep(
        self,
        vault_id: UUID,
        rule_configs: dict[str, AutoApplyRuleConfig],
        *,
        api: MemexAPI | None = None,
        telemetry_service: Any = None,
    ) -> AutoApplyResult:
        """Scan pending proposals and auto-resolve those that clear the gate.

        ``api`` is required for actions to actually fire. When ``None``, the
        sweep logs a warning and refuses to proceed — we don't ship silent
        no-ops where the proposal says "deprioritized" but the unit stays
        active.
        """
        from memex_core.services.proposal_actions import get_action

        if api is None:
            logger.warning('auto_apply_sweep called without api — refusing to proceed')
            return AutoApplyResult(vault_id=vault_id)

        result = AutoApplyResult(vault_id=vault_id)

        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

        for rule_name, config in rule_configs.items():
            if not config.enabled:
                continue

            # Check daily cap.
            # NOTE: The cap check runs in a separate session from the
            # resolution UPDATE below. This is acceptable because the
            # auto-apply sweep is driven by a single scheduler thread —
            # there is no concurrent caller that could interleave between
            # the count read and the resolution write. If we ever move to
            # concurrent auto-apply workers, this must be made atomic.
            # TODO: enforce the cap atomically via a Postgres advisory lock
            # (pg_advisory_xact_lock) scoped to (vault_id, rule_name, date)
            # so concurrent workers cannot over-apply.
            async with self.metastore.session() as session:
                cap_row = (
                    await session.execute(
                        text(_COUNT_AUTO_APPLIED_TODAY_SQL),
                        {'vault_id': str(vault_id), 'today_start': today_start},
                    )
                ).scalar()
            already_applied = int(cap_row or 0)
            remaining_budget = max(0, config.daily_cap - already_applied)
            if remaining_budget <= 0:
                result.skipped_cap_reached += 1
                continue

            # Check accept_rate from telemetry.
            accept_rate: float | None = None
            if telemetry_service is not None:
                try:
                    rows = await telemetry_service.get_telemetry(
                        rule_name=rule_name, vault_id=vault_id
                    )
                    if rows:
                        accept_rate = rows[0].accept_rate
                except Exception:
                    logger.warning(
                        'auto_apply: failed to fetch telemetry for rule %s',
                        rule_name,
                        exc_info=True,
                    )

            if accept_rate is None or accept_rate < config.accept_rate_threshold:
                result.skipped_low_accept_rate += 1
                continue

            # Fetch pending proposals for this rule.
            async with self.metastore.session() as session:
                proposals_raw = (
                    (
                        await session.execute(
                            text(_PENDING_FOR_AUTO_APPLY_SQL),
                            {
                                'vault_id': str(vault_id),
                                'rule_name': rule_name,
                                'limit': remaining_budget,
                            },
                        )
                    )
                    .mappings()
                    .all()
                )

            for prop in proposals_raw:
                result.proposals_scanned += 1

                surprise = prop.get('surprise_score')
                if surprise is None or surprise < config.confidence_threshold:
                    result.skipped_low_confidence += 1
                    continue

                try:
                    action = get_action(config.action)
                except KeyError:
                    logger.warning(
                        'auto_apply: action %s not found for rule %s',
                        config.action,
                        rule_name,
                    )
                    continue

                if not action.reversible:
                    result.skipped_not_reversible += 1
                    continue

                target_type = str(prop.get('target_type', ''))
                target_id = str(prop.get('target_id', ''))
                finding_id = str(prop.get('id', ''))

                try:
                    action.validate({}, target_type=target_type, target_id=target_id)
                except Exception:
                    continue

                # Guard: lock the proposal row and re-verify it is still
                # pending before executing the side-effecting action. Without
                # this, a concurrent resolution (or a prior sweep whose status
                # flip failed) could cause re-execution of the action on a
                # proposal that is no longer pending.
                async with self.metastore.session() as session:
                    lock_row = (
                        await session.execute(
                            text(
                                'SELECT status FROM maintenance_proposals '
                                'WHERE id = CAST(:id AS uuid) '
                                'FOR UPDATE'
                            ),
                            {'id': finding_id},
                        )
                    ).first()
                    if lock_row is None or lock_row.status != 'pending':
                        logger.debug(
                            'auto_apply: skipping %s (status no longer pending)',
                            finding_id[:8],
                        )
                        continue

                actor = 'system:auto-learn'
                try:
                    execute_result = await action.execute(
                        api, {}, target_id=target_id, vault_id=vault_id, actor=actor
                    )
                    resolution = {
                        'verdict': 'accepted',
                        'actor': actor,
                        'decided_at': datetime.now(timezone.utc).isoformat(),
                        'note': (
                            f'Auto-applied: accept_rate={accept_rate:.2f} '
                            f'>= {config.accept_rate_threshold}, '
                            f'surprise={surprise:.2f} >= {config.confidence_threshold}'
                        ),
                        'followup': {
                            'action': config.action,
                            'params': {},
                            'applied_at': datetime.now(timezone.utc).isoformat(),
                            'applied_state': execute_result.applied_state,
                            'prior_state': execute_result.prior_state,
                            'reversible': True,
                        },
                    }
                    async with self.metastore.session() as session:
                        update_result = await session.execute(
                            text(
                                'UPDATE maintenance_proposals '  # noqa: S608
                                "SET status = 'resolved', "
                                '    resolved_at = now(), '
                                "    resolved_by = 'system:auto-learn', "
                                '    evidence = jsonb_set('
                                "      COALESCE(evidence, '{}'::jsonb), "
                                "      '{resolution}', "
                                '      CAST(:resolution_json AS jsonb), '
                                '      true'
                                '    ) '
                                'WHERE id = CAST(:id AS uuid) '
                                "  AND status = 'pending'"
                            ),
                            {
                                'id': finding_id,
                                'resolution_json': _json.dumps(resolution),
                            },
                        )
                        await session.commit()
                    if update_result.rowcount:
                        result.auto_applied += 1
                        result.details.append(
                            {
                                'finding_id': finding_id,
                                'rule': rule_name,
                                'action': config.action,
                                'surprise': surprise,
                            }
                        )
                        logger.info(
                            'auto_apply: resolved %s via %s (rule=%s, surprise=%.2f)',
                            finding_id[:8],
                            config.action,
                            rule_name,
                            surprise,
                        )
                    else:
                        # Action executed but status flip failed (row deleted
                        # or status already non-pending). The side effect is
                        # real and may need manual reconciliation.
                        logger.warning(
                            'auto_apply.side_effect_without_status_flip',
                            extra={
                                'finding_id': finding_id,
                                'action': config.action,
                                'target_id': target_id,
                                'applied_state': execute_result.applied_state,
                            },
                        )
                except Exception:
                    logger.exception('auto_apply: failed to resolve %s', finding_id[:8])
                    result.errors += 1

        return result
