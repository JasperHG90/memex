"""Client-facing models for externally-submitted lint proposals.

A lint *rule* is pure metadata that travels with the proposal — there is
no server-side rule registration. This module is the SSOT for the request
*shape* and the hygiene validators that do not need anything from
``memex_core``; an external tool constructs a :class:`LintProposal` (or
subclasses :class:`LintRule`) and sends it via
``RemoteMemexAPI.submit_lint_proposals``.

The server's ``memex_core.services.lint_external.ExternalProposalRequest``
subclasses :class:`LintProposal` and adds the only validation that needs
core internals — rejecting rule names reserved for internal emitters
(computed from the live rule set). So the wire shape is defined exactly
once, here, and the server can only add constraints, never diverge.

``LINT_TYPES`` is duplicated from ``memex_core.memory.sql_models.LintType``
because common must not import core; the parity test
``packages/core/tests/unit/services/test_lint_external.py`` pins them equal.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Mirror of ``LintType`` (memex_core.memory.sql_models). Parity-tested.
LINT_TYPES: tuple[str, ...] = ('structural', 'quality', 'governance', 'schema', 'routing')

# Lowercase slug; dash or underscore. The reserved-name *set* (server-side)
# fences collisions with internal snake_case rule names — the pattern only
# constrains shape.
_RULE_NAME_RE = re.compile(r'^[a-z][a-z0-9_-]*$')
_TARGET_TYPE_RE = re.compile(r'^[a-z][a-z0-9_]*$')

# Evidence keys the server owns; a submitter setting them could forge
# resolution records, rule metadata, or — via vaults_affected — the
# authorization scope the global-finding gate trusts.
RESERVED_EVIDENCE_KEYS: frozenset[str] = frozenset(
    {'resolution', 'rule_metadata', 'proposed_action', 'vaults_affected'}
)

MAX_EVIDENCE_BYTES: int = 16_384


def validate_rule_name_shape(value: str) -> str:
    """Slug + reserved-prefix check (the core-independent half).

    The server additionally rejects names in its reserved set; that check
    needs the live rule list and lives in core.
    """
    if not _RULE_NAME_RE.fullmatch(value):
        raise ValueError('rule_name must be a lowercase slug ([a-z][a-z0-9_-]*).')
    if value.startswith('llm_'):
        raise ValueError("rule_name prefix 'llm_' is reserved for internal emitters.")
    return value


class ProposedAction(BaseModel):
    """A catalogue action the submitter suggests for a proposal.

    ``action_name`` must reference a registered action in the closed
    catalogue (``GET /lint/actions`` / ``memex_list_lint_actions``); the
    server validates it applies to the proposal's ``target_type`` and that
    ``params`` satisfy the action's published schema.
    """

    action_name: str = Field(
        min_length=1,
        max_length=64,
        description='id of a registered catalogue action (see the lint actions listing).',
    )
    params: dict[str, Any] = Field(
        default_factory=dict,
        description='Parameters for the action, matching its published params_schema.',
    )


class LintProposal(BaseModel):
    """One externally-submitted lint proposal — the canonical wire shape.

    Construct directly, or build from a :class:`LintRule` subclass. Field
    validators here are the core-independent hygiene checks; the server
    subclass adds reserved-rule-name rejection.
    """

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
    vault_id: str | None = Field(
        default=None,
        description='Vault UUID or name the finding belongs to (required by the server by default).',
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
    def _rule_name_shape(cls, v: str) -> str:
        return validate_rule_name_shape(v)

    @field_validator('lint_type')
    @classmethod
    def _lint_type_member(cls, v: str) -> str:
        if v not in LINT_TYPES:
            raise ValueError(f'lint_type must be one of {sorted(LINT_TYPES)}.')
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
        clashes = RESERVED_EVIDENCE_KEYS & v.keys()
        if clashes:
            raise ValueError(f'evidence keys {sorted(clashes)} are reserved for the server.')
        try:
            size = len(json.dumps(v, default=str))
        except (TypeError, ValueError) as exc:
            raise ValueError(f'evidence is not JSON-serializable: {exc}') from exc
        if size > MAX_EVIDENCE_BYTES:
            raise ValueError(f'evidence too large ({size} bytes > {MAX_EVIDENCE_BYTES}).')
        return v


class LintRule(BaseModel):
    """Subclass this to define a custom lint rule as reusable metadata.

    The rule itself is just its identity (``rule_name`` / ``lint_type`` /
    ``description``); *when* it fires is entirely your detection logic's
    business. Call :meth:`build` per finding to stamp that identity onto a
    concrete :class:`LintProposal`, then send a batch via
    ``RemoteMemexAPI.submit_lint_proposals``.

    >>> class DecommissionedSkillRef(LintRule):
    ...     rule_name: str = 'decommissioned-skill-ref'
    ...     lint_type: str = 'governance'
    ...     description: str = 'Unit cites a skill retired in the 2026-05 cleanup.'
    >>> rule = DecommissionedSkillRef()
    >>> proposal = rule.build(
    ...     vault_id='hermes',
    ...     target_type='memory_unit',
    ...     target_id=unit_id,
    ...     suggested_action='Deprioritise the unit; the skill no longer exists.',
    ...     evidence={'skill': 'old-router'},
    ...     proposed_action=ProposedAction(
    ...         action_name='deprioritize_unit', params={'reason': 'decommissioned skill'}
    ...     ),
    ... )
    """

    # Subclasses set the metadata as field defaults; validate them so a
    # malformed rule_name / lint_type fails at instantiation, not at submit.
    model_config = ConfigDict(validate_default=True)

    rule_name: str = Field(min_length=1, max_length=64)
    lint_type: str = Field(description='structural | quality | governance | schema | routing.')
    description: str = Field(
        min_length=1,
        max_length=500,
        description='What the rule detects and why it fires — shown to the reviewer.',
    )

    @field_validator('rule_name')
    @classmethod
    def _rule_name_shape(cls, v: str) -> str:
        return validate_rule_name_shape(v)

    @field_validator('lint_type')
    @classmethod
    def _lint_type_member(cls, v: str) -> str:
        if v not in LINT_TYPES:
            raise ValueError(f'lint_type must be one of {sorted(LINT_TYPES)}.')
        return v

    def build(
        self,
        *,
        target_type: str,
        target_id: str,
        suggested_action: str,
        vault_id: str | None = None,
        evidence: dict[str, Any] | None = None,
        proposed_action: ProposedAction | None = None,
    ) -> LintProposal:
        """Produce a concrete proposal carrying this rule's metadata."""
        return LintProposal(
            rule_name=self.rule_name,
            lint_type=self.lint_type,
            description=self.description,
            target_type=target_type,
            target_id=target_id,
            suggested_action=suggested_action,
            vault_id=vault_id,
            evidence=evidence or {},
            proposed_action=proposed_action,
        )


