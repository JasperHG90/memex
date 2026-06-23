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
from sqlalchemy import cast, func, literal, text
from sqlalchemy import select as sa_select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
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
    # 'inbox_vault_route' / 'inbox_vault_no_fit' were reserved while the in-core
    # router emitted them. The router is gone; routing is now owned by the
    # external triage-inbox skill, so these are deliberately NOT reserved and
    # external submissions may use them. Historical routing findings stay
    # resolvable (the maintenance_proposals rows are untouched).
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


def _build_external_proposal_stmt(
    *,
    vault_id: UUID | None,
    req: ExternalProposalRequest,
    evidence: dict[str, Any],
    cooldown_days: int,
) -> Any:
    """Build the external-proposal ``INSERT … SELECT … WHERE NOT EXISTS … ON CONFLICT``.

    Mirrors :func:`lint.\\_build_insert_finding_stmt` (same single-statement
    cooldown-guard + conflict-arbiter idiom) with ``source='external'``.

    The ``index_where`` MUST be the partial index's literal predicate verbatim
    (``status = 'pending'``). A ``col()`` comparison renders as a bound parameter
    that Postgres cannot prove implies the index predicate once the prepared
    statement flips to a generic plan, raising "no unique or exclusion constraint
    matching the ON CONFLICT specification".
    """
    mp = MaintenanceProposal

    # A resolved/dismissed sibling within the cooldown window suppresses
    # re-creation. cooldown_days=0 makes `resolved_at > now() - make_interval(0)`
    # (i.e. resolved_at > now()) false for every past row, so this is a no-op.
    recent_resolution = (
        sa_select(literal(1))
        .where(
            col(mp.rule_name) == req.rule_name,
            col(mp.target_type) == req.target_type,
            col(mp.target_id) == req.target_id,
            col(mp.vault_id).is_not_distinct_from(vault_id),
            col(mp.status).in_(('resolved', 'dismissed')),
            col(mp.resolved_at) > func.now() - func.make_interval(0, 0, 0, cooldown_days),
        )
        .exists()
    )

    # INSERT … SELECT <new row> WHERE NOT EXISTS(<recent resolution>) ON CONFLICT
    # DO NOTHING RETURNING id — the cooldown guard and the conflict arbiter are
    # evaluated in ONE statement (no check-then-insert TOCTOU), built entirely
    # from typed SQLAlchemy constructs so every value is a bound parameter.
    source = sa_select(
        literal(vault_id, type_=PG_UUID(as_uuid=True)).label('vault_id'),
        literal(req.lint_type).label('lint_type'),
        literal(req.target_type).label('target_type'),
        literal(req.target_id).label('target_id'),
        literal(req.rule_name).label('rule_name'),
        # default=str mirrors the internal _json_dumps fallback: over HTTP the
        # evidence is JSON-native, but a direct in-process caller may pass
        # datetime/UUID values, which bare json.dumps would raise on.
        cast(literal(json.dumps(evidence, default=str)), JSONB).label('evidence'),
        literal(req.suggested_action).label('suggested_action'),
        literal('pending').label('status'),
        literal('external').label('source'),
    ).where(~recent_resolution)

    return (
        pg_insert(mp)
        .from_select(
            [
                'vault_id',
                'lint_type',
                'target_type',
                'target_id',
                'rule_name',
                'evidence',
                'suggested_action',
                'status',
                'source',
            ],
            source,
        )
        .on_conflict_do_nothing(
            index_elements=['rule_name', 'target_type', 'target_id', 'vault_id'],
            index_where=text("status = 'pending'"),
        )
        .returning(mp.id)
    )


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

    insert_stmt = _build_external_proposal_stmt(
        vault_id=vault_id, req=req, evidence=evidence, cooldown_days=cooldown_days
    )

    async with api.metastore.session() as session:
        inserted = (await session.execute(insert_stmt)).scalar_one_or_none()
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
