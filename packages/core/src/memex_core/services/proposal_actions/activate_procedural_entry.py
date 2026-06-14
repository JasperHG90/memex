"""`activate_procedural_entry` — confirm a distilled draft into active (§18.6.1).

The derivation pass writes procedures/strategies as `status='draft'` (§9:
draft → confirm → active — only confirmed entries are retrievable) and
files a `governance` lint proposal targeting the draft. The reviewer
(human or agent, through the existing lint resolve route) confirms it with
this action, which promotes the entry draft → published. Reversible: undo
re-drafts it.

This is the first catalogue action for `target_type='procedural_entry'`
(§18.6 correction (c) flagged that entry-targeted resolves had no action;
this closes the distilled-draft flow).
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


class ActivateProceduralEntryAction:
    id: ClassVar[str] = 'activate_procedural_entry'
    name: ClassVar[str] = 'Activate distilled procedure/strategy'
    description: ClassVar[str] = (
        'Confirm a distilled draft procedure/strategy into active: promote '
        'status draft → published so it is retrievable. Reversible: undo '
        're-drafts it.'
    )
    applicable_target_types: ClassVar[tuple[str, ...]] = ('procedural_entry',)
    reversible: ClassVar[bool] = True
    params_schema: ClassVar[dict[str, Any] | None] = None

    def validate(self, params: dict[str, Any], *, target_type: str, target_id: str) -> None:
        if target_type != 'procedural_entry':
            raise ActionValidationError(
                f'activate_procedural_entry applies to procedural_entry targets, not {target_type!r}.'
            )
        try:
            UUID(target_id)
        except (ValueError, AttributeError):
            raise ActionValidationError(f'target_id {target_id!r} is not a valid UUID.')

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
            raise ProposalActionError(
                f'procedural entry {entry_id} not found — cannot activate.'
            ) from exc

        prior_status = current.status
        if prior_status == 'published':
            # Idempotent: already active. Record so reverse is a no-op-safe.
            return ExecuteResult(
                applied_state={'entry_id': str(entry_id), 'activated': False},
                prior_state={'status': prior_status},
            )

        await api.procedural.update(
            entry_id,
            ProceduralEntryUpdate(
                status='published',
                edited_by=f'activate:{actor}',
                edit_reason='Confirmed distilled draft → active (§18.6.1).',
            ),
        )
        return ExecuteResult(
            applied_state={'entry_id': str(entry_id), 'activated': True},
            prior_state={'status': prior_status},
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

        if not applied_state.get('activated'):
            return ReverseResult(restored_state={'redrafted': False})

        entry_id = UUID(str(applied_state['entry_id']))
        prior_status = prior_state.get('status') or 'draft'
        await api.procedural.update(
            entry_id,
            ProceduralEntryUpdate(
                status=prior_status,  # type: ignore[arg-type]
                edited_by=f'activate-undo:{actor}',
                edit_reason='Reverted activation → draft (§18.6.1 reverse).',
            ),
        )
        return ReverseResult(restored_state={'redrafted': True, 'status': prior_status})

    async def preview(
        self,
        api: 'MemexAPI',
        params: dict[str, Any],
        *,
        target_id: str,
        vault_id: UUID | None,
    ) -> str:
        return f'Activate procedural entry {target_id} (draft → published).'


register_action(ActivateProceduralEntryAction())
