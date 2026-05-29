"""Pure decision logic for the inbox router.

Given the scored candidates for one note, decide whether to auto-route, propose
candidates for the cockpit, or mark the note as no-fit. No I/O — trivially
unit-testable. The thresholds and the warmed-up flag are supplied by the caller
(from config + the live match-class observation count).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from uuid import UUID


class DecisionKind(str, Enum):
    AUTO_ROUTE = 'auto_route'
    PROPOSE_CANDIDATES = 'propose_candidates'
    PROPOSE_NO_FIT = 'propose_no_fit'


class RoutingState(str, Enum):
    #: Auto-apply eligible — counts cleared the cold-start gate.
    WARMED_UP = 'warmed_up_auto_eligible'
    #: Still learning — propose only, regardless of confidence.
    COLD_START = 'cold_start_no_auto'
    #: Auto-apply disabled by config — always propose.
    DISABLED = 'disabled'


@dataclass(frozen=True)
class CandidateScore:
    """One scored (note, vault) pair from ``SCORE_NOTES_SQL``."""

    vault_id: UUID
    vault_name: str
    p_match: float  # per-note softmax-normalised P(match)
    p_match_raw: float  # raw pairwise sigmoid P(match)
    ci_half_width: float  # delta-method credible-interval half-width


@dataclass(frozen=True)
class DecisionThresholds:
    auto_apply_enabled: bool
    auto_apply_min_p_match: float
    t_margin: float
    t_low: float


@dataclass(frozen=True)
class RouterDecision:
    note_id: UUID
    kind: DecisionKind
    routing_state: RoutingState
    candidates: tuple[CandidateScore, ...]  # sorted desc by p_match
    margin: float  # top1.p_match - top2.p_match (0.0 if a single candidate)

    @property
    def top(self) -> CandidateScore | None:
        return self.candidates[0] if self.candidates else None


def decide(
    note_id: UUID,
    candidates: list[CandidateScore],
    *,
    thresholds: DecisionThresholds,
    warmed_up: bool,
) -> RouterDecision:
    """Classify a note's routing outcome from its scored candidates.

    ``candidates`` need not be pre-sorted; this sorts by ``p_match`` descending.
    """
    ordered = tuple(sorted(candidates, key=lambda c: c.p_match, reverse=True))

    if not thresholds.auto_apply_enabled:
        routing_state = RoutingState.DISABLED
    elif not warmed_up:
        routing_state = RoutingState.COLD_START
    else:
        routing_state = RoutingState.WARMED_UP

    # No candidate clears the floor → the note doesn't fit any vault.
    top = ordered[0] if ordered else None
    if top is None or top.p_match_raw < thresholds.t_low:
        return RouterDecision(
            note_id=note_id,
            kind=DecisionKind.PROPOSE_NO_FIT,
            routing_state=routing_state,
            candidates=ordered,
            margin=0.0,
        )

    margin = top.p_match - (ordered[1].p_match if len(ordered) > 1 else 0.0)

    auto_ok = (
        routing_state is RoutingState.WARMED_UP
        and top.p_match >= thresholds.auto_apply_min_p_match
        and margin >= thresholds.t_margin
    )
    kind = DecisionKind.AUTO_ROUTE if auto_ok else DecisionKind.PROPOSE_CANDIDATES

    return RouterDecision(
        note_id=note_id,
        kind=kind,
        routing_state=routing_state,
        candidates=ordered,
        margin=margin,
    )
