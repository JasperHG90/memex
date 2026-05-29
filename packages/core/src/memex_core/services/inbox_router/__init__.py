"""Inbox router service package.

Periodic triage of the ``inbox`` vault: scores each note against every other
vault with a pairwise GaussianNB model evaluated in Postgres, then auto-routes
confident notes or emits cockpit proposals. See ``service.InboxRouterService``.
"""

from memex_core.services.inbox_router.decisions import (
    CandidateScore,
    DecisionKind,
    DecisionThresholds,
    RouterDecision,
    RoutingState,
    decide,
)
from memex_core.services.inbox_router.service import InboxRouterService, TriageResult

__all__ = [
    'CandidateScore',
    'DecisionKind',
    'DecisionThresholds',
    'InboxRouterService',
    'RouterDecision',
    'RoutingState',
    'TriageResult',
    'decide',
]
