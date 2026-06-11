"""`set_note_role` — promote a note to a case on confirmation (§18.6.2).

The EVENT-cluster auto-promotion pass (§8) proposes worked-episode notes as
cases via the lint surface (``target_type='note'``). The reviewer confirms
with this action, which stamps ``role='case'`` (the §18.3 distillation
gate). Reversible: undo restores the prior role.

This is the §18.6.2 "auto-proposed cases" flow — JG's "this could be a
lint proposal" note, as-built.
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

# Roles this action may set — kept tight: it exists to promote worked
# episodes to cases, not as a general role editor.
_ALLOWED_ROLES = ('case', None)


class SetNoteRoleAction:
    id: ClassVar[str] = 'set_note_role'
    name: ClassVar[str] = 'Set note role (promote to case)'
    description: ClassVar[str] = (
        "Stamp a note's role (default 'case' — the distillation gate). "
        'Reversible: undo restores the prior role.'
    )
    applicable_target_types: ClassVar[tuple[str, ...]] = ('note',)
    reversible: ClassVar[bool] = True
    params_schema: ClassVar[dict[str, Any] | None] = None

    def validate(self, params: dict[str, Any], *, target_type: str, target_id: str) -> None:
        if target_type != 'note':
            raise ActionValidationError(
                f'set_note_role applies to note targets, not {target_type!r}.'
            )
        try:
            UUID(target_id)
        except (ValueError, AttributeError):
            raise ActionValidationError(f'target_id {target_id!r} is not a valid UUID.')
        role = params.get('role', 'case')
        if role not in _ALLOWED_ROLES:
            raise ActionValidationError(f"role must be 'case' or null, got {role!r}.")

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
        from memex_core.memory.sql_models import Note

        note_id = UUID(target_id)
        role = params.get('role', 'case')
        async with api.metastore.session() as db:
            note = await db.get(Note, note_id)
            if note is None:
                raise ProposalActionError(f'note {note_id} not found — cannot set role.')
            prior_role = note.role
            note.role = role
            db.add(note)
            await db.commit()
        return ExecuteResult(
            applied_state={'note_id': str(note_id), 'role': role},
            prior_state={'role': prior_role},
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
        from memex_core.memory.sql_models import Note

        note_id = UUID(str(applied_state['note_id']))
        async with api.metastore.session() as db:
            note = await db.get(Note, note_id)
            if note is not None:
                note.role = prior_state.get('role')
                db.add(note)
                await db.commit()
        return ReverseResult(restored_state={'role': prior_state.get('role')})

    async def preview(
        self,
        api: 'MemexAPI',
        params: dict[str, Any],
        *,
        target_id: str,
        vault_id: UUID | None,
    ) -> str:
        role = params.get('role', 'case')
        return f'Set note {target_id} role → {role!r}.'


register_action(SetNoteRoleAction())
