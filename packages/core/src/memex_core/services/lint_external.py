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

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from pydantic import ConfigDict, field_validator
from sqlalchemy import text as sa_text
from sqlmodel import col, select

from memex_common.lint import LintProposal, ProposedAction
from memex_core.memory.sql_models import MaintenanceProposal
from memex_core.services.lint import V1_RULES
from memex_core.services.proposal_actions import (
    ActionValidationError,
    ProposalAction,
    get_action,
)

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

    ``extra='forbid'`` rejects unknown keys: at the untrusted server
    ingress a submitter's typo'd field should surface as a per-item 400,
    not be silently dropped (the client-side ``LintProposal`` stays lenient).
    """

    model_config = ConfigDict(extra='forbid')

    @field_validator('rule_name')
    @classmethod
    def _rule_name_not_reserved(cls, v: str) -> str:
        # LintProposal's own validator has already enforced slug + llm_
        # prefix; here we only add the reserved-set rejection.
        if v in RESERVED_RULE_NAMES:
            raise ValueError(f'rule_name {v!r} is reserved for internal emitters.')
        return v


SubmissionStatus = Literal['created', 'deduplicated', 'cooldown_suppressed', 'rejected']


@dataclass(frozen=True)
class SubmissionItemResult:
    """Per-item outcome of a batch submission (batch is partial-success)."""

    index: int
    status: SubmissionStatus
    finding_id: str | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {'index': self.index, 'status': self.status}
        if self.finding_id is not None:
            body['finding_id'] = self.finding_id
        if self.detail is not None:
            body['detail'] = self.detail
        return body


def action_descriptor(action: ProposalAction) -> dict[str, Any]:
    """Wire shape for one catalogue action (the lint actions listing + facade).

    ``params_schema`` is the action's Pydantic ``model_json_schema()`` and is
    served verbatim to external callers. Every current action uses a flat
    params model, so the schema carries no ``$defs`` with internal type
    names; if a future action nests models, sanitise/inline ``$defs`` here
    before exposing it so internal class names don't leak.
    """
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
) -> tuple[Literal['created', 'deduplicated', 'cooldown_suppressed'], UUID | None]:
    """Insert one validated proposal; returns ``(status, finding_id)``.

    Status semantics mirror what internal rules get from
    ``ON CONFLICT DO NOTHING`` plus the post-resolution cooldown:

    * ``created`` — fresh pending row, id returned.
    * ``deduplicated`` — a pending row for the same (rule, target, vault)
      already covers this; its id is returned so retry-happy callers stay
      idempotent. ``finding_id`` is always a real pending row.
    * ``cooldown_suppressed`` — a human resolved/dismissed this same finding
      within the cooldown window; no row is written, ``finding_id`` is None.

    The cooldown check and the insert are ONE statement — an
    ``INSERT … SELECT … WHERE NOT EXISTS(<recent resolution>) ON CONFLICT
    DO NOTHING`` (the contract internal-rule emission uses). That removes the
    check-then-insert TOCTOU: a concurrent resolution cannot slip between a
    separate cooldown SELECT and the insert, and the classification below is
    unambiguous — a failed insert is either a live pending dedup or a
    cooldown block, never a resolved-row id masquerading as a dedup.
    """
    cooldown_days = api.config.server.memory.lint.external_proposals.cooldown_days
    evidence = _assemble_evidence(req, actor=actor)

    params: dict[str, Any] = {
        'vault_id': str(vault_id) if vault_id is not None else None,
        'lint_type': req.lint_type,
        'target_type': req.target_type,
        'target_id': req.target_id,
        'rule_name': req.rule_name,
        'evidence': json.dumps(evidence),
        'suggested_action': req.suggested_action,
        'cooldown_days': cooldown_days,
    }
    # The cooldown predicate is part of the INSERT's source SELECT, so it is
    # evaluated atomically with the conflict arbiter. cooldown_days=0 short-
    # circuits the NOT EXISTS to a no-op (nothing recent ever matches).
    # Injection-safe: cooldown_clause is a module-local constant (no runtime
    # interpolation) and the only dynamic value — cooldown_days — is bound as
    # the :cooldown_days parameter. The f-string splice below only chooses
    # WHETHER to include the constant clause, never what it contains.
    cooldown_clause = """
      AND NOT EXISTS (
          SELECT 1 FROM maintenance_proposals mp
           WHERE mp.rule_name = :rule_name
             AND mp.target_type = :target_type
             AND mp.target_id = :target_id
             AND mp.vault_id IS NOT DISTINCT FROM CAST(:vault_id AS uuid)
             AND mp.status IN ('resolved', 'dismissed')
             AND mp.resolved_at > now() - make_interval(days => :cooldown_days)
      )
    """
    # Defense-in-depth: cooldown_clause must stay a constant string. A future
    # refactor that made it carry a runtime value would turn the f-string splice
    # below into an injection vector. An explicit raise (NOT assert, which
    # `python -O` strips) keeps the guard alive under optimized bytecode.
    if not isinstance(cooldown_clause, str):
        raise TypeError('cooldown_clause must be a constant string, never a runtime value')
    insert_sql = sa_text(
        'INSERT INTO maintenance_proposals '
        '(vault_id, lint_type, target_type, target_id, rule_name, evidence, '
        ' suggested_action, status, source) '
        'SELECT CAST(:vault_id AS uuid), :lint_type, :target_type, :target_id, '
        '       :rule_name, CAST(:evidence AS jsonb), :suggested_action, '
        "       'pending', 'external' "
        'WHERE true'
        f'{cooldown_clause if cooldown_days > 0 else ""} '
        'ON CONFLICT (rule_name, target_type, target_id, vault_id) '
        "WHERE status = 'pending' DO NOTHING "
        'RETURNING id'
    )

    async with api.metastore.session() as session:
        inserted = (await session.execute(insert_sql, params)).scalar_one_or_none()
        if inserted is not None:
            await session.commit()
            return ('created', inserted)

        # No row inserted: either a pending row already covers this (ON
        # CONFLICT), or the cooldown NOT EXISTS blocked the source SELECT.
        # A live pending row is the dedup target; its absence means the
        # cooldown is what stopped us.
        pending = (
            await session.exec(
                select(MaintenanceProposal.id)
                .where(
                    col(MaintenanceProposal.rule_name) == req.rule_name,
                    col(MaintenanceProposal.target_type) == req.target_type,
                    col(MaintenanceProposal.target_id) == req.target_id,
                    # IS NOT DISTINCT FROM semantics: a global (NULL) vault must
                    # match the existing NULL pending row; `== None` → `= NULL`
                    # never matches, misclassifying a dup as cooldown_suppressed.
                    (
                        col(MaintenanceProposal.vault_id).is_(None)
                        if vault_id is None
                        else col(MaintenanceProposal.vault_id) == vault_id
                    ),
                    col(MaintenanceProposal.status) == 'pending',
                )
                .limit(1)
            )
        ).first()
        if pending is not None:
            return ('deduplicated', pending)
        return ('cooldown_suppressed', None)
