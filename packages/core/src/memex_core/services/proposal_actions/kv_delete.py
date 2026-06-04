"""`kv_delete` — forward-only removal of a KV entry.

For `kv`-targeted findings the finding's ``target_id`` IS the KV key (a
namespaced string, not a UUID); ``params.key`` overrides it when supplied.
Forward-only: procedure-key history, TTL state, and the value's embedding
cannot be faithfully reconstructed by a re-put, so there is no reverse.
The deleted value itself is deliberately NOT captured into the resolution
payload — KV rows can hold sensitive preferences and `evidence` is visible
to the agent surface.
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
    from memex_core.api import MemexAPI


class _KvDeleteParams(BaseModel):
    key: str | None = Field(
        default=None,
        min_length=1,
        description="KV key to delete; defaults to the finding's target_id.",
    )


class KvDeleteAction:
    id: ClassVar[str] = 'kv_delete'
    name: ClassVar[str] = 'Delete KV entry'
    description: ClassVar[str] = (
        'Hard-delete the KV entry (key from params.key or the finding target). '
        'NOT reversible — history, TTL, and embedding state are lost.'
    )
    applicable_target_types: ClassVar[tuple[str, ...]] = ('kv',)
    reversible: ClassVar[bool] = False
    params_schema: ClassVar[dict[str, Any] | None] = _KvDeleteParams.model_json_schema()

    def _resolve_key(self, params: dict[str, Any], target_id: str) -> str:
        key = str(params.get('key') or target_id or '').strip()
        return key

    def validate(
        self,
        params: dict[str, Any],
        *,
        target_type: str,
        target_id: str,
    ) -> None:
        if target_type != 'kv':
            raise ActionValidationError(f'kv_delete applies to kv targets, not {target_type!r}.')
        try:
            _KvDeleteParams(**params)
        except ValidationError as exc:
            raise ActionValidationError(f'invalid kv_delete params: {exc}') from exc
        if not self._resolve_key(params, target_id):
            raise ActionValidationError(
                'kv_delete requires a key (params.key or a non-empty target_id).'
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
        key = self._resolve_key(params, target_id)
        deleted = await api.kv_delete(key)
        if not deleted:
            raise ProposalActionError(f'KV key {key!r} not found; nothing deleted.')
        return ExecuteResult(applied_state={'key': key, 'deleted': True}, prior_state={})

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
            'kv_delete is forward-only: history, TTL, and embedding state are not restorable.'
        )

    async def preview(
        self,
        api: MemexAPI,
        params: dict[str, Any],
        *,
        target_id: str,
        vault_id: UUID | None,
    ) -> str:
        key = self._resolve_key(params, target_id) or '<unspecified>'
        return f'Will hard-delete KV key {key!r} including any procedure history. NOT reversible.'


register_action(KvDeleteAction())
