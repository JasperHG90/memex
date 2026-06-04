"""`record_outcome` — append a Memory-Worth outcome to the targeted unit.

Lets a reviewer resolve a finding by crediting (``helpful``) or debiting
(``not_helpful``) the unit's outcome ledger instead of flipping a lifecycle
flag — e.g. a `cold_low_mw_unit` finding where the human knows the unit was
recently misleading. Forward-only by design: outcome counters feed the
Beta-Bernoulli Memory-Worth posterior, and the ledger is append-only —
decrementing it to "undo" would corrupt the posterior's evidence count.
``reason`` is required for the credit-bearing verbs (same contract as the
outcome service).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Literal
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
    from memex_core.api import MemexAPI


class _RecordOutcomeParams(BaseModel):
    verb: Literal['helpful', 'not_helpful', 'not_used'] = Field(
        description='Outcome classification appended to the unit ledger.'
    )
    reason: str | None = Field(
        default=None,
        description='Audit justification; REQUIRED for helpful / not_helpful.',
    )


class RecordOutcomeAction:
    id: ClassVar[str] = 'record_outcome'
    name: ClassVar[str] = 'Record outcome on unit'
    description: ClassVar[str] = (
        "Append a helpful / not_helpful / not_used outcome to the unit's "
        'Memory-Worth ledger. NOT reversible — the ledger is append-only.'
    )
    applicable_target_types: ClassVar[tuple[str, ...]] = ('memory_unit',)
    reversible: ClassVar[bool] = False
    params_schema: ClassVar[dict[str, Any] | None] = _RecordOutcomeParams.model_json_schema()

    def validate(
        self,
        params: dict[str, Any],
        *,
        target_type: str,
        target_id: str,
    ) -> None:
        if target_type != 'memory_unit':
            raise ActionValidationError(
                f'record_outcome applies to memory_unit targets, not {target_type!r}.'
            )
        try:
            UUID(target_id)
        except (ValueError, AttributeError):
            raise ActionValidationError(f'target_id {target_id!r} is not a valid UUID.')
        try:
            parsed = _RecordOutcomeParams(**params)
        except ValidationError as exc:
            raise ActionValidationError(f'invalid record_outcome params: {exc}') from exc
        if parsed.verb in ('helpful', 'not_helpful') and not (
            parsed.reason and parsed.reason.strip()
        ):
            raise ActionValidationError(
                f'record_outcome with verb={parsed.verb!r} requires a non-empty reason.'
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
        parsed = _RecordOutcomeParams(**params)
        result = await api.record_outcome(
            vault_id=str(vault_id) if vault_id is not None else None,
            units=[{'unit_id': target_id, 'verb': parsed.verb, 'reason': parsed.reason}],
            caller_id=actor,
        )
        return ExecuteResult(
            applied_state={
                'unit_id': target_id,
                'verb': parsed.verb,
                'verb_counts': result.get('verb_counts'),
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
            'record_outcome is forward-only: the outcome ledger is append-only.'
        )

    async def preview(
        self,
        api: MemexAPI,
        params: dict[str, Any],
        *,
        target_id: str,
        vault_id: UUID | None,
    ) -> str:
        verb = params.get('verb', '<unspecified>')
        return (
            f'Will append a {verb!r} outcome to this unit (Memory-Worth posterior '
            'shifts accordingly). NOT reversible.'
        )


register_action(RecordOutcomeAction())
