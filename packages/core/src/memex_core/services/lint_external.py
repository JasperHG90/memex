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
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlmodel import col, select

from memex_core.memory.sql_models import LintType, MaintenanceProposal
from memex_core.services.lint import V1_RULES
from memex_core.services.proposal_actions import ActionValidationError, get_action

if TYPE_CHECKING:
    from memex_core.api import MemexAPI


# Lowercase slug: dash or underscore. Underscores are allowed on purpose —
# internal rule names are snake_case, so the reserved set below (not the
# pattern) is what fences collisions.
_RULE_NAME_RE = re.compile(r'^[a-z][a-z0-9_-]*$')
_TARGET_TYPE_RE = re.compile(r'^[a-z][a-z0-9_]*$')

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

# Evidence keys the server owns; accepting them from a submitter would let
# an external tool forge resolution records, rule metadata, or — via
# vaults_affected — the authorization scope the global-finding gate trusts.
_RESERVED_EVIDENCE_KEYS = frozenset(
    {'resolution', 'rule_metadata', 'proposed_action', 'vaults_affected'}
)

_MAX_EVIDENCE_BYTES = 16_384

_VALID_LINT_TYPES = frozenset(item.value for item in LintType)


class ExternalProposalRejected(ValueError):
    """A single proposal failed validation; carries the per-item detail."""


class ProposedAction(BaseModel):
    action_name: str = Field(
        min_length=1,
        max_length=64,
        description='id of a registered catalogue action (see the lint actions listing).',
    )
    params: dict[str, Any] = Field(
        default_factory=dict,
        description='Parameters for the action, matching its published params_schema.',
    )


class ExternalProposalRequest(BaseModel):
    """One externally-submitted lint proposal (rule metadata included inline)."""

    vault_id: str | None = Field(
        default=None,
        description='Vault UUID or name the finding belongs to.',
    )
    rule_name: str = Field(
        min_length=1,
        max_length=64,
        description='Caller-owned rule identifier (lowercase slug).',
    )
    lint_type: str = Field(
        description='Finding category: structural | quality | governance | schema | routing.',
    )
    target_type: str = Field(
        min_length=1,
        max_length=64,
        description="Construct the finding targets (e.g. 'note', 'memory_unit', 'entity', 'kv').",
    )
    target_id: str = Field(
        min_length=1,
        max_length=512,
        description='Identifier of the targeted construct (UUID for rows, key for KV).',
    )
    description: str = Field(
        min_length=1,
        max_length=500,
        description='What the rule detects and why it fired — shown to the reviewer.',
    )
    suggested_action: str = Field(
        min_length=1,
        max_length=500,
        description='Free-text remediation summary shown on the finding card.',
    )
    evidence: dict[str, Any] = Field(
        default_factory=dict,
        description='Rule-specific payload supporting the finding.',
    )
    proposed_action: ProposedAction | None = Field(
        default=None,
        description='Optional catalogue action the cockpit pre-selects at review time.',
    )

    @field_validator('rule_name')
    @classmethod
    def _rule_name_hygiene(cls, v: str) -> str:
        if not _RULE_NAME_RE.fullmatch(v):
            raise ValueError('rule_name must be a lowercase slug ([a-z][a-z0-9_-]*).')
        if v in RESERVED_RULE_NAMES:
            raise ValueError(f'rule_name {v!r} is reserved for internal emitters.')
        if v.startswith('llm_'):
            raise ValueError("rule_name prefix 'llm_' is reserved for internal emitters.")
        return v

    @field_validator('lint_type')
    @classmethod
    def _lint_type_member(cls, v: str) -> str:
        if v not in _VALID_LINT_TYPES:
            raise ValueError(f'lint_type must be one of {sorted(_VALID_LINT_TYPES)}.')
        return v

    @field_validator('target_type')
    @classmethod
    def _target_type_hygiene(cls, v: str) -> str:
        if not _TARGET_TYPE_RE.fullmatch(v):
            raise ValueError('target_type must be a lowercase identifier ([a-z][a-z0-9_]*).')
        return v

    @field_validator('evidence')
    @classmethod
    def _evidence_hygiene(cls, v: dict[str, Any]) -> dict[str, Any]:
        clashes = _RESERVED_EVIDENCE_KEYS & v.keys()
        if clashes:
            raise ValueError(f'evidence keys {sorted(clashes)} are reserved for the server.')
        try:
            size = len(json.dumps(v, default=str))
        except (TypeError, ValueError) as exc:
            raise ValueError(f'evidence is not JSON-serializable: {exc}') from exc
        if size > _MAX_EVIDENCE_BYTES:
            raise ValueError(f'evidence too large ({size} bytes > {_MAX_EVIDENCE_BYTES}).')
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
