"""`assign_case` — resolve a contested case → procedure assignment.

The Phase-1 prerequisite action from the design §19.7: ``case_submit``
escalates non-clean assignments (§19.3c separability rule) to the lint
surface with the candidates + judgment as evidence; the reviewer (human
or agent — file-then-lint, decision #5) resolves the finding with this
action.

Targets the case NOTE (entry-targeted resolves need no new machinery
this way — §19.7's verified path). Two modes:

* ``mode='assign'`` — attach the case to an existing procedural entry
  (``params.entry_id``): provenance edge + derivation enqueue.
* ``mode='new_procedure'`` — create a draft procedure anchor
  (``params.scope/verb/context/title``) and attach the case to it. The
  draft stays invisible to search/briefing until promoted.

Reversible: undo removes the provenance edge (and deprecates the draft
anchor this resolution created, when applicable).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Literal
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


class _AssignCaseParams(BaseModel):
    mode: Literal['assign', 'new_procedure'] = Field(
        description='assign: attach to params.entry_id. '
        'new_procedure: create a draft anchor from scope/verb/context/title.'
    )
    entry_id: str | None = Field(
        default=None,
        description='Target procedural entry UUID (mode=assign).',
    )
    scope: str | None = Field(
        default=None,
        description='Anchor scope for the new draft (mode=new_procedure).',
    )
    verb: str | None = Field(
        default=None,
        description='Anchor verb for the new draft (mode=new_procedure).',
    )
    context: str | None = Field(
        default=None,
        description='Anchor context for the new draft (mode=new_procedure).',
    )
    title: str | None = Field(
        default=None,
        description='Title for the new draft (mode=new_procedure).',
    )


class AssignCaseAction:
    id: ClassVar[str] = 'assign_case'
    name: ClassVar[str] = 'Assign case to procedure'
    description: ClassVar[str] = (
        'Attach the case note to a procedural entry (params.entry_id), or '
        'create a draft procedure anchor (params.scope/verb/context/title) '
        'and attach to it. Reversible: undo detaches the case (and '
        'deprecates a draft this resolution created).'
    )
    applicable_target_types: ClassVar[tuple[str, ...]] = ('note',)
    reversible: ClassVar[bool] = True
    params_schema: ClassVar[dict[str, Any] | None] = _AssignCaseParams.model_json_schema()

    def validate(self, params: dict[str, Any], *, target_type: str, target_id: str) -> None:
        if target_type != 'note':
            raise ActionValidationError(
                f'assign_case applies to note targets (the case note), not {target_type!r}.'
            )
        try:
            UUID(target_id)
        except (ValueError, AttributeError):
            raise ActionValidationError(f'target_id {target_id!r} is not a valid UUID.')
        mode = params.get('mode')
        if mode == 'assign':
            entry_id = params.get('entry_id')
            if not entry_id:
                raise ActionValidationError('mode=assign requires params.entry_id.')
            try:
                UUID(str(entry_id))
            except (ValueError, AttributeError):
                raise ActionValidationError(f'entry_id {entry_id!r} is not a valid UUID.')
        elif mode == 'new_procedure':
            missing = [k for k in ('scope', 'verb', 'context', 'title') if not params.get(k)]
            if missing:
                raise ActionValidationError(
                    f'mode=new_procedure requires params.{", params.".join(missing)}.'
                )
        else:
            raise ActionValidationError(
                f"params.mode must be 'assign' or 'new_procedure', got {mode!r}."
            )

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
        from memex_common.procedural_schemas import ProceduralEntryCreate
        from memex_core.services.case_service import CaseService

        note_id = UUID(target_id)
        created_entry_id: str | None = None

        if params['mode'] == 'new_procedure':
            if vault_id is None:
                raise ProposalActionError(
                    'assign_case (new_procedure) needs the finding vault_id '
                    'to home the draft anchor.'
                )
            # ``create`` (NOT ``upsert``): a 409 means the anchor already
            # exists as a real entry — the reviewer should pick
            # mode='assign' against it rather than overwrite it with a
            # stub draft. Surface that as an actionable validation error.
            from memex_core.services.procedural_repository import ProceduralIdentityConflict

            try:
                draft = await api.procedural.create(
                    ProceduralEntryCreate(
                        vault_id=vault_id,
                        kind='procedure',
                        scope=str(params['scope']),
                        verb=str(params['verb']),
                        context=str(params['context']),
                        title=str(params['title']),
                        summary=f'Draft anchor created by assign_case (actor: {actor}).',
                        trigger=str(params.get('trigger') or params['title']),
                        status='draft',
                        origin='derived',
                    )
                )
            except ProceduralIdentityConflict as exc:
                raise ProposalActionError(
                    f'a procedure already exists at ({params["scope"]}, '
                    f'{params["verb"]}, {params["context"]}) — resolve with '
                    "mode='assign' against it instead of creating a new anchor."
                ) from exc
            entry_id = draft.id
            created_entry_id = str(entry_id)
        else:
            entry_id = UUID(str(params['entry_id']))

        await CaseService(api).apply_assignment(note_id=note_id, entry_id=entry_id)

        return ExecuteResult(
            applied_state={
                'note_id': str(note_id),
                'entry_id': str(entry_id),
                'created_entry_id': created_entry_id,
            },
            prior_state={},
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
        from sqlmodel import col, select

        from memex_core.memory.sql_models import ProceduralSource

        note_id = UUID(str(applied_state['note_id']))
        entry_id = UUID(str(applied_state['entry_id']))

        # Detach: remove the provenance edge(s) this resolution created.
        async with api.metastore.session() as db:
            stmt = (
                select(ProceduralSource)
                .where(col(ProceduralSource.entry_id) == entry_id)
                .where(col(ProceduralSource.source_note_id) == note_id)
            )
            rows = (await db.exec(stmt)).all()
            for row in rows:
                await db.delete(row)
            await db.commit()

        # A draft anchor created by this resolution gets deprecated, not
        # deleted — the version ledger and audit trail stay intact.
        created = applied_state.get('created_entry_id')
        if created:
            await api.procedural.deprecate(UUID(str(created)))

        return ReverseResult(
            restored_state={
                'detached': len(rows),
                'deprecated_draft': created,
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
        if params.get('mode') == 'new_procedure':
            return (
                f'Create draft procedure ({params.get("scope")}, {params.get("verb")}, '
                f'{params.get("context")}) and attach case note {target_id} to it.'
            )
        return f'Attach case note {target_id} to procedural entry {params.get("entry_id")}.'


register_action(AssignCaseAction())
