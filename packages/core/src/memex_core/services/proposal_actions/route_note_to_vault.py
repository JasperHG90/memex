"""`route_note_to_vault` — reversible note migration for routing proposals.

Moves a note from its current vault (the inbox) to a target vault via
``MemexAPI.migrate_note``. The cockpit renders one option per candidate vault
(from ``evidence.top_candidates``) and supplies ``target_vault_id`` in params.
Reverse migrates the note back to the vault it came from (captured in
``prior_state`` from the forward migrate's result).

The in-core inbox router (and its online-learning feedback loop) was removed in
V6: routing is now owned by the external triage-inbox skill, which learns from
resolved findings on its own side. This action performs only the migration.
"""

from __future__ import annotations

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


class _RouteNoteToVaultParams(BaseModel):
    target_vault_id: str = Field(description='UUID of the vault to migrate the note into.')
    other_vault_ids: list[str] = Field(
        default_factory=list,
        description=(
            'UUIDs of the candidate vaults NOT chosen. Accepted for cockpit '
            'back-compat; no longer consumed (the in-core learning loop was '
            'removed in V6). Still validated as UUIDs.'
        ),
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
        source_vault_id = result.get('source_vault_id')
        if source_vault_id is None and vault_id is not None:
            # migrate_note is a no-op when source == target; the finding's vault
            # IS the known source. Guard on vault_id: a global (NULL-vault)
            # finding has no source, so leave it None — str(None) would persist
            # the literal 'None' and 500 on reverse's UUID('None'). reverse()
            # refuses cleanly on a missing/None source_vault_id instead.
            source_vault_id = vault_id
        return ExecuteResult(
            applied_state={
                'note_id': str(note_id),
                'target_vault_id': str(target_vault_id),
                'status': result.get('status'),
            },
            prior_state={
                'note_id': str(note_id),
                'source_vault_id': str(source_vault_id) if source_vault_id is not None else None,
            },
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
