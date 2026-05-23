"""Unit tests for the maintenance-proposal action registry.

Each action's execute/reverse pair is exercised with a fake MemexAPI so the
registry's contract (atomicity attributes, validate, preview shape) is
locked down without standing up Postgres.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest

from memex_core.services.proposal_actions import (
    ActionValidationError,
    get_action,
    list_actions,
)


_VAULT = UUID('00000000-0000-0000-0000-000000000001')
_ACTOR = 'test:cockpit'


@dataclass
class _FakeMetastoreSession:
    """Minimal async session that records SQL executions and returns canned rows."""

    rows: list[Any] = field(default_factory=list)
    executed: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    committed: bool = False

    async def execute(self, stmt: Any, params: dict[str, Any]) -> '_FakeMetastoreResult':
        self.executed.append((str(stmt), params))
        return _FakeMetastoreResult(self.rows.pop(0) if self.rows else None)

    async def commit(self) -> None:
        self.committed = True


@dataclass
class _FakeMetastoreResult:
    _row: Any | None

    def first(self) -> Any | None:
        return self._row


class _FakeMetastoreSessionCtx:
    def __init__(self, session: _FakeMetastoreSession) -> None:
        self._session = session

    async def __aenter__(self) -> _FakeMetastoreSession:
        return self._session

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None


@dataclass
class _FakeMetastore:
    sessions: list[_FakeMetastoreSession] = field(default_factory=list)

    def session(self) -> _FakeMetastoreSessionCtx:
        sess = self.sessions[0] if self.sessions else _FakeMetastoreSession()
        if not self.sessions:
            self.sessions.append(sess)
        return _FakeMetastoreSessionCtx(sess)


@dataclass
class _FakeApi:
    """Just enough surface for action.execute() / action.reverse()."""

    metastore: _FakeMetastore = field(default_factory=_FakeMetastore)
    deprioritized: list[tuple[UUID, str, UUID | None, str | None]] = field(default_factory=list)
    restored: list[tuple[UUID, UUID | None, str | None]] = field(default_factory=list)

    async def deprioritize_memory_unit(
        self,
        unit_id: UUID,
        reason: str,
        *,
        vault_id: UUID | None = None,
        actor: str | None = None,
        background_tasks: Any | None = None,
    ) -> None:
        self.deprioritized.append((unit_id, reason, vault_id, actor))

    async def restore_memory_unit(
        self,
        unit_id: UUID,
        *,
        vault_id: UUID | None = None,
        actor: str | None = None,
        background_tasks: Any | None = None,
    ) -> None:
        self.restored.append((unit_id, vault_id, actor))


class TestNoOpAction:
    def test_applies_to_every_target_type(self) -> None:
        action = get_action('no_op')
        assert 'memory_unit' in action.applicable_target_types
        assert 'mental_model' in action.applicable_target_types
        assert 'note' in action.applicable_target_types
        assert action.reversible is True

    @pytest.mark.asyncio
    async def test_execute_records_noop_state(self) -> None:
        action = get_action('no_op')
        api = _FakeApi()
        target_id = str(uuid4())
        result = await action.execute(api, {}, target_id=target_id, vault_id=_VAULT, actor=_ACTOR)
        assert result.applied_state == {'noop': True}
        assert result.prior_state == {}

    @pytest.mark.asyncio
    async def test_reverse_is_identity(self) -> None:
        action = get_action('no_op')
        api = _FakeApi()
        result = await action.reverse(
            api,
            {},
            {'noop': True},
            {},
            target_id=str(uuid4()),
            vault_id=_VAULT,
            actor=_ACTOR,
        )
        assert result.restored_state == {'noop_reversed': True}


class TestDeprioritizeUnitAction:
    def test_applies_to_memory_unit_only(self) -> None:
        action = get_action('deprioritize_unit')
        assert action.applicable_target_types == ('memory_unit',)
        assert action.reversible is True

    def test_validate_rejects_wrong_target_type(self) -> None:
        action = get_action('deprioritize_unit')
        with pytest.raises(ActionValidationError):
            action.validate({}, target_type='mental_model', target_id=str(uuid4()))

    @pytest.mark.asyncio
    async def test_execute_calls_api_deprioritize(self) -> None:
        action = get_action('deprioritize_unit')
        api = _FakeApi()
        target_id = str(uuid4())
        result = await action.execute(
            api,
            {'reason': 'because'},
            target_id=target_id,
            vault_id=_VAULT,
            actor=_ACTOR,
        )
        assert api.deprioritized == [(UUID(target_id), 'because', _VAULT, _ACTOR)]
        assert result.applied_state['is_deprioritized'] is True
        assert result.prior_state['is_deprioritized'] is False

    @pytest.mark.asyncio
    async def test_reverse_calls_api_restore(self) -> None:
        action = get_action('deprioritize_unit')
        api = _FakeApi()
        target_id = str(uuid4())
        await action.reverse(
            api,
            {},
            {'is_deprioritized': True},
            {'is_deprioritized': False},
            target_id=target_id,
            vault_id=_VAULT,
            actor=_ACTOR,
        )
        assert api.restored == [(UUID(target_id), _VAULT, _ACTOR)]

    @pytest.mark.asyncio
    async def test_round_trip_execute_then_reverse(self) -> None:
        action = get_action('deprioritize_unit')
        api = _FakeApi()
        target_id = str(uuid4())
        forward = await action.execute(api, {}, target_id=target_id, vault_id=_VAULT, actor=_ACTOR)
        await action.reverse(
            api,
            {},
            forward.applied_state,
            forward.prior_state,
            target_id=target_id,
            vault_id=_VAULT,
            actor=_ACTOR,
        )
        # Forward + reverse leaves the API with one deprio + one restore on the same id.
        assert len(api.deprioritized) == 1
        assert len(api.restored) == 1
        assert api.deprioritized[0][0] == api.restored[0][0]


class TestRestoreUnitAction:
    def test_validates_target_type(self) -> None:
        action = get_action('restore_unit')
        with pytest.raises(ActionValidationError):
            action.validate({}, target_type='note', target_id=str(uuid4()))

    @pytest.mark.asyncio
    async def test_execute_calls_restore(self) -> None:
        action = get_action('restore_unit')
        api = _FakeApi()
        target_id = str(uuid4())
        result = await action.execute(api, {}, target_id=target_id, vault_id=_VAULT, actor=_ACTOR)
        assert api.restored == [(UUID(target_id), _VAULT, _ACTOR)]
        assert result.applied_state['is_deprioritized'] is False
        assert result.prior_state['is_deprioritized'] is True


class TestArchiveMentalModelAction:
    def test_applies_to_mental_model_only(self) -> None:
        action = get_action('archive_mental_model')
        assert action.applicable_target_types == ('mental_model',)
        assert action.reversible is True

    def test_validate_rejects_wrong_target(self) -> None:
        action = get_action('archive_mental_model')
        with pytest.raises(ActionValidationError):
            action.validate({}, target_type='memory_unit', target_id=str(uuid4()))

    @pytest.mark.asyncio
    async def test_execute_writes_archive_and_records_prior(self) -> None:
        action = get_action('archive_mental_model')
        from datetime import datetime, timezone

        api = _FakeApi()
        api.metastore.sessions.append(
            _FakeMetastoreSession(
                rows=[type('Row', (), {'archived_at': datetime.now(timezone.utc)})()],
            )
        )
        target_id = str(uuid4())
        result = await action.execute(api, {}, target_id=target_id, vault_id=_VAULT, actor=_ACTOR)
        assert result.applied_state['mental_model_id'] == target_id
        assert result.applied_state['archived_at'] is not None
        assert result.prior_state['archived_at'] is None

    @pytest.mark.asyncio
    async def test_execute_raises_when_no_row_updated(self) -> None:
        from memex_core.services.proposal_actions import ProposalActionError

        action = get_action('archive_mental_model')
        api = _FakeApi()
        api.metastore.sessions.append(_FakeMetastoreSession(rows=[None]))
        with pytest.raises(ProposalActionError):
            await action.execute(api, {}, target_id=str(uuid4()), vault_id=_VAULT, actor=_ACTOR)


class TestRegistryShape:
    def test_unknown_action_id_raises(self) -> None:
        with pytest.raises(KeyError):
            get_action('not_a_real_action')

    def test_list_filter_by_target_type(self) -> None:
        unit_actions = {a.id for a in list_actions(target_type='memory_unit')}
        model_actions = {a.id for a in list_actions(target_type='mental_model')}
        assert 'deprioritize_unit' in unit_actions
        assert 'restore_unit' in unit_actions
        assert 'archive_mental_model' not in unit_actions
        assert 'no_op' in unit_actions
        assert 'archive_mental_model' in model_actions
        assert 'deprioritize_unit' not in model_actions

    @pytest.mark.asyncio
    async def test_every_action_has_preview_string(self) -> None:
        api = _FakeApi()
        for action in list_actions():
            text = await action.preview(api, {}, target_id=str(uuid4()), vault_id=_VAULT)
            assert isinstance(text, str)
            assert text.strip()
