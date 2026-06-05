"""`set_note_status` — reversible note lifecycle transition.

Delegates to `MemexAPI.set_note_status` ('active' | 'superseded' | 'archived').
The forward apply snapshots the note's lifecycle fields (status,
superseded_by, archived_at, appended_to) so reverse can re-apply them:
prior 'superseded' restores the supersede pointer, prior archived_at
re-archives (re-stamped at reverse time, not the original timestamp),
plain prior 'active' clears everything. A prior `appended_to` provenance
pointer cannot be restored through this path (the service rejects
'appended' as a settable status), so reverse refuses with a clear error
instead of silently dropping it.

Snapshot caveat: the prior-state SELECT and the status mutation run in
separate sessions (read-committed), so a concurrent edit between them
could make `prior_state` slightly stale — `reverse()` would then restore
that near-current state rather than a strictly-consistent snapshot. The
resolve route holds a row lock across execute, which serialises against
other lint resolutions of the same finding; an out-of-band note edit is
the only remaining window and is acceptable for this advisory path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, Field, ValidationError
from sqlmodel import select

from memex_core.memory.sql_models import Note
from memex_core.services.proposal_actions.base import (
    ActionValidationError,
    ExecuteResult,
    ProposalActionError,
    ReverseResult,
    register_action,
)

if TYPE_CHECKING:
    from memex_core.api import MemexAPI


class _SetNoteStatusParams(BaseModel):
    status: Literal['active', 'superseded', 'archived'] = Field(
        description='Lifecycle transition to apply to the note.'
    )
    linked_note_id: str | None = Field(
        default=None,
        description="UUID of the superseding note; only meaningful with status='superseded'.",
    )


class SetNoteStatusAction:
    id: ClassVar[str] = 'set_note_status'
    name: ClassVar[str] = 'Set note status'
    description: ClassVar[str] = (
        "Transition the note's lifecycle: 'superseded' marks its units stale, "
        "'archived' deprioritizes them, 'active' reactivates. Reversible "
        '(prior lifecycle fields are snapshotted and re-applied).'
    )
    applicable_target_types: ClassVar[tuple[str, ...]] = ('note',)
    reversible: ClassVar[bool] = True
    params_schema: ClassVar[dict[str, Any] | None] = _SetNoteStatusParams.model_json_schema()

    def validate(
        self,
        params: dict[str, Any],
        *,
        target_type: str,
        target_id: str,
    ) -> None:
        if target_type != 'note':
            raise ActionValidationError(
                f'set_note_status applies to note targets, not {target_type!r}.'
            )
        try:
            UUID(target_id)
        except (ValueError, AttributeError):
            raise ActionValidationError(f'target_id {target_id!r} is not a valid UUID.')
        try:
            parsed = _SetNoteStatusParams(**params)
        except ValidationError as exc:
            raise ActionValidationError(f'invalid set_note_status params: {exc}') from exc
        if parsed.linked_note_id is not None:
            try:
                UUID(parsed.linked_note_id)
            except (ValueError, AttributeError):
                raise ActionValidationError(
                    f'linked_note_id {parsed.linked_note_id!r} is not a valid UUID.'
                )

    async def execute(
        self,
        api: MemexAPI,
        params: dict[str, Any],
        *,
        target_id: str,
        vault_id: UUID | None,
        actor: str,
    ) -> ExecuteResult:
        parsed = _SetNoteStatusParams(**params)
        note_id = UUID(target_id)
        async with api.metastore.session() as session:
            stmt = select(
                Note.status, Note.superseded_by, Note.archived_at, Note.appended_to
            ).where(Note.id == note_id)
            if vault_id is not None:
                stmt = stmt.where(Note.vault_id == vault_id)
            result = await session.execute(stmt)
            row = result.first()
        if row is None:
            raise ProposalActionError(f'note {target_id} not found in vault.')
        prior_state = {
            'note_id': str(note_id),
            'status': row.status,
            'superseded_by': str(row.superseded_by) if row.superseded_by else None,
            'archived_at': row.archived_at.isoformat() if row.archived_at else None,
            'appended_to': str(row.appended_to) if row.appended_to else None,
        }
        linked = UUID(parsed.linked_note_id) if parsed.linked_note_id else None
        await api.set_note_status(note_id, parsed.status, linked)
        return ExecuteResult(
            applied_state={
                'note_id': str(note_id),
                'status': parsed.status,
                'linked_note_id': parsed.linked_note_id,
            },
            prior_state=prior_state,
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
        if prior_state.get('appended_to'):
            raise ProposalActionError(
                'cannot reverse set_note_status: the prior appended_to pointer is not '
                'restorable through lifecycle transitions; re-append the content instead.'
            )
        note_id = UUID(str(prior_state.get('note_id') or target_id))
        prior_status = str(prior_state.get('status') or 'active')
        prior_superseded = prior_state.get('superseded_by')
        # 'superseded' and 'archived' coexist: NoteService.set_note_status(
        # 'archived') only stamps archived_at — it does NOT touch status or
        # superseded_by — so re-applying the supersede pointer first and
        # re-archiving second restores BOTH signals without losing the pointer.
        # Unknown statuses are refused rather than silently collapsed to
        # 'active', so a future lifecycle state cannot mis-restore here.
        if prior_status == 'superseded':
            await api.set_note_status(
                note_id, 'superseded', UUID(str(prior_superseded)) if prior_superseded else None
            )
        elif prior_status in ('active', 'archived'):
            # 'archived''s base is the active note it was archived from; the
            # archived_at re-stamp below reconstructs the archived signal.
            await api.set_note_status(note_id, 'active')
        else:
            raise ProposalActionError(
                f'cannot reverse set_note_status: unhandled prior status {prior_status!r}.'
            )
        if prior_state.get('archived_at'):
            await api.set_note_status(note_id, 'archived')
        return ReverseResult(
            restored_state={
                'note_id': str(note_id),
                'status': prior_status,
                'superseded_by': str(prior_superseded) if prior_superseded else None,
                'archived': bool(prior_state.get('archived_at')),
            }
        )

    async def preview(
        self,
        api: MemexAPI,
        params: dict[str, Any],
        *,
        target_id: str,
        vault_id: UUID | None,
    ) -> str:
        status = params.get('status', '<unspecified>')
        cascade = {
            'superseded': 'every memory unit goes stale and mental-model evidence is pruned',
            'archived': 'archived_at is stamped and every unit is deprioritized',
            'active': 'units reactivate and archive/supersede pointers clear',
        }.get(str(status), 'lifecycle fields change per the requested status')
        return f'Will set note status to {status!r}; {cascade}.'


register_action(SetNoteStatusAction())