class CandidateEvidence(BaseModel):
    """One scored candidate vault, surfaced in a routing proposal's evidence."""

    vault_id: str = Field(description='UUID of the candidate target vault.')
    vault_name: str = Field(description='Human-readable vault name, shown in the cockpit option.')
    p_match: float = Field(
        description='Per-note P(match), normalised across candidate vaults (sums to 1).'
    )
    p_match_raw: float = Field(
        description='Unnormalised per-pair P(match) for this vault, in [0, 1].'
    )
    ci_half_width: float = Field(
        description='Wald 95% half-width on p_match (uncertainty hint for the reviewer).'
    )


class RouteEvidence(BaseModel):
    """Typed ``evidence`` payload for an ``inbox_vault_route`` proposal.

    ``top_candidates`` drives the per-candidate cockpit options; ``routing_state``
    tells the reviewer whether the model is warmed up or cold-starting. Emitted by
    the triage-inbox routing skill and consumed by the maintenance cockpit — both
    agree on this one shape. Rides ``LintProposal.evidence`` as the dumped dict.
    """

    kind: str = Field(
        default='inbox_vault_route', description='Discriminator for the evidence shape.'
    )
    routing_state: str = Field(
        description="Model state when scored, e.g. 'warm' (trained) or 'cold' (below warm-up)."
    )
    margin: float = Field(
        description='Top-1 minus top-2 normalised p_match — the decisiveness of the route.'
    )
    source_vault_id: str = Field(
        description='UUID of the vault the note is being routed out of (the inbox).'
    )
    top_candidates: list[CandidateEvidence] = Field(
        description='Candidate vaults ranked by p_match (highest first); the first is the proposed route.'
    )


class NoFitEvidence(BaseModel):
    """Typed ``evidence`` payload for an ``inbox_vault_no_fit`` proposal."""

    kind: str = Field(
        default='inbox_vault_no_fit', description='Discriminator for the evidence shape.'
    )
    routing_state: str = Field(
        description="Model state when scored, e.g. 'warm' (trained) or 'cold' (below warm-up)."
    )
    best_p_match_raw: float = Field(
        description='Highest unnormalised P(match) across vaults — below the t_low floor, hence no fit.'
    )
    retry_n: int = Field(default=0, description='Number of no-fit re-evaluations so far.')
    next_retry_at: str | None = Field(
        default=None,
        description='ISO-8601 timestamp when this note becomes eligible for re-evaluation.',
    )
    last_evaluated_at: str | None = Field(
        default=None, description='ISO-8601 timestamp of the most recent no-fit evaluation.'
    )


__all__ = [
    'LINT_TYPES',
    'MAX_EVIDENCE_BYTES',
    'RESERVED_EVIDENCE_KEYS',
    'CandidateEvidence',
    'LintProposal',
    'LintRule',
    'NoFitEvidence',
    'ProposedAction',
    'RouteEvidence',
    'validate_rule_name_shape',
]
