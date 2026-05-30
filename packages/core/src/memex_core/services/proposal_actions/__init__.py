"""Maintenance-proposal action registry.

Importing this package registers every built-in action as a side effect.
Server, CLI, and (in a follow-on) MCP layers look up actions by id via
`get_action`; the cockpit menu uses `list_actions(target_type=...)` to
filter the catalogue to actions that apply to the proposal in front of it.
"""

from __future__ import annotations

# Re-export the registry surface.
from memex_core.services.proposal_actions.base import (
    ActionValidationError,
    ExecuteResult,
    ProposalAction,
    ProposalActionError,
    get_action,
    list_actions,
    register_action,
)

# Side-effect imports — each module calls `register_action(...)` on import.
from memex_core.services.proposal_actions import (  # noqa: F401  (registration side effects)
    archive_mental_model,
    deprioritize_unit,
    no_op,
    route_note_to_vault,
)

__all__ = [
    'ActionValidationError',
    'ExecuteResult',
    'ProposalAction',
    'ProposalActionError',
    'get_action',
    'list_actions',
    'register_action',
]
