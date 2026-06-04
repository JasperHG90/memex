"""`archive_mental_model` — reversible soft-delete of a mental model.

Sets `mental_models.archived_at = now()`; retrieval, survey, and reflection
read-paths filter `WHERE archived_at IS NULL`. Reverse clears the column.
Row content is untouched on either direction — the action only flips the
visibility flag, so a stale model stays whole on disk.

Used by the `orphan_mental_model` SQL rule (`services/lint.py`) when a
model has had zero linked active units for >30 days.

Limitation (auto-apply): execute() opens its own session via
``api.metastore.session()`` and commits independently. If the caller's
subsequent status flip fails, the archive persists with no resolved
proposal — the side effect is real but the proposal stays pending.
The auto-apply layer logs a structured warning when this happens.

TODO: accept an optional shared session so the archive + status flip
can commit atomically. Requires a deeper refactor of the action protocol.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID

from sqlalchemy import text

from memex_core.services.proposal_actions.base import (
    ActionValidationError,
    ExecuteResult,
    ProposalActionError,
    ReverseResult,
    register_action,
)

if TYPE_CHECKING:
    from memex_core.api import MemexAPI


_ARCHIVE_SQL = text("""
    UPDATE mental_models
    SET archived_at = now()
    WHERE id = :id
      AND (CAST(:vault_id AS uuid) IS NULL OR vault_id = CAST(:vault_id AS uuid))
      AND archived_at IS NULL
    RETURNING archived_at
""")


_UNARCHIVE_SQL = text("""
    UPDATE mental_models
    SET archived_at = NULL
    WHERE id = :id
      AND (CAST(:vault_id AS uuid) IS NULL OR vault_id = CAST(:vault_id AS uuid))
      AND archived_at IS NOT NULL
    RETURNING id
""")


class ArchiveMentalModelAction:
    id: ClassVar[str] = 'archive_mental_model'
    name: ClassVar[str] = 'Archive mental model'
    description: ClassVar[str] = (
        'Set archived_at on this mental model so it is hidden from retrieval, '
        'survey, and reflection. Reversible (reverse clears archived_at).'
    )
    applicable_target_types: ClassVar[tuple[str, ...]] = ('mental_model',)
    reversible: ClassVar[bool] = True
    params_schema: ClassVar[dict[str, Any] | None] = None

    def validate(
        self,
        params: dict[str, Any],
        *,
        target_type: str,
        target_id: str,
    ) -> None:
        if target_type != 'mental_model':
            raise ActionValidationError(
                f'archive_mental_model applies to mental_model targets, not {target_type!r}.'
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
    ) -> ExecuteResult:
        async with api.metastore.session() as session:
            result = await session.execute(
                _ARCHIVE_SQL,
                {
                    'id': UUID(target_id),
                    'vault_id': str(vault_id) if vault_id is not None else None,
                },
            )
            row = result.first()
            await session.commit()
        if row is None:
            # Either the model is in another vault or already archived. CAS
            # guard refuses both — surface the precise reason so the cockpit
            # can render it.
            raise ProposalActionError(
                f'mental model {target_id} not found in vault, or already archived.'
            )
        return ExecuteResult(
            applied_state={
                'mental_model_id': target_id,
                'archived_at': row.archived_at.isoformat() if row.archived_at else None,
            },
            prior_state={'mental_model_id': target_id, 'archived_at': None},
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
        async with api.metastore.session() as session:
            result = await session.execute(
                _UNARCHIVE_SQL,
                {
                    'id': UUID(target_id),
                    'vault_id': str(vault_id) if vault_id is not None else None,
                },
            )
            row = result.first()
            await session.commit()
        if row is None:
            raise ProposalActionError(f'mental model {target_id} not archived; nothing to restore.')
        return ReverseResult(restored_state={'mental_model_id': target_id, 'archived_at': None})

    async def preview(
        self,
        api: MemexAPI,
        params: dict[str, Any],
        *,
        target_id: str,
        vault_id: UUID | None,
    ) -> str:
        return (
            'Will set archived_at=now() on this mental model; it stops surfacing '
            'in retrieval, survey, and reflection. Row content is preserved.'
        )


register_action(ArchiveMentalModelAction())
