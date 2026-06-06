"""`update_note_title` / `update_note_date` — reversible note-metadata fixes.

Both snapshot the prior value before delegating to the `MemexAPI` facade
(`update_note_title` re-extracts embedded title facts; `update_note_date`
cascades to child memory-unit timestamps). Reverse re-applies the prior
value through the same facade so the cascades stay consistent. A prior
NULL value cannot be re-applied (the facades require a concrete value),
so reverse refuses with a clear error in that case.

Snapshot caveat: the prior-value SELECT and the mutation run in separate
sessions (read-committed), so a concurrent edit between them could make
the snapshot slightly stale; reverse would then restore that near-current
value. The resolve route's row lock serialises against other lint
resolutions of the same finding — an out-of-band edit is the only window.

Date-reverse caveat: the service recomputes the unit-timestamp delta from
the note's CURRENT publish date, so a reverse restores the note-level
date exactly but child unit timestamps only when nothing else moved them
in between — approximate, like any cascade-based undo.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, ClassVar
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
    from sqlalchemy.ext.asyncio import AsyncSession

    from memex_core.api import MemexAPI


def _validate_note_target(action_id: str, target_type: str, target_id: str) -> None:
    if target_type != 'note':
        raise ActionValidationError(f'{action_id} applies to note targets, not {target_type!r}.')
    try:
        UUID(target_id)
    except (ValueError, AttributeError):
        raise ActionValidationError(f'target_id {target_id!r} is not a valid UUID.')


def _parse_iso_datetime(raw: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ActionValidationError(
            f'new_date {raw!r} is not ISO-8601 (YYYY-MM-DD or full timestamp).'
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class _UpdateNoteTitleParams(BaseModel):
    new_title: str = Field(min_length=1, max_length=512, description='Replacement note title.')


class UpdateNoteTitleAction:
    id: ClassVar[str] = 'update_note_title'
    name: ClassVar[str] = 'Update note title'
    description: ClassVar[str] = (
        "Replace the note's title (embedded title facts are re-extracted). "
        'Reversible: the prior title is snapshotted and restored on reverse.'
    )
    applicable_target_types: ClassVar[tuple[str, ...]] = ('note',)
    reversible: ClassVar[bool] = True
    params_schema: ClassVar[dict[str, Any] | None] = _UpdateNoteTitleParams.model_json_schema()

    def validate(
        self,
        params: dict[str, Any],
        *,
        target_type: str,
        target_id: str,
    ) -> None:
        _validate_note_target(self.id, target_type, target_id)
        try:
            _UpdateNoteTitleParams(**params)
        except ValidationError as exc:
            raise ActionValidationError(f'invalid update_note_title params: {exc}') from exc

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
        parsed = _UpdateNoteTitleParams(**params)
        note_id = UUID(target_id)
        async with api.metastore.session() as session:
            stmt = select(Note.title).where(Note.id == note_id)
            if vault_id is not None:
                stmt = stmt.where(Note.vault_id == vault_id)
            result = await session.execute(stmt)
            row = result.first()
        if row is None:
            raise ProposalActionError(f'note {target_id} not found in vault.')
        await api.update_note_title(note_id, parsed.new_title)
        return ExecuteResult(
            applied_state={'note_id': str(note_id), 'title': parsed.new_title},
            prior_state={'note_id': str(note_id), 'title': row.title},
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
        prior_title = prior_state.get('title')
        if prior_title is None:
            raise ProposalActionError(
                'cannot reverse update_note_title: the prior title was unset.'
            )
        note_id = UUID(str(prior_state.get('note_id') or target_id))
        await api.update_note_title(note_id, str(prior_title))
        return ReverseResult(restored_state={'note_id': str(note_id), 'title': prior_title})

    async def preview(
        self,
        api: MemexAPI,
        params: dict[str, Any],
        *,
        target_id: str,
        vault_id: UUID | None,
    ) -> str:
        new_title = params.get('new_title', '<unspecified>')
        return f'Will retitle this note to {new_title!r} and re-extract its title facts.'


class _UpdateNoteDateParams(BaseModel):
    new_date: str = Field(
        description='ISO-8601 publish date (YYYY-MM-DD or full timestamp) to set on the note.'
    )


class UpdateNoteDateAction:
    id: ClassVar[str] = 'update_note_date'
    name: ClassVar[str] = 'Update note date'
    description: ClassVar[str] = (
        "Replace the note's publish date; child memory-unit timestamps cascade. "
        'Reversible: the prior date is snapshotted and restored on reverse.'
    )
    applicable_target_types: ClassVar[tuple[str, ...]] = ('note',)
    reversible: ClassVar[bool] = True
    params_schema: ClassVar[dict[str, Any] | None] = _UpdateNoteDateParams.model_json_schema()

    def validate(
        self,
        params: dict[str, Any],
        *,
        target_type: str,
        target_id: str,
    ) -> None:
        _validate_note_target(self.id, target_type, target_id)
        try:
            parsed = _UpdateNoteDateParams(**params)
        except ValidationError as exc:
            raise ActionValidationError(f'invalid update_note_date params: {exc}') from exc
        _parse_iso_datetime(parsed.new_date)

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
        parsed = _UpdateNoteDateParams(**params)
        new_date = _parse_iso_datetime(parsed.new_date)
        note_id = UUID(target_id)
        async with api.metastore.session() as session:
            stmt = select(Note.publish_date).where(Note.id == note_id)
            if vault_id is not None:
                stmt = stmt.where(Note.vault_id == vault_id)
            result = await session.execute(stmt)
            row = result.first()
        if row is None:
            raise ProposalActionError(f'note {target_id} not found in vault.')
        await api.update_note_date(note_id, new_date)
        return ExecuteResult(
            applied_state={'note_id': str(note_id), 'publish_date': new_date.isoformat()},
            prior_state={
                'note_id': str(note_id),
                'publish_date': row.publish_date.isoformat() if row.publish_date else None,
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
        prior_date = prior_state.get('publish_date')
        if not prior_date:
            raise ProposalActionError(
                'cannot reverse update_note_date: the prior publish date was unset.'
            )
        note_id = UUID(str(prior_state.get('note_id') or target_id))
        await api.update_note_date(note_id, _parse_iso_datetime(str(prior_date)))
        return ReverseResult(restored_state={'note_id': str(note_id), 'publish_date': prior_date})

    async def preview(
        self,
        api: MemexAPI,
        params: dict[str, Any],
        *,
        target_id: str,
        vault_id: UUID | None,
    ) -> str:
        new_date = params.get('new_date', '<unspecified>')
        return (
            f"Will set this note's publish date to {new_date!r}; "
            'child memory-unit timestamps cascade.'
        )


register_action(UpdateNoteTitleAction())
register_action(UpdateNoteDateAction())
