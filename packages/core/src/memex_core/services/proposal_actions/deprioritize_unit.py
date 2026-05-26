"""`deprioritize_unit` / `restore_unit` — reversible pair on `MemoryUnit.is_deprioritized`.

The deprio path delegates to `MemexAPI.deprioritize_memory_unit`, which flips
the flag, scans `mental_models.observations` for citing observations, and
enqueues one `refresh_observation` task per match. Reverse restores the flag
to False via `restore_memory_unit`. Refresh tasks queued during the apply
are NOT cancelled on reverse — they observe the live unit state when they
run, so a restored unit will surface again in observations naturally.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID

from memex_core.services.proposal_actions.base import (
    ActionValidationError,
    ExecuteResult,
    ReverseResult,
    register_action,
)

if TYPE_CHECKING:
    from memex_core.api import MemexAPI


class DeprioritizeUnitAction:
    id: ClassVar[str] = 'deprioritize_unit'
    name: ClassVar[str] = 'Deprioritize unit'
    description: ClassVar[str] = (
        'Flip is_deprioritized=true; the unit is hidden from retrieval and '
        'citing observations are queued for refresh. Reversible.'
    )
    applicable_target_types: ClassVar[tuple[str, ...]] = ('memory_unit',)
    reversible: ClassVar[bool] = True

    def validate(
        self,
        params: dict[str, Any],
        *,
        target_type: str,
        target_id: str,
    ) -> None:
        if target_type != 'memory_unit':
            raise ActionValidationError(
                f'deprioritize_unit applies to memory_unit targets, not {target_type!r}.'
            )
        try:
            UUID(target_id)
        except (ValueError, AttributeError):
            raise ActionValidationError(f'target_id {target_id!r} is not a valid UUID.')
        override = params.get('override_target_id')
        if override:
            try:
                UUID(override)
            except (ValueError, AttributeError):
                raise ActionValidationError(f'override_target_id {override!r} is not a valid UUID.')
        # `reason` is optional; the proposal's evidence supplies a default when absent.

    async def execute(
        self,
        api: MemexAPI,
        params: dict[str, Any],
        *,
        target_id: str,
        vault_id: UUID,
        actor: str,
    ) -> ExecuteResult:
        reason = str(params.get('reason') or 'cockpit: maintenance proposal accepted')
        override = params.get('override_target_id')
        unit_id = UUID(override) if override else UUID(target_id)
        await api.deprioritize_memory_unit(
            unit_id,
            reason,
            vault_id=vault_id,
            actor=actor,
        )
        return ExecuteResult(
            applied_state={'unit_id': str(unit_id), 'is_deprioritized': True, 'reason': reason},
            prior_state={'unit_id': str(unit_id), 'is_deprioritized': False},
        )

    async def reverse(
        self,
        api: MemexAPI,
        params: dict[str, Any],
        applied_state: dict[str, Any],
        prior_state: dict[str, Any],
        *,
        target_id: str,
        vault_id: UUID,
        actor: str,
    ) -> ReverseResult:
        actual_id = applied_state.get('unit_id') or target_id
        unit_id = UUID(actual_id)
        await api.restore_memory_unit(unit_id, vault_id=vault_id, actor=actor)
        return ReverseResult(restored_state={'unit_id': str(unit_id), 'is_deprioritized': False})

    async def preview(
        self,
        api: MemexAPI,
        params: dict[str, Any],
        *,
        target_id: str,
        vault_id: UUID,
    ) -> str:
        # Counting citing observations costs a JSONB scan; defer the exact
        # count and surface the contract instead. The receipt after execute
        # carries the actual queued-refresh count via applied_state.
        return (
            'Will set is_deprioritized=true on this unit; queue refresh on every '
            'observation citing it.'
        )


class RestoreUnitAction:
    id: ClassVar[str] = 'restore_unit'
    name: ClassVar[str] = 'Restore unit'
    description: ClassVar[str] = (
        'Clear is_deprioritized; the unit becomes retrievable again. Reversible '
        '(inverse of deprioritize_unit).'
    )
    applicable_target_types: ClassVar[tuple[str, ...]] = ('memory_unit',)
    reversible: ClassVar[bool] = True

    def validate(
        self,
        params: dict[str, Any],
        *,
        target_type: str,
        target_id: str,
    ) -> None:
        if target_type != 'memory_unit':
            raise ActionValidationError(
                f'restore_unit applies to memory_unit targets, not {target_type!r}.'
            )

    async def execute(
        self,
        api: MemexAPI,
        params: dict[str, Any],
        *,
        target_id: str,
        vault_id: UUID,
        actor: str,
    ) -> ExecuteResult:
        unit_id = UUID(target_id)
        await api.restore_memory_unit(unit_id, vault_id=vault_id, actor=actor)
        return ExecuteResult(
            applied_state={'unit_id': str(unit_id), 'is_deprioritized': False},
            prior_state={'unit_id': str(unit_id), 'is_deprioritized': True},
        )

    async def reverse(
        self,
        api: MemexAPI,
        params: dict[str, Any],
        applied_state: dict[str, Any],
        prior_state: dict[str, Any],
        *,
        target_id: str,
        vault_id: UUID,
        actor: str,
    ) -> ReverseResult:
        unit_id = UUID(target_id)
        await api.deprioritize_memory_unit(
            unit_id,
            'cockpit: restore_unit reversed',
            vault_id=vault_id,
            actor=actor,
        )
        return ReverseResult(restored_state={'unit_id': str(unit_id), 'is_deprioritized': True})

    async def preview(
        self,
        api: MemexAPI,
        params: dict[str, Any],
        *,
        target_id: str,
        vault_id: UUID,
    ) -> str:
        return 'Will clear is_deprioritized on this unit; observations will re-surface it.'


register_action(DeprioritizeUnitAction())
register_action(RestoreUnitAction())
