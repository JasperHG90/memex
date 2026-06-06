"""`merge_entities` / `collapse_into_new_entity` — forward-only cluster merges.

Both operate on entity-cluster findings (e.g. `entity_collapse_cluster`).
Because the action protocol receives only ``params`` (never the finding's
evidence), ``member_ids`` is the explicit, authoritative merge list — the
cockpit fills it from ``evidence.cluster_members``; external callers supply
it directly. ``merge_entities`` folds the members onto an existing winner
picked from the list; ``collapse_into_new_entity`` creates a fresh survivor
named by the reviewer and folds ALL listed members onto it. Both delegate to
the audited `EntityService` collapse machinery and are NOT reversible — the
merged member entities are hard-deleted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID

from pydantic import BaseModel, Field, ValidationError

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


def _validate_entity_target(action_id: str, target_type: str, target_id: str) -> None:
    if target_type != 'entity':
        raise ActionValidationError(f'{action_id} applies to entity targets, not {target_type!r}.')
    try:
        UUID(target_id)
    except (ValueError, AttributeError):
        raise ActionValidationError(f'target_id {target_id!r} is not a valid UUID.')


def _parse_member_uuids(action_id: str, member_ids: list[str]) -> list[UUID]:
    parsed: list[UUID] = []
    for raw in member_ids:
        try:
            parsed.append(UUID(str(raw)))
        except (ValueError, AttributeError):
            raise ActionValidationError(f'{action_id}: member_id {raw!r} is not a valid UUID.')
    # Dedup is intentional and order-preserving: a caller that lists the same
    # entity twice gets it merged once (re-collapsing an entity into itself is a
    # no-op), so duplicates are silently dropped rather than rejected.
    deduped = list(dict.fromkeys(parsed))
    if len(deduped) < 2:
        raise ActionValidationError(
            f'{action_id} requires at least two distinct member_ids to merge.'
        )
    return deduped


class _MergeEntitiesParams(BaseModel):
    winner_id: str = Field(description='UUID of the surviving entity; must be one of member_ids.')
    member_ids: list[str] = Field(
        min_length=2,
        description=(
            'UUIDs of every entity in the merge (winner included) — the cockpit '
            "fills this from the finding's cluster_members evidence."
        ),
    )


class MergeEntitiesAction:
    id: ClassVar[str] = 'merge_entities'
    name: ClassVar[str] = 'Merge entities into winner'
    description: ClassVar[str] = (
        'Fold the listed entities onto the chosen winner: links, aliases, '
        'counters, and per-vault mental models merge; the losers are '
        'hard-deleted. NOT reversible.'
    )
    applicable_target_types: ClassVar[tuple[str, ...]] = ('entity',)
    reversible: ClassVar[bool] = False
    params_schema: ClassVar[dict[str, Any] | None] = _MergeEntitiesParams.model_json_schema()

    def validate(
        self,
        params: dict[str, Any],
        *,
        target_type: str,
        target_id: str,
    ) -> None:
        _validate_entity_target(self.id, target_type, target_id)
        try:
            parsed = _MergeEntitiesParams(**params)
        except ValidationError as exc:
            raise ActionValidationError(f'invalid merge_entities params: {exc}') from exc
        members = _parse_member_uuids(self.id, parsed.member_ids)
        try:
            winner = UUID(parsed.winner_id)
        except (ValueError, AttributeError):
            raise ActionValidationError(f'winner_id {parsed.winner_id!r} is not a valid UUID.')
        if winner not in members:
            raise ActionValidationError('winner_id must be one of the supplied member_ids.')

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
        from memex_common.exceptions import EntityNotFoundError

        parsed = _MergeEntitiesParams(**params)
        members = _parse_member_uuids(self.id, parsed.member_ids)
        winner = UUID(parsed.winner_id)
        losers = [m for m in members if m != winner]
        try:
            summary = await api.entities.collapse_cluster(
                winner_id=winner, loser_ids=losers, actor=actor
            )
        except EntityNotFoundError as exc:
            # A valid-but-nonexistent member UUID → clean 409, not a 500
            # surfacing from deep in the entity service.
            raise ProposalActionError(f'merge_entities: {exc}') from exc
        return ExecuteResult(
            applied_state={
                'winner_id': str(winner),
                'merged_member_ids': [str(m) for m in losers],
                'summary': summary,
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
            'merge_entities is forward-only: the merged entities were hard-deleted.'
        )

    async def preview(
        self,
        api: MemexAPI,
        params: dict[str, Any],
        *,
        target_id: str,
        vault_id: UUID | None,
    ) -> str:
        members = params.get('member_ids')
        count = len(members) if isinstance(members, list) else 0
        winner = params.get('winner_id', '<unspecified>')
        return (
            f'Will merge {max(count - 1, 0)} entities into winner {winner} '
            '(links, aliases, counters, mental models fold onto it; losers are '
            'hard-deleted). NOT reversible.'
        )


class _CollapseIntoNewEntityParams(BaseModel):
    new_canonical_name: str = Field(
        min_length=1,
        max_length=512,
        description='Canonical name for the freshly created surviving entity.',
    )
    member_ids: list[str] = Field(
        min_length=2,
        description=(
            'UUIDs of every entity to fold into the new survivor — the cockpit '
            "fills this from the finding's cluster_members evidence."
        ),
    )


class CollapseIntoNewEntityAction:
    id: ClassVar[str] = 'collapse_into_new_entity'
    name: ClassVar[str] = 'Collapse into a new entity'
    description: ClassVar[str] = (
        'Create a new entity with the given canonical name and fold ALL listed '
        'entities onto it (links, aliases, counters, mental models merge; the '
        'originals are hard-deleted). NOT reversible.'
    )
    applicable_target_types: ClassVar[tuple[str, ...]] = ('entity',)
    reversible: ClassVar[bool] = False
    params_schema: ClassVar[dict[str, Any] | None] = (
        _CollapseIntoNewEntityParams.model_json_schema()
    )

    def validate(
        self,
        params: dict[str, Any],
        *,
        target_type: str,
        target_id: str,
    ) -> None:
        _validate_entity_target(self.id, target_type, target_id)
        try:
            parsed = _CollapseIntoNewEntityParams(**params)
        except ValidationError as exc:
            raise ActionValidationError(f'invalid collapse_into_new_entity params: {exc}') from exc
        if not parsed.new_canonical_name.strip():
            raise ActionValidationError('new_canonical_name must be non-empty.')
        _parse_member_uuids(self.id, parsed.member_ids)

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
        from memex_common.exceptions import EntityNotFoundError

        parsed = _CollapseIntoNewEntityParams(**params)
        members = _parse_member_uuids(self.id, parsed.member_ids)
        try:
            summary = await api.entities.collapse_into_new_entity(
                member_ids=members,
                new_canonical_name=parsed.new_canonical_name,
                actor=actor,
            )
        except (ValueError, EntityNotFoundError) as exc:
            # ValueError = duplicate name / bad input; EntityNotFoundError =
            # a valid-but-nonexistent member UUID. Both → clean 409, not 500.
            raise ProposalActionError(str(exc)) from exc
        return ExecuteResult(
            applied_state={
                'created_entity_id': summary.get('created_entity_id'),
                'created_canonical_name': summary.get('created_canonical_name'),
                'merged_member_ids': [str(m) for m in members],
                'summary': summary,
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
            'collapse_into_new_entity is forward-only: the merged entities were hard-deleted.'
        )

    async def preview(
        self,
        api: MemexAPI,
        params: dict[str, Any],
        *,
        target_id: str,
        vault_id: UUID | None,
    ) -> str:
        members = params.get('member_ids')
        count = len(members) if isinstance(members, list) else 0
        name = params.get('new_canonical_name', '<unspecified>')
        return (
            f'Will create a new entity {name!r} and fold {count} entities '
            'onto it; the originals are hard-deleted. NOT reversible.'
        )


register_action(MergeEntitiesAction())
register_action(CollapseIntoNewEntityAction())
