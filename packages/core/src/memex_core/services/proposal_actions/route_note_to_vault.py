"""`route_note_to_vault` — reversible note migration for inbox-router proposals.

Moves a note from its current vault (the inbox) to a target vault via
``MemexAPI.migrate_note``. The cockpit renders one option per candidate vault
(from ``evidence.top_candidates``) and supplies ``target_vault_id`` in params.
Reverse migrates the note back to the vault it came from (captured in
``prior_state`` from the forward migrate's result).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID

from pydantic import BaseModel, Field

from memex_core.services.proposal_actions.base import (
    ActionValidationError,
    ExecuteResult,
    ProposalActionError,
    ReverseResult,
    register_action,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from memex_core.api import MemexAPI

logger = logging.getLogger(__name__)


class _RouteNoteToVaultParams(BaseModel):
    target_vault_id: str = Field(description='UUID of the vault to migrate the note into.')
    other_vault_ids: list[str] = Field(
        default_factory=list,
        description='UUIDs of the candidate vaults NOT chosen (negative routing signal).',
    )


class RouteNoteToVaultAction:
    id: ClassVar[str] = 'route_note_to_vault'
    name: ClassVar[str] = 'Route note to vault'
    description: ClassVar[str] = (
        'Migrate this note from the inbox to the chosen vault (params.target_vault_id). '
        'Reversible: undo migrates it back to the source vault.'
    )
    applicable_target_types: ClassVar[tuple[str, ...]] = ('note',)
    reversible: ClassVar[bool] = True
    params_schema: ClassVar[dict[str, Any] | None] = _RouteNoteToVaultParams.model_json_schema()

    def validate(self, params: dict[str, Any], *, target_type: str, target_id: str) -> None:
        if target_type != 'note':
            raise ActionValidationError(
                f'route_note_to_vault applies to note targets, not {target_type!r}.'
            )
        try:
            UUID(target_id)
        except (ValueError, AttributeError):
            raise ActionValidationError(f'target_id {target_id!r} is not a valid UUID.')
        target_vault = params.get('target_vault_id')
        if not target_vault:
            raise ActionValidationError('route_note_to_vault requires params.target_vault_id.')
        try:
            UUID(str(target_vault))
        except (ValueError, AttributeError):
            raise ActionValidationError(f'target_vault_id {target_vault!r} is not a valid UUID.')
        other_vault_ids = params.get('other_vault_ids')
        if other_vault_ids is not None:
            if not isinstance(other_vault_ids, list):
                raise ActionValidationError('params.other_vault_ids must be a list of UUIDs.')
            for other in other_vault_ids:
                try:
                    UUID(str(other))
                except (ValueError, AttributeError):
                    raise ActionValidationError(f'other_vault_id {other!r} is not a valid UUID.')

    async def execute(
        self,
        api: MemexAPI,
        params: dict[str, Any],
        *,
        target_id: str,
        vault_id: UUID | None,
        actor: str,
        session: AsyncSession | None = None,
    ) -> ExecuteResult:
        note_id = UUID(target_id)
        target_vault_id = UUID(str(params['target_vault_id']))
        result = await api.migrate_note(note_id, target_vault_id)
        # Feed the human confirmation back into the router's online model — a
        # manually-accepted route is a positive (label=1) for the chosen vault,
        # the same signal an auto-route records. Best-effort: never fail the
        # migration because learning hiccuped. This is what lets cockpit
        # confirmations count toward the warm-up gate.
        try:
            await api.inbox_router.record_feedback(note_id, target_vault_id, 1)
        except Exception:  # noqa: BLE001 - learning is best-effort
            logger.warning('route_note_to_vault: positive record_feedback failed', exc_info=True)
        # The other candidates the user did NOT pick are negatives — same learning
        # signal an auto-route records for its runners-up. Per-candidate try so a
        # stale candidate (vault renamed/removed since the proposal was created)
        # doesn't drop the rest of the negative signal.
        for other in params.get('other_vault_ids') or []:
            try:
                await api.inbox_router.record_feedback(note_id, UUID(str(other)), 0)
            except Exception:  # noqa: BLE001 - learning is best-effort
                logger.warning(
                    'route_note_to_vault: negative record_feedback failed for vault %s',
                    other,
                    exc_info=True,
                )
        source_vault_id = result.get('source_vault_id')
        if source_vault_id is None:
            # migrate_note is a no-op when source == target; nothing to reverse.
            source_vault_id = str(vault_id)
        return ExecuteResult(
            applied_state={
                'note_id': str(note_id),
                'target_vault_id': str(target_vault_id),
                'status': result.get('status'),
            },
            prior_state={'note_id': str(note_id), 'source_vault_id': str(source_vault_id)},
        )

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
        note_id = UUID(str(applied_state.get('note_id') or target_id))
        source_vault_id = prior_state.get('source_vault_id')
        if not source_vault_id:
            raise ProposalActionError(
                'cannot reverse route_note_to_vault: prior_state has no source_vault_id.'
            )
        await api.migrate_note(note_id, UUID(str(source_vault_id)))
        # A reversal is a strong negative: the route we applied was wrong, so
        # record label=0 for the vault we (incorrectly) routed to. Best-effort.
        applied_target = applied_state.get('target_vault_id')
        if applied_target:
            try:
                await api.inbox_router.record_feedback(note_id, UUID(str(applied_target)), 0)
            except Exception:  # noqa: BLE001 - learning is best-effort
                logger.warning('route_note_to_vault: reverse record_feedback failed', exc_info=True)
        return ReverseResult(
            restored_state={'note_id': str(note_id), 'vault_id': str(source_vault_id)}
        )

    async def preview(
        self,
        api: MemexAPI,
        params: dict[str, Any],
        *,
        target_id: str,
        vault_id: UUID | None,
    ) -> str:
        target_vault = params.get('target_vault_id', '<unspecified>')
        return f'Will migrate this note to vault {target_vault} (note + units + chunks + links).'


register_action(RouteNoteToVaultAction())
