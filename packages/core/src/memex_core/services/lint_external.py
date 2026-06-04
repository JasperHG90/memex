"""Externally-submitted lint proposals: request models, validation, insert.

External tools (agent skills, routing agents) participate in the lint loop
by submitting proposals — a rule is pure metadata traveling WITH the
proposal (rule_name, description, lint_type); there is no server-side rule
registration and no detection code crosses the wire. The proposal pairs
that metadata with an optional suggestion from the closed action catalogue
(`services/proposal_actions`), validated at the door so the cockpit never
renders a dead suggestion. A human resolves the proposal in the cockpit;
submission itself mutates nothing beyond the pending ledger row.

Dedup and cooldown reuse the contracts internal rules already have: the
partial unique index `uq_maintenance_proposals_pending` arbitrates
duplicate pending submissions (idempotent — the existing row's id is
returned), and a post-resolution cooldown stops an external tool from
nagging past a human dismissal.

This module is deliberately SQLModel-native and separate from
`services/lint.py` (whose raw-SQL surface is earmarked for its own
refactor).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import field_validator
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import col, select

from memex_common.lint import LintProposal, ProposedAction
from memex_core.memory.sql_models import MaintenanceProposal
from memex_core.services.lint import V1_RULES
from memex_core.services.proposal_actions import ActionValidationError, get_action

if TYPE_CHECKING:
    from memex_core.api import MemexAPI

# Re-export so existing importers (server, tests) keep their import site.
__all__ = [
    'ExternalProposalRejected',
    'ExternalProposalRequest',
    'ProposedAction',
    'RESERVED_RULE_NAMES',
    'SubmissionItemResult',
    'action_descriptor',
    'insert_external_proposal',
    'validate_proposed_action',
]

_RESERVED_LITERALS = (
    'entity_collapse_cluster',
    'propose_contradiction_winner',
    'inbox_vault_route',
    'inbox_vault_no_fit',
    'llm_deferred',
)

# Names internal emitters own. Computed from V1_RULES so the set cannot
# drift from the live rule definitions.
RESERVED_RULE_NAMES: frozenset[str] = frozenset(spec.name for spec in V1_RULES) | frozenset(
    _RESERVED_LITERALS
)


class ExternalProposalRejected(ValueError):
    """A single proposal failed validation; carries the per-item detail."""


class ExternalProposalRequest(LintProposal):
    """Server-side proposal validator.

    Inherits the wire shape + core-independent hygiene from
    ``memex_common.lint.LintProposal`` (the SSOT) and adds the one check
    that needs core internals: rejecting rule names reserved for internal
    emitters. The shared shape means the client builder and this validator
    can never diverge — the server can only add constraints.
    """

    @field_validator('rule_name')
    @classmethod
    def _rule_name_not_reserved(cls, v: str) -> str:
        # LintProposal's own validator has already enforced slug + llm_
        # prefix; here we only add the reserved-set rejection.
        if v in RESERVED_RULE_NAMES:
            raise ValueError(f'rule_name {v!r} is reserved for internal emitters.')
        return v
        return v


@dataclass(frozen=True)
class SubmissionItemResult:
    """Per-item outcome of a batch submission (batch is partial-success)."""

    index: int
    status: str  # 'created' | 'deduplicated' | 'cooldown_suppressed' | 'rejected'
    finding_id: str | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {'index': self.index, 'status': self.status}
        if self.finding_id is not None:
            body['finding_id'] = self.finding_id
        if self.detail is not None:
            body['detail'] = self.detail
        return body


def action_descriptor(action: Any) -> dict[str, Any]:
    """Wire shape for one catalogue action (the lint actions listing + facade)."""
    return {
        'id': action.id,
        'name': action.name,
        'description': action.description,
        'applicable_target_types': list(action.applicable_target_types),
        'reversible': action.reversible,
        'params_schema': action.params_schema,
    }


def validate_proposed_action(req: ExternalProposalRequest) -> None:
    """Reject unknown actions, target mismatches, and bad params at the door.

    Runs the action's own imperative ``validate()`` so a proposal whose
    suggestion could never execute is rejected at submission time — the
    cockpit must never pre-select a dead suggestion.
    """
    if req.proposed_action is None:
        return
    try:
        action = get_action(req.proposed_action.action_name)
    except KeyError as exc:
        raise ExternalProposalRejected(str(exc)) from exc
    if req.target_type not in action.applicable_target_types:
        raise ExternalProposalRejected(
            f'proposed action {action.id!r} does not apply to target_type '
            f'{req.target_type!r}; applicable types are {list(action.applicable_target_types)}.'
        )
    try:
        action.validate(
            req.proposed_action.params,
            target_type=req.target_type,
            target_id=req.target_id,
        )
    except ActionValidationError as exc:
        raise ExternalProposalRejected(f'proposed action params invalid: {exc}') from exc


def _assemble_evidence(req: ExternalProposalRequest, *, actor: str) -> dict[str, Any]:
    evidence = dict(req.evidence)
    evidence['rule_metadata'] = {'description': req.description, 'submitted_by': actor}
    if req.proposed_action is not None:
        evidence['proposed_action'] = req.proposed_action.model_dump()
    return evidence


async def insert_external_proposal(
    api: MemexAPI,
    req: ExternalProposalRequest,
    *,
    vault_id: UUID | None,
    actor: str,
) -> tuple[str, UUID | None]:
    """Insert one validated proposal; returns ``(status, finding_id)``.

    Status semantics mirror what internal rules get from
    ``ON CONFLICT DO NOTHING`` plus the post-resolution cooldown:

    * ``created`` — fresh pending row, id returned.
    * ``deduplicated`` — a row for the same (rule, target, vault) already
      covers this; its id is returned so retry-happy callers stay idempotent.
    * ``cooldown_suppressed`` — a human resolved/dismissed this same finding
      within the cooldown window; no row is written.
    """
    cooldown_days = api.config.server.memory.lint.external_proposals.cooldown_days
    evidence = _assemble_evidence(req, actor=actor)

    async with api.metastore.session() as session:
        if cooldown_days > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(days=cooldown_days)
            recent_stmt = (
                select(MaintenanceProposal.id)
                .where(
                    col(MaintenanceProposal.rule_name) == req.rule_name,
                    col(MaintenanceProposal.target_type) == req.target_type,
                    col(MaintenanceProposal.target_id) == req.target_id,
                    col(MaintenanceProposal.vault_id) == vault_id,
                    col(MaintenanceProposal.status).in_(('resolved', 'dismissed')),
                    col(MaintenanceProposal.resolved_at) > cutoff,
                )
                .limit(1)
            )
            recent = (await session.exec(recent_stmt)).first()
            if recent is not None:
                return ('cooldown_suppressed', None)

        insert_stmt = (
            pg_insert(MaintenanceProposal)
            .values(
                vault_id=vault_id,
                lint_type=req.lint_type,
                target_type=req.target_type,
                target_id=req.target_id,
                rule_name=req.rule_name,
                evidence=evidence,
                suggested_action=req.suggested_action,
                status='pending',
                source='external',
            )
            .on_conflict_do_nothing(
                index_elements=['rule_name', 'target_type', 'target_id', 'vault_id'],
                index_where=sa_text("status = 'pending'"),
            )
            .returning(MaintenanceProposal.id)  # type: ignore[arg-type]
        )
        inserted = (await session.execute(insert_stmt)).scalar_one_or_none()
        if inserted is not None:
            await session.commit()
            return ('created', inserted)

        pending_stmt = (
            select(MaintenanceProposal.id)
            .where(
                col(MaintenanceProposal.rule_name) == req.rule_name,
                col(MaintenanceProposal.target_type) == req.target_type,
                col(MaintenanceProposal.target_id) == req.target_id,
                col(MaintenanceProposal.vault_id) == vault_id,
                col(MaintenanceProposal.status) == 'pending',
            )
            .limit(1)
        )
        existing = (await session.exec(pending_stmt)).first()
        if existing is not None:
            return ('deduplicated', existing)
        # Conflict fired but the pending row vanished in the race window
        # (resolved between insert and select); the latest row still covers
        # the submission for idempotency purposes.
        latest_stmt = (
            select(MaintenanceProposal.id)
            .where(
                col(MaintenanceProposal.rule_name) == req.rule_name,
                col(MaintenanceProposal.target_type) == req.target_type,
                col(MaintenanceProposal.target_id) == req.target_id,
                col(MaintenanceProposal.vault_id) == vault_id,
            )
            .order_by(col(MaintenanceProposal.created_at).desc())
            .limit(1)
        )
        latest = (await session.exec(latest_stmt)).first()
        return ('deduplicated', latest)
