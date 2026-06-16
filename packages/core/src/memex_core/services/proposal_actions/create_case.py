"""`create_case` — synthesize and file a NEW case from review.

The capture-via-review path: an agent (or operator) drafts a worked
episode from existing notes / episodic memory and files it as a lint
proposal instead of submitting the case directly. A human reviews the
draft in the CLI cockpit (`memex lint review` / `lint resolve`) and, on
approval, THIS action creates the case — running the full §5.1
submission flow (compose → ingest → stamp role='case' → assign).

Targets the SOURCE note the episode was distilled from (provenance +
the finding's anchor); the case itself is a brand-new note in the hidden
``procedural`` system vault. The case fields ride in ``params`` — the
reviewer sees them rendered from ``params_schema`` before approving.

Reversible: undo archives the created case note and unwinds the
assignment it triggered (detaches the provenance edge, deprecates a
draft anchor the assignment created, or dismisses an escalation finding).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from memex_core.services.proposal_actions.base import (
    ActionValidationError,
    ExecuteResult,
    ReverseResult,
    register_action,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from memex_core.api import MemexAPI


class _CreateCaseParams(BaseModel):
    title: str = Field(description='Case title (the task in a few words).')
    trigger: str = Field(description='What kicked the episode off / the symptom.')
    outcome: Literal['success', 'failure', 'mixed'] = Field(description='How it turned out.')
    situation: str = Field(default='', description='Prior context / constraints.')
    actions: list[str] = Field(default_factory=list, description='Ordered steps taken.')
    lesson: str = Field(default='', description='The takeaway worth carrying forward.')
    case_of: str | None = Field(
        default=None,
        description='Procedural entry UUID this case instantiates (skip to let the judge assign).',
    )
    project_id: str | None = Field(default=None, description='Provenance project id.')
    scope: str = Field(
        description='Identity scope for the case: "global" | "project:<id>" | "app:<id>".',
    )
    scope_reasoning: str = Field(
        description='One-sentence justification for the chosen scope.',
    )
    tags: list[str] = Field(default_factory=list, description='Extra case tags.')


class CreateCaseAction:
    id: ClassVar[str] = 'create_case'
    name: ClassVar[str] = 'Create case from notes'
    description: ClassVar[str] = (
        'Synthesize a worked episode (trigger / situation / actions / outcome / '
        'lesson) into a new case note and run assignment — the same flow as a '
        'direct case submission, gated behind human review. Reversible: undo '
        'archives the created case, detaches its provenance edge, deprecates a '
        'draft anchor it minted, and dismisses an escalation finding — but the '
        'append-only outcome counter the assignment bumped is NOT rolled back.'
    )
    applicable_target_types: ClassVar[tuple[str, ...]] = ('note',)
    reversible: ClassVar[bool] = True
    params_schema: ClassVar[dict[str, Any] | None] = _CreateCaseParams.model_json_schema()

    def validate(self, params: dict[str, Any], *, target_type: str, target_id: str) -> None:
        if target_type != 'note':
            raise ActionValidationError(
                f'create_case anchors on a source note, not {target_type!r}.'
            )
        try:
            UUID(target_id)
        except (ValueError, AttributeError):
            raise ActionValidationError(f'target_id {target_id!r} is not a valid UUID.')
        # The Pydantic model is the schema of record — coercing here raises a
        # readable message (the HTTP layer maps it to 400) before any write.
        try:
            parsed = _CreateCaseParams.model_validate(params)
        except Exception as exc:
            raise ActionValidationError(f'invalid create_case params: {exc}') from exc
        if not parsed.title.strip():
            raise ActionValidationError('params.title must be non-empty.')
        if not parsed.trigger.strip():
            raise ActionValidationError('params.trigger must be non-empty.')
        if parsed.case_of is not None:
            try:
                UUID(str(parsed.case_of))
            except (ValueError, AttributeError):
                raise ActionValidationError(f'case_of {parsed.case_of!r} is not a valid UUID.')

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
        from memex_common.procedural_schemas import CaseSubmit
        from memex_core.services.case_service import CaseService

        parsed = _CreateCaseParams.model_validate(params)
        payload = CaseSubmit(
            title=parsed.title,
            trigger=parsed.trigger,
            situation=parsed.situation,
            actions=parsed.actions,
            outcome=parsed.outcome,
            lesson=parsed.lesson,
            project_id=parsed.project_id,
            scope=parsed.scope,
            scope_reasoning=parsed.scope_reasoning,
            case_of=UUID(str(parsed.case_of)) if parsed.case_of else None,
            submitted_by=actor,
            tags=parsed.tags,
        )
        result = await CaseService(api).submit(payload)
        assignment = result.assignment
        return ExecuteResult(
            applied_state={
                'case_note_id': str(result.note_id),
                'case_vault_id': str(result.vault_id),
                'assignment_mode': assignment.mode,
                'entry_id': str(assignment.entry_id) if assignment.entry_id else None,
                'finding_id': str(assignment.finding_id) if assignment.finding_id else None,
                'source_note_id': target_id,
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

        case_note_id = UUID(str(applied_state['case_note_id']))
        mode = applied_state.get('assignment_mode')
        entry_id_raw = applied_state.get('entry_id')
        finding_id_raw = applied_state.get('finding_id')

        detached = 0
        deprecated_draft: str | None = None
        dismissed_finding: str | None = None

        # 1. Detach the provenance edge the assignment created (any mode that
        #    bound the case to an entry). Mirrors assign_case.reverse — the
        #    append-only outcome bump + derivation enqueue are NOT unwound
        #    (the draft this points at is deprecated below, and the queue
        #    dedups on pending rows).
        if entry_id_raw:
            entry_id = UUID(str(entry_id_raw))
            async with api.metastore.session() as db:
                stmt = (
                    select(ProceduralSource)
                    .where(col(ProceduralSource.entry_id) == entry_id)
                    .where(col(ProceduralSource.source_note_id) == case_note_id)
                )
                rows = (await db.exec(stmt)).all()
                for row in rows:
                    await db.delete(row)
                await db.commit()
            detached = len(rows)
            # A draft anchor the assignment minted exists only because of this
            # case — deprecate it (status→deprecated; ledger/audit stay intact).
            if mode == 'new_procedure_draft':
                await api.procedural.deprecate(entry_id)
                deprecated_draft = str(entry_id)

        # 2. An escalated assignment left a pending lint finding — dismiss it.
        if mode == 'escalated' and finding_id_raw:
            finding_id = UUID(str(finding_id_raw))
            await api.lint.set_status(finding_id, 'dismissed', actor=actor, vault_id=vault_id)
            dismissed_finding = str(finding_id)

        # 3. Archive the created case note (non-destructive: stamps archived_at
        #    and deprioritizes its units; the row and audit trail stay intact).
        await api.set_note_status(case_note_id, 'archived')

        return ReverseResult(
            restored_state={
                'archived_case_note': str(case_note_id),
                'detached': detached,
                'deprecated_draft': deprecated_draft,
                'dismissed_finding': dismissed_finding,
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
        title = str(params.get('title') or '(untitled)')
        outcome = str(params.get('outcome') or '?')
        link = params.get('case_of')
        tail = f' linked to procedure {link}' if link else ' (judge assigns)'
        return f'Create case {title!r} (outcome={outcome}) from note {target_id}{tail}.'


register_action(CreateCaseAction())
