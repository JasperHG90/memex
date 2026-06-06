"""`no_op` action — records the verdict; no mutation.

Used when the reviewer accepts a proposal without remediation (e.g.
`llm_schema_drift` flagged as intentional). Trivially reversible — there's
nothing to undo. The action exists so the resolution carries a structured
record instead of a magic empty case.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID

from memex_core.services.proposal_actions.base import (
    ExecuteResult,
    ReverseResult,
    register_action,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from memex_core.api import MemexAPI


class NoOpAction:
    id: ClassVar[str] = 'no_op'
    name: ClassVar[str] = 'No-op (record only)'
    description: ClassVar[str] = (
        'Record the verdict and any reviewer note without touching the target.'
    )
    applicable_target_types: ClassVar[tuple[str, ...]] = (
        'memory_unit',
        'mental_model',
        'note',
        'unit_entity',
        'entity',
        'kv',
    )
    reversible: ClassVar[bool] = True
    params_schema: ClassVar[dict[str, Any] | None] = None

    def validate(
        self,
        params: dict[str, Any],
        *,
        target_type: str,
        target_id: str,
    ) -> None:
        """Accepts any target — no-op records the verdict only."""

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
        return ExecuteResult(applied_state={'noop': True}, prior_state={})

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
        return ReverseResult(restored_state={'noop_reversed': True})

    async def preview(
        self,
        api: MemexAPI,
        params: dict[str, Any],
        *,
        target_id: str,
        vault_id: UUID | None,
    ) -> str:
        return 'No mutation; only the verdict and note are recorded.'


register_action(NoOpAction())
