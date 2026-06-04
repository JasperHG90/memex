"""Protocol + registry for maintenance-proposal actions.

A `ProposalAction` is a canned mutation paired with its inverse. The cockpit
shows them to the human as numbered options; the human picks one (or supplies
a free-form Other and maps it to a canned action); the server executes
`execute()` and stamps `prior_state` + `applied_state` into
`evidence.resolution.followup` so a later `reverse()` can roll the side
effects back. Reversibility is a binary class attribute — forward-only
actions declare `reversible = False`, and the `/reverse` HTTP endpoint
short-circuits to 409 with `{reason: 'forward_only'}` without writing an
audit row.

Actions are NOT free-form. The catalogue is hand-curated in the registry
and exposed verbatim to the CLI cockpit and (in a follow-on) the MCP layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Protocol
from uuid import UUID

if TYPE_CHECKING:
    from memex_core.api import MemexAPI


class ProposalActionError(RuntimeError):
    """Generic failure inside an action — execute or reverse refused to proceed."""


class ActionValidationError(ProposalActionError):
    """`validate()` rejected the supplied params for this target."""


@dataclass(frozen=True)
class ExecuteResult:
    """What an `execute()` call returns.

    `applied_state` describes what the action just did (e.g. unit IDs touched,
    rows queued for refresh). `prior_state` captures everything needed to
    drive `reverse()` later. Both are stored under
    `evidence.resolution.followup` on the maintenance_proposal row.
    """

    applied_state: dict[str, Any] = field(default_factory=dict)
    prior_state: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReverseResult:
    """What a `reverse()` call returns.

    `restored_state` is the post-reverse snapshot — useful for audit display.
    """

    restored_state: dict[str, Any] = field(default_factory=dict)


class ProposalAction(Protocol):
    """Contract every registered action satisfies.

    Class-level attributes are read by the cockpit (to render the menu) and
    by `/reverse` (to short-circuit forward-only actions). Methods are
    awaitable so they compose with the `MemexAPI` async surface.
    """

    id: ClassVar[str]
    name: ClassVar[str]
    description: ClassVar[str]
    applicable_target_types: ClassVar[tuple[str, ...]]
    reversible: ClassVar[bool]
    # JSON schema for `params` (from a Pydantic model's `model_json_schema()`),
    # or None for parameterless actions. Discoverability only — `validate()`
    # remains the execution-time gate. Surfaced verbatim by `GET /lint/actions`
    # so external submitters and the cockpit can render parameter forms.
    params_schema: ClassVar[dict[str, Any] | None]

    def validate(
        self,
        params: dict[str, Any],
        *,
        target_type: str,
        target_id: str,
    ) -> None:
        """Reject impossible params before any side effect runs.

        Raises `ActionValidationError` with a human-readable message; the
        HTTP layer maps this to 400 with `detail` verbatim.
        """
        ...

    async def execute(
        self,
        api: MemexAPI,
        params: dict[str, Any],
        *,
        target_id: str,
        vault_id: UUID | None,
        actor: str,
    ) -> ExecuteResult:
        """Run the mutation and return its before/after snapshot."""
        ...

    async def reverse(
        self,
        api: MemexAPI,
        params: dict[str, Any],
        applied_state: dict[str, Any],
        prior_state: dict[str, Any],
        *,
        target_id: str,
        vault_id: UUID | None,
        actor: str,
    ) -> ReverseResult:
        """Undo the side effects captured under `prior_state`.

        Forward-only actions should raise `ProposalActionError` here; the
        server-level fence on `action.reversible` is the primary guard, but
        defensive raises keep the contract local.
        """
        ...

    async def preview(
        self,
        api: MemexAPI,
        params: dict[str, Any],
        *,
        target_id: str,
        vault_id: UUID | None,
    ) -> str:
        """One-line description of the blast radius shown in the cockpit.

        Read-only. Must NOT mutate state. Coarse estimates ('~3 observations')
        are fine where exact counts would be expensive.
        """
        ...


_REGISTRY: dict[str, ProposalAction] = {}


def register_action(action: ProposalAction) -> ProposalAction:
    """Register a single action instance under its `id`.

    Used as `register_action(MyAction())` in each action module; importing
    the module triggers registration as a side effect. Re-registering the
    same id raises so we catch typos at import time.
    """
    aid = action.id
    if aid in _REGISTRY:
        existing = _REGISTRY[aid]
        if existing is not action:
            raise ValueError(f'proposal action id {aid!r} already registered: {existing!r}')
        return action
    _REGISTRY[aid] = action
    return action


def get_action(action_id: str) -> ProposalAction:
    """Look up a registered action; raises `KeyError` on miss.

    Callers should treat KeyError as a 400-class error (the client referenced
    an unknown action_id).
    """
    try:
        return _REGISTRY[action_id]
    except KeyError as exc:
        raise KeyError(f'unknown proposal action: {action_id!r}') from exc


def list_actions(*, target_type: str | None = None) -> list[ProposalAction]:
    """Enumerate registered actions, optionally filtered by target_type.

    The cockpit calls this with the proposal's target_type to populate the
    `[O]ther → map to a standard action` menu — actions that don't apply to
    this target are hidden, not greyed out, so users can't pick a dead end.
    """
    actions = list(_REGISTRY.values())
    if target_type is not None:
        actions = [a for a in actions if target_type in a.applicable_target_types]
    return actions
