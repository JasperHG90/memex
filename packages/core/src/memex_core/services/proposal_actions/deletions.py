"""`delete_note` / `delete_entity` / `delete_mental_model` — fenced hard deletes.

Forward-only, irreversible removals. Containment is layered: the resolve
endpoint's attended-mode gate covers every canned action, the human picks
the action in the cockpit, and each action here ships a blast-radius
``preview()`` (computed live from the database) that the cockpit renders
before confirmation. The lifecycle-state alternatives (`set_note_status`,
`archive_mental_model`, `deprioritize_unit`) remain the P4-consistent
default; these exist for the cases where content must actually go away.

``execute()`` snapshots the blast-radius counts into ``applied_state`` so
the resolution record carries what was destroyed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID

from sqlalchemy import func
from sqlmodel import select

from memex_core.memory.sql_models import Chunk, Entity, MemoryUnit, MentalModel, Note
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


# The vault guard scopes the row to the finding's vault when one is bound, and
# matches by id alone when ``vault_id`` is None (global finding). Implemented as
# a Python conditional on the select statement: the base statement matches by id
# only, and a ``.where(<Model>.vault_id == vault_id)`` clause is appended exactly
# when ``vault_id is not None`` — i.e. a None vault means "don't constrain by
# vault", not "match no rows".
def _validate_uuid_target(action_id: str, expected: str, target_type: str, target_id: str) -> None:
    if target_type != expected:
        raise ActionValidationError(
            f'{action_id} applies to {expected} targets, not {target_type!r}.'
        )
    try:
        UUID(target_id)
    except (ValueError, AttributeError):
        raise ActionValidationError(f'target_id {target_id!r} is not a valid UUID.')


class DeleteNoteAction:
    id: ClassVar[str] = 'delete_note'
    name: ClassVar[str] = 'Delete note (permanent)'
    description: ClassVar[str] = (
        'Permanently delete the note and everything derived from it: memory '
        'units, chunks, nodes, links, and filestore assets. NOT reversible — '
        'prefer set_note_status(archived) unless the content must go away.'
    )
    applicable_target_types: ClassVar[tuple[str, ...]] = ('note',)
    reversible: ClassVar[bool] = False
    params_schema: ClassVar[dict[str, Any] | None] = None

    def validate(
        self,
        params: dict[str, Any],
        *,
        target_type: str,
        target_id: str,
    ) -> None:
        _validate_uuid_target(self.id, 'note', target_type, target_id)

    async def _blast_radius(self, api: MemexAPI, note_id: UUID, vault_id: UUID | None) -> Any:
        unit_count = (
            select(func.count())
            .select_from(MemoryUnit)
            .where(MemoryUnit.note_id == Note.id)
            .scalar_subquery()
        )
        chunk_count = (
            select(func.count())
            .select_from(Chunk)
            .where(Chunk.note_id == Note.id)
            .scalar_subquery()
        )
        stmt = select(
            Note.title,
            unit_count.label('unit_count'),
            chunk_count.label('chunk_count'),
        ).where(Note.id == note_id)
        if vault_id is not None:
            stmt = stmt.where(Note.vault_id == vault_id)
        async with api.metastore.session() as session:
            result = await session.execute(stmt)
            return result.first()

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
        row = await self._blast_radius(api, note_id, vault_id)
        if row is None:
            raise ProposalActionError(f'note {target_id} not found in vault.')
        await api.delete_note(note_id)
        return ExecuteResult(
            applied_state={
                'note_id': str(note_id),
                'title': row.title,
                'units_deleted': int(row.unit_count),
                'chunks_deleted': int(row.chunk_count),
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
        raise ProposalActionError('delete_note is forward-only: the note was hard-deleted.')

    async def preview(
        self,
        api: MemexAPI,
        params: dict[str, Any],
        *,
        target_id: str,
        vault_id: UUID | None,
    ) -> str:
        try:
            row = await self._blast_radius(api, UUID(target_id), vault_id)
        except (ValueError, AttributeError):
            return 'Will permanently delete this note and all derived data. NOT reversible.'
        if row is None:
            return 'Note not found in this vault — execute would refuse.'
        return (
            f'Will permanently delete note {row.title!r} plus {int(row.unit_count)} memory '
            f'unit(s), {int(row.chunk_count)} chunk(s), its nodes, links, and filestore '
            'assets. Orphaned entities are cleaned in the background. NOT reversible.'
        )


class DeleteEntityAction:
    id: ClassVar[str] = 'delete_entity'
    name: ClassVar[str] = 'Delete entity (permanent)'
    description: ClassVar[str] = (
        'Permanently delete the entity plus its mental models, aliases, links, '
        'and unit-entity rows. NOT reversible — prefer the merge actions when '
        'the entity is a duplicate rather than wrong.'
    )
    applicable_target_types: ClassVar[tuple[str, ...]] = ('entity',)
    reversible: ClassVar[bool] = False
    params_schema: ClassVar[dict[str, Any] | None] = None

    def validate(
        self,
        params: dict[str, Any],
        *,
        target_type: str,
        target_id: str,
    ) -> None:
        _validate_uuid_target(self.id, 'entity', target_type, target_id)

    async def _blast_radius(self, api: MemexAPI, entity_id: UUID) -> Any:
        model_count = (
            select(func.count())
            .select_from(MentalModel)
            .where(MentalModel.entity_id == Entity.id)
            .scalar_subquery()
        )
        stmt = select(
            Entity.canonical_name,
            Entity.mention_count,
            model_count.label('model_count'),
        ).where(Entity.id == entity_id)
        async with api.metastore.session() as session:
            result = await session.execute(stmt)
            return result.first()

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
        entity_id = UUID(target_id)
        row = await self._blast_radius(api, entity_id)
        if row is None:
            raise ProposalActionError(f'entity {target_id} not found.')
        await api.delete_entity(entity_id)
        return ExecuteResult(
            applied_state={
                'entity_id': str(entity_id),
                'canonical_name': row.canonical_name,
                'mention_count': int(row.mention_count),
                'mental_models_deleted': int(row.model_count),
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
        raise ProposalActionError('delete_entity is forward-only: the entity was hard-deleted.')

    async def preview(
        self,
        api: MemexAPI,
        params: dict[str, Any],
        *,
        target_id: str,
        vault_id: UUID | None,
    ) -> str:
        try:
            row = await self._blast_radius(api, UUID(target_id))
        except (ValueError, AttributeError):
            return 'Will permanently delete this entity and all its graph state. NOT reversible.'
        if row is None:
            return 'Entity not found — execute would refuse.'
        return (
            f'Will permanently delete entity {row.canonical_name!r} '
            f'({int(row.mention_count)} mention(s)) plus {int(row.model_count)} mental '
            'model(s), its aliases, links, cooccurrences, and unit-entity rows. '
            'NOT reversible.'
        )


class DeleteMentalModelAction:
    id: ClassVar[str] = 'delete_mental_model'
    name: ClassVar[str] = 'Delete mental model (permanent)'
    description: ClassVar[str] = (
        "Permanently delete this vault's mental model for the entity (the "
        'entity itself is untouched). NOT reversible — prefer '
        'archive_mental_model unless the model must go away.'
    )
    # entity-only: execute keys the delete on entity_id (the MM belonging to an
    # entity in the finding's vault). A `mental_model`-typed finding carries
    # target_id = mental_model.id (NOT entity_id), so this action cannot honour
    # it — those findings resolve via archive_mental_model (which keys on .id).
    applicable_target_types: ClassVar[tuple[str, ...]] = ('entity',)
    reversible: ClassVar[bool] = False
    params_schema: ClassVar[dict[str, Any] | None] = None

    def validate(
        self,
        params: dict[str, Any],
        *,
        target_type: str,
        target_id: str,
    ) -> None:
        if target_type not in self.applicable_target_types:
            raise ActionValidationError(
                f'delete_mental_model applies to entity targets, not {target_type!r}.'
            )
        try:
            UUID(target_id)
        except (ValueError, AttributeError):
            raise ActionValidationError(f'target_id {target_id!r} is not a valid UUID.')

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
        entity_id = UUID(target_id)
        if vault_id is None:
            # A mental model is one (entity_id, vault_id) row; without a vault
            # there is nothing to scope the delete to. Mirrors the CAST-NULL
            # guards on the note actions rather than 500-ing on str(None).
            raise ProposalActionError(
                'delete_mental_model requires a vault-scoped finding; '
                f'finding for entity {target_id} has no vault.'
            )
        stmt = select(
            func.coalesce(func.jsonb_array_length(MentalModel.observations), 0).label(
                'observation_count'
            )
        ).where(MentalModel.entity_id == entity_id, MentalModel.vault_id == vault_id)
        async with api.metastore.session() as session:
            result = await session.execute(stmt)
            row = result.first()
        if row is None:
            raise ProposalActionError(
                f'no mental model for entity {target_id} in this vault; nothing to delete.'
            )
        await api.delete_mental_model(entity_id, vault_id)
        return ExecuteResult(
            applied_state={
                'entity_id': str(entity_id),
                'vault_id': str(vault_id),
                'observations_deleted': int(row.observation_count),
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
        raise ProposalActionError(
            'delete_mental_model is forward-only: the model row was hard-deleted. '
            'Reflection will rebuild a fresh model from the surviving units over time.'
        )

    async def preview(
        self,
        api: MemexAPI,
        params: dict[str, Any],
        *,
        target_id: str,
        vault_id: UUID | None,
    ) -> str:
        try:
            entity_id = UUID(target_id)
        except (ValueError, AttributeError):
            return "Will permanently delete this vault's mental model. NOT reversible."
        if vault_id is None:
            # Mirror execute()'s guard: a mental model is an (entity, vault) row,
            # so a vault-less finding has nothing to preview; binding str(None)
            # into the uuid-cast SQL would raise rather than match zero rows.
            return 'No vault-scoped mental model for this entity — execute would refuse.'
        stmt = select(
            func.coalesce(func.jsonb_array_length(MentalModel.observations), 0).label(
                'observation_count'
            )
        ).where(MentalModel.entity_id == entity_id, MentalModel.vault_id == vault_id)
        async with api.metastore.session() as session:
            result = await session.execute(stmt)
            row = result.first()
        if row is None:
            return 'No mental model for this entity in this vault — execute would refuse.'
        return (
            f"Will permanently delete this vault's mental model "
            f'({int(row.observation_count)} observation(s)); the parent entity is untouched. '
            'NOT reversible.'
        )


register_action(DeleteNoteAction())
register_action(DeleteEntityAction())
register_action(DeleteMentalModelAction())
