"""`apply_derivation` — apply a proposed distillation to an authored entry (§18.6.4).

A hand-edited (``origin='authored'``) procedure/strategy is never
auto-updated by derivation. When derivation wants to change one it files a
proposal carrying the distilled diff; the reviewer resolves it with this
action, which applies the diff as a NEW version (the version ledger keeps
the prior hand-edit). ``origin`` stays ``authored``. Reversible: undo
re-applies the prior content as another version (non-destructive).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID

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

_CONTENT_FIELDS = ('title', 'summary', 'body', 'trigger')


class ApplyDerivationAction:
    id: ClassVar[str] = 'apply_derivation'
    name: ClassVar[str] = 'Apply derivation to authored entry'
    description: ClassVar[str] = (
        'Apply a proposed distillation diff to a hand-authored procedure/'
        'strategy as a new version (origin stays authored). Reversible: undo '
        'restores the prior content as another version.'
    )
    applicable_target_types: ClassVar[tuple[str, ...]] = ('procedural_entry',)
    reversible: ClassVar[bool] = True
    params_schema: ClassVar[dict[str, Any] | None] = None

    def validate(self, params: dict[str, Any], *, target_type: str, target_id: str) -> None:
        if target_type != 'procedural_entry':
            raise ActionValidationError(
                f'apply_derivation applies to procedural_entry targets, not {target_type!r}.'
            )
        try:
            UUID(target_id)
        except (ValueError, AttributeError):
            raise ActionValidationError(f'target_id {target_id!r} is not a valid UUID.')
        if not any(params.get(f) for f in _CONTENT_FIELDS):
            raise ActionValidationError(
                'apply_derivation requires at least one of title/summary/body/trigger in params.'
            )

    async def execute(
        self,
        api: 'MemexAPI',
        params: dict[str, Any],
        *,
        target_id: str,
        vault_id: UUID | None,
        actor: str,
        session: 'AsyncSession | None' = None,
    ) -> ExecuteResult:
        from memex_common.procedural_schemas import ProceduralEntryUpdate
        from memex_core.services.procedural_repository import ProceduralEntryNotFound

        entry_id = UUID(target_id)
        try:
            current = await api.procedural.get(entry_id)
        except ProceduralEntryNotFound as exc:
            raise ProposalActionError(f'procedural entry {entry_id} not found.') from exc

        prior = {f: getattr(current, f) for f in _CONTENT_FIELDS}
        update_kwargs = {f: params[f] for f in _CONTENT_FIELDS if params.get(f) is not None}
        await api.procedural.update(
            entry_id,
            ProceduralEntryUpdate(
                **update_kwargs,
                edited_by=f'apply_derivation:{actor}',
                edit_reason='Applied derivation diff to authored entry (§18.6.4).',
            ),
        )
        return ExecuteResult(
            applied_state={'entry_id': str(entry_id)},
            prior_state={'content': prior},
        )

    async def reverse(
        self,
        api: 'MemexAPI',
        params: dict[str, Any],
        applied_state: dict[str, Any],
        prior_state: dict[str, Any],
        *,
        target_id: str,
        vault_id: UUID | None,
        actor: str,
    ) -> ReverseResult:
        from memex_common.procedural_schemas import ProceduralEntryUpdate

        entry_id = UUID(str(applied_state['entry_id']))
        prior = prior_state.get('content') or {}
        restore = {f: prior[f] for f in _CONTENT_FIELDS if prior.get(f) is not None}
        if restore:
            await api.procedural.update(
                entry_id,
                ProceduralEntryUpdate(
                    **restore,
                    edited_by=f'apply_derivation-undo:{actor}',
                    edit_reason='Reverted applied derivation (§18.6.4 reverse).',
                ),
            )
        return ReverseResult(restored_state={'restored': bool(restore)})

    async def preview(
        self,
        api: 'MemexAPI',
        params: dict[str, Any],
        *,
        target_id: str,
        vault_id: UUID | None,
    ) -> str:
        fields = ', '.join(f for f in _CONTENT_FIELDS if params.get(f))
        return f'Apply derivation diff ({fields}) to authored entry {target_id} as a new version.'


register_action(ApplyDerivationAction())
