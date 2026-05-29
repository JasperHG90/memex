"""Pydantic models for the ``evidence`` JSONB payload on routing proposals.

The cockpit and any downstream consumer read these shapes off
``maintenance_proposals.evidence``. Keeping them typed (rather than a bare dict)
gives one schema both the emitter and the cockpit agree on.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CandidateEvidence(BaseModel):
    """One scored candidate vault, as surfaced to the cockpit."""

    vault_id: str
    vault_name: str
    p_match: float = Field(description='Per-note softmax-normalised P(match).')
    p_match_raw: float = Field(description='Raw pairwise sigmoid P(match).')
    ci_half_width: float = Field(description='Credible-interval half-width.')


class RouteEvidence(BaseModel):
    """Evidence for an ``inbox_vault_route`` proposal.

    ``top_candidates`` drives the per-candidate cockpit options. ``routing_state``
    tells the operator whether the model is warmed up or still cold-starting.
    """

    kind: str = 'inbox_vault_route'
    routing_state: str
    margin: float
    source_vault_id: str
    top_candidates: list[CandidateEvidence]


class NoFitEvidence(BaseModel):
    """Evidence for an ``inbox_vault_no_fit`` proposal, including backoff state."""

    kind: str = 'inbox_vault_no_fit'
    routing_state: str
    best_p_match_raw: float = Field(
        description='Highest raw P(match) across vaults — below the t_low floor.'
    )
    retry_n: int = Field(default=0, description='Number of no-fit re-evaluations so far.')
    next_retry_at: str | None = Field(
        default=None, description='ISO timestamp when this note becomes eligible again.'
    )
    last_evaluated_at: str | None = None
