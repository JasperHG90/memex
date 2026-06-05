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
class _FakeEntities:
    """Recorded entity-merge surface for the merge actions."""

    collapsed: list[tuple[UUID, list[UUID], str | None]] = field(default_factory=list)
    collapsed_new: list[tuple[list[UUID], str, str | None]] = field(default_factory=list)
    collapse_error: Exception | None = None

    async def collapse_cluster(
        self,
        *,
        winner_id: UUID,
        loser_ids: list[UUID],
        actor: str | None = None,
    ) -> dict[str, Any]:
        if self.collapse_error is not None:
            raise self.collapse_error
        self.collapsed.append((winner_id, list(loser_ids), actor))
        return {'winner_id': str(winner_id), 'losers': len(loser_ids)}

    async def collapse_into_new_entity(
        self,
        *,
        member_ids: list[UUID],
        new_canonical_name: str,
        actor: str | None = None,
    ) -> dict[str, Any]:
        if self.collapse_error is not None:
            raise self.collapse_error
        self.collapsed_new.append((list(member_ids), new_canonical_name, actor))
        return {
            'created_entity_id': str(uuid4()),
            'created_canonical_name': new_canonical_name,
        }


@dataclass
class _FakeApi:
    """Just enough surface for action.execute() / action.reverse()."""

    metastore: _FakeMetastore = field(default_factory=_FakeMetastore)
    entities: _FakeEntities = field(default_factory=_FakeEntities)
    deprioritized: list[tuple[UUID, str, UUID | None, str | None]] = field(default_factory=list)
    restored: list[tuple[UUID, UUID | None, str | None]] = field(default_factory=list)
    note_status_calls: list[tuple[UUID, str, UUID | None]] = field(default_factory=list)
    title_calls: list[tuple[UUID, str]] = field(default_factory=list)
    date_calls: list[tuple[UUID, Any]] = field(default_factory=list)
    deleted_notes: list[UUID] = field(default_factory=list)
    deleted_entities: list[UUID] = field(default_factory=list)
    deleted_models: list[tuple[UUID, UUID]] = field(default_factory=list)
    kv_deleted: list[str] = field(default_factory=list)
    kv_delete_result: bool = True
    outcomes: list[dict[str, Any]] = field(default_factory=list)

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

    async def set_note_status(
        self,
        note_id: UUID,
        status: str,
        linked_note_id: UUID | None = None,
    ) -> dict[str, Any]:
        self.note_status_calls.append((note_id, status, linked_note_id))
        return {'note_id': str(note_id), 'status': status}

    async def update_note_title(self, note_id: UUID, new_title: str) -> dict[str, Any]:
        self.title_calls.append((note_id, new_title))
        return {'note_id': str(note_id), 'title': new_title}

    async def update_note_date(self, note_id: UUID, new_date: Any) -> dict[str, Any]:
        self.date_calls.append((note_id, new_date))
        return {'note_id': str(note_id)}

    async def delete_note(self, note_id: UUID) -> bool:
        self.deleted_notes.append(note_id)
        return True

    async def delete_entity(self, entity_id: UUID) -> bool:
        self.deleted_entities.append(entity_id)
        return True

    async def delete_mental_model(self, entity_id: UUID, vault_id: UUID) -> bool:
        self.deleted_models.append((entity_id, vault_id))
        return True

    async def kv_delete(self, key: str) -> bool:
        self.kv_deleted.append(key)
        return self.kv_delete_result

    async def record_outcome(self, **kwargs: Any) -> dict[str, Any]:
        self.outcomes.append(kwargs)
        return {'verb_counts': {'helpful': 1, 'not_helpful': 0, 'not_used': 0}}


def _row(**attrs: Any) -> Any:
    return type('Row', (), attrs)()


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


_FORWARD_ONLY = (
    'merge_entities',
    'collapse_into_new_entity',
    'kv_delete',
    'record_outcome',
    'delete_note',
    'delete_entity',
    'delete_mental_model',
)

_PARAMETERIZED = (
    'deprioritize_unit',
    'route_note_to_vault',
    'set_note_status',
    'update_note_title',
    'update_note_date',
    'merge_entities',
    'collapse_into_new_entity',
    'kv_delete',
    'record_outcome',
)


class TestParamsSchemaDiscoverability:
    def test_every_action_declares_the_attribute(self) -> None:
        for action in list_actions():
            assert hasattr(action, 'params_schema'), action.id

    def test_parameterized_actions_publish_a_schema(self) -> None:
        for action_id in _PARAMETERIZED:
            schema = get_action(action_id).params_schema
            assert isinstance(schema, dict) and 'properties' in schema, action_id

    def test_parameterless_actions_publish_none(self) -> None:
        for action_id in ('no_op', 'archive_mental_model', 'restore_unit', 'delete_note'):
            assert get_action(action_id).params_schema is None, action_id


class TestForwardOnlyFences:
    @pytest.mark.asyncio
    async def test_reverse_refuses_for_every_forward_only_action(self) -> None:
        from memex_core.services.proposal_actions import ProposalActionError

        api = _FakeApi()
        for action_id in _FORWARD_ONLY:
            action = get_action(action_id)
            assert action.reversible is False, action_id
            with pytest.raises(ProposalActionError):
                await action.reverse(
                    api, {}, {}, {}, target_id=str(uuid4()), vault_id=_VAULT, actor=_ACTOR
                )


class TestSetNoteStatusAction:
    def test_validate_rejects_bad_status(self) -> None:
        action = get_action('set_note_status')
        with pytest.raises(ActionValidationError):
            action.validate({'status': 'appended'}, target_type='note', target_id=str(uuid4()))

    def test_validate_rejects_bad_linked_uuid(self) -> None:
        action = get_action('set_note_status')
        with pytest.raises(ActionValidationError):
            action.validate(
                {'status': 'superseded', 'linked_note_id': 'nope'},
                target_type='note',
                target_id=str(uuid4()),
            )

    @pytest.mark.asyncio
    async def test_execute_snapshots_prior_lifecycle(self) -> None:
        action = get_action('set_note_status')
        api = _FakeApi()
        api.metastore.sessions.append(
            _FakeMetastoreSession(
                rows=[_row(status='active', superseded_by=None, archived_at=None, appended_to=None)]
            )
        )
        target_id = str(uuid4())
        result = await action.execute(
            api, {'status': 'archived'}, target_id=target_id, vault_id=_VAULT, actor=_ACTOR
        )
        assert api.note_status_calls == [(UUID(target_id), 'archived', None)]
        assert result.prior_state['status'] == 'active'
        assert result.prior_state['archived_at'] is None

    @pytest.mark.asyncio
    async def test_reverse_restores_superseded_then_archived(self) -> None:
        action = get_action('set_note_status')
        api = _FakeApi()
        target_id = str(uuid4())
        winner = str(uuid4())
        result = await action.reverse(
            api,
            {},
            {'note_id': target_id, 'status': 'active'},
            {
                'note_id': target_id,
                'status': 'superseded',
                'superseded_by': winner,
                'archived_at': '2026-01-01T00:00:00+00:00',
                'appended_to': None,
            },
            target_id=target_id,
            vault_id=_VAULT,
            actor=_ACTOR,
        )
        assert api.note_status_calls == [
            (UUID(target_id), 'superseded', UUID(winner)),
            (UUID(target_id), 'archived', None),
        ]
        # The supersede pointer survives the re-archive (archived coexists with
        # superseded) and is echoed in restored_state for the audit trail.
        assert result.restored_state['superseded_by'] == winner
        assert result.restored_state['status'] == 'superseded'
        assert result.restored_state['archived'] is True

    @pytest.mark.asyncio
    async def test_reverse_refuses_unknown_prior_status(self) -> None:
        """An unrecognised prior status is refused, not silently restored as
        'active' — a future lifecycle state cannot mis-restore here."""
        from memex_core.services.proposal_actions import ProposalActionError

        action = get_action('set_note_status')
        api = _FakeApi()
        with pytest.raises(ProposalActionError, match='unhandled prior status'):
            await action.reverse(
                api,
                {},
                {},
                {'note_id': str(uuid4()), 'status': 'some_future_state'},
                target_id=str(uuid4()),
                vault_id=_VAULT,
                actor=_ACTOR,
            )
        assert api.note_status_calls == []

    @pytest.mark.asyncio
    async def test_reverse_refuses_appended_to(self) -> None:
        from memex_core.services.proposal_actions import ProposalActionError

        action = get_action('set_note_status')
        api = _FakeApi()
        with pytest.raises(ProposalActionError):
            await action.reverse(
                api,
                {},
                {},
                {'status': 'active', 'appended_to': str(uuid4())},
                target_id=str(uuid4()),
                vault_id=_VAULT,
                actor=_ACTOR,
            )


class TestNoteMetadataActions:
    @pytest.mark.asyncio
    async def test_title_round_trip_restores_prior(self) -> None:
        action = get_action('update_note_title')
        api = _FakeApi()
        api.metastore.sessions.append(_FakeMetastoreSession(rows=[_row(title='Old name')]))
        target_id = str(uuid4())
        forward = await action.execute(
            api, {'new_title': 'New name'}, target_id=target_id, vault_id=_VAULT, actor=_ACTOR
        )
        assert forward.prior_state['title'] == 'Old name'
        await action.reverse(
            api,
            {},
            forward.applied_state,
            forward.prior_state,
            target_id=target_id,
            vault_id=_VAULT,
            actor=_ACTOR,
        )
        assert api.title_calls == [(UUID(target_id), 'New name'), (UUID(target_id), 'Old name')]

    @pytest.mark.asyncio
    async def test_title_reverse_refuses_empty_prior(self) -> None:
        from memex_core.services.proposal_actions import ProposalActionError

        action = get_action('update_note_title')
        with pytest.raises(ProposalActionError):
            await action.reverse(
                api=_FakeApi(),
                params={},
                applied_state={},
                prior_state={'title': None},
                target_id=str(uuid4()),
                vault_id=_VAULT,
                actor=_ACTOR,
            )

    def test_date_validate_rejects_non_iso(self) -> None:
        action = get_action('update_note_date')
        with pytest.raises(ActionValidationError):
            action.validate(
                {'new_date': 'next tuesday'}, target_type='note', target_id=str(uuid4())
            )

    @pytest.mark.asyncio
    async def test_date_execute_snapshots_prior(self) -> None:
        from datetime import datetime, timezone

        action = get_action('update_note_date')
        api = _FakeApi()
        prior = datetime(2025, 3, 1, tzinfo=timezone.utc)
        api.metastore.sessions.append(_FakeMetastoreSession(rows=[_row(publish_date=prior)]))
        result = await action.execute(
            api, {'new_date': '2026-01-15'}, target_id=str(uuid4()), vault_id=_VAULT, actor=_ACTOR
        )
        assert result.prior_state['publish_date'] == prior.isoformat()
        assert len(api.date_calls) == 1


class TestMergeEntitiesAction:
    def test_validate_requires_two_members(self) -> None:
        action = get_action('merge_entities')
        winner = str(uuid4())
        with pytest.raises(ActionValidationError):
            action.validate(
                {'winner_id': winner, 'member_ids': [winner]},
                target_type='entity',
                target_id=winner,
            )

    def test_validate_requires_winner_in_members(self) -> None:
        action = get_action('merge_entities')
        with pytest.raises(ActionValidationError):
            action.validate(
                {'winner_id': str(uuid4()), 'member_ids': [str(uuid4()), str(uuid4())]},
                target_type='entity',
                target_id=str(uuid4()),
            )

    @pytest.mark.asyncio
    async def test_execute_collapses_losers_onto_winner(self) -> None:
        action = get_action('merge_entities')
        api = _FakeApi()
        winner, loser_a, loser_b = uuid4(), uuid4(), uuid4()
        result = await action.execute(
            api,
            {'winner_id': str(winner), 'member_ids': [str(winner), str(loser_a), str(loser_b)]},
            target_id=str(winner),
            vault_id=_VAULT,
            actor=_ACTOR,
        )
        assert api.entities.collapsed == [(winner, [loser_a, loser_b], _ACTOR)]
        assert result.applied_state['winner_id'] == str(winner)
        assert result.prior_state == {}

    @pytest.mark.asyncio
    async def test_nonexistent_member_raises_action_error_not_500(self) -> None:
        from memex_common.exceptions import EntityNotFoundError
        from memex_core.services.proposal_actions import ProposalActionError

        action = get_action('merge_entities')
        api = _FakeApi()
        api.entities.collapse_error = EntityNotFoundError('Entities not found: [...]')
        winner, loser = uuid4(), uuid4()
        with pytest.raises(ProposalActionError):
            await action.execute(
                api,
                {'winner_id': str(winner), 'member_ids': [str(winner), str(loser)]},
                target_id=str(winner),
                vault_id=_VAULT,
                actor=_ACTOR,
            )


class TestCollapseIntoNewEntityAction:
    def test_validate_requires_name(self) -> None:
        action = get_action('collapse_into_new_entity')
        with pytest.raises(ActionValidationError):
            action.validate(
                {'new_canonical_name': '   ', 'member_ids': [str(uuid4()), str(uuid4())]},
                target_type='entity',
                target_id=str(uuid4()),
            )

    @pytest.mark.asyncio
    async def test_execute_creates_then_collapses(self) -> None:
        action = get_action('collapse_into_new_entity')
        api = _FakeApi()
        members = [uuid4(), uuid4(), uuid4()]
        result = await action.execute(
            api,
            {'new_canonical_name': 'Unified Entity', 'member_ids': [str(m) for m in members]},
            target_id=str(members[0]),
            vault_id=_VAULT,
            actor=_ACTOR,
        )
        assert api.entities.collapsed_new == [(members, 'Unified Entity', _ACTOR)]
        assert result.applied_state['created_canonical_name'] == 'Unified Entity'

    @pytest.mark.asyncio
    async def test_service_value_error_becomes_action_error(self) -> None:
        from memex_core.services.proposal_actions import ProposalActionError

        action = get_action('collapse_into_new_entity')
        api = _FakeApi()
        api.entities.collapse_error = ValueError('an entity named X already exists')
        with pytest.raises(ProposalActionError):
            await action.execute(
                api,
                {
                    'new_canonical_name': 'X',
                    'member_ids': [str(uuid4()), str(uuid4())],
                },
                target_id=str(uuid4()),
                vault_id=_VAULT,
                actor=_ACTOR,
            )


class TestKvDeleteAction:
    def test_key_defaults_to_target(self) -> None:
        action = get_action('kv_delete')
        action.validate({}, target_type='kv', target_id='user:editor')

    def test_missing_key_everywhere_rejected(self) -> None:
        action = get_action('kv_delete')
        with pytest.raises(ActionValidationError):
            action.validate({}, target_type='kv', target_id='')

    @pytest.mark.asyncio
    async def test_execute_deletes_and_missing_key_raises(self) -> None:
        from memex_core.services.proposal_actions import ProposalActionError

        action = get_action('kv_delete')
        api = _FakeApi()
        result = await action.execute(
            api, {}, target_id='user:editor', vault_id=_VAULT, actor=_ACTOR
        )
        assert api.kv_deleted == ['user:editor']
        assert result.applied_state['deleted'] is True
        api.kv_delete_result = False
        with pytest.raises(ProposalActionError):
            await action.execute(api, {}, target_id='user:gone', vault_id=_VAULT, actor=_ACTOR)


class TestRecordOutcomeAction:
    def test_credit_bearing_verbs_require_reason(self) -> None:
        action = get_action('record_outcome')
        with pytest.raises(ActionValidationError):
            action.validate(
                {'verb': 'not_helpful'}, target_type='memory_unit', target_id=str(uuid4())
            )
        action.validate({'verb': 'not_used'}, target_type='memory_unit', target_id=str(uuid4()))

    @pytest.mark.asyncio
    async def test_execute_appends_unit_outcome(self) -> None:
        action = get_action('record_outcome')
        api = _FakeApi()
        target_id = str(uuid4())
        await action.execute(
            api,
            {'verb': 'helpful', 'reason': 'held for 30 days'},
            target_id=target_id,
            vault_id=_VAULT,
            actor=_ACTOR,
        )
        assert api.outcomes[0]['units'] == [
            {'unit_id': target_id, 'verb': 'helpful', 'reason': 'held for 30 days'}
        ]


class TestDeleteActions:
    @pytest.mark.asyncio
    async def test_delete_note_snapshots_blast_radius(self) -> None:
        action = get_action('delete_note')
        api = _FakeApi()
        api.metastore.sessions.append(
            _FakeMetastoreSession(rows=[_row(title='Old note', unit_count=4, chunk_count=2)])
        )
        target_id = str(uuid4())
        result = await action.execute(api, {}, target_id=target_id, vault_id=_VAULT, actor=_ACTOR)
        assert api.deleted_notes == [UUID(target_id)]
        assert result.applied_state['units_deleted'] == 4
        assert result.applied_state['chunks_deleted'] == 2

    @pytest.mark.asyncio
    async def test_delete_note_missing_target_refuses(self) -> None:
        from memex_core.services.proposal_actions import ProposalActionError

        action = get_action('delete_note')
        api = _FakeApi()
        api.metastore.sessions.append(_FakeMetastoreSession(rows=[None]))
        with pytest.raises(ProposalActionError):
            await action.execute(api, {}, target_id=str(uuid4()), vault_id=_VAULT, actor=_ACTOR)
        assert api.deleted_notes == []

    @pytest.mark.asyncio
    async def test_delete_entity_snapshots_blast_radius(self) -> None:
        action = get_action('delete_entity')
        api = _FakeApi()
        api.metastore.sessions.append(
            _FakeMetastoreSession(
                rows=[_row(canonical_name='Dup Entity', mention_count=7, model_count=2)]
            )
        )
        target_id = str(uuid4())
        result = await action.execute(api, {}, target_id=target_id, vault_id=_VAULT, actor=_ACTOR)
        assert api.deleted_entities == [UUID(target_id)]
        assert result.applied_state['mention_count'] == 7

    @pytest.mark.asyncio
    async def test_delete_mental_model_refuses_null_vault(self) -> None:
        from memex_core.services.proposal_actions import ProposalActionError

        action = get_action('delete_mental_model')
        api = _FakeApi()
        with pytest.raises(ProposalActionError, match='vault-scoped'):
            await action.execute(api, {}, target_id=str(uuid4()), vault_id=None, actor=_ACTOR)
        assert api.deleted_models == []

    @pytest.mark.asyncio
    async def test_delete_mental_model_scopes_to_vault(self) -> None:
        action = get_action('delete_mental_model')
        api = _FakeApi()
        api.metastore.sessions.append(_FakeMetastoreSession(rows=[_row(observation_count=5)]))
        target_id = str(uuid4())
        result = await action.execute(api, {}, target_id=target_id, vault_id=_VAULT, actor=_ACTOR)
        assert api.deleted_models == [(UUID(target_id), _VAULT)]
        assert result.applied_state['observations_deleted'] == 5

    @pytest.mark.asyncio
    async def test_delete_previews_warn_not_reversible(self) -> None:
        api = _FakeApi()
        for action_id, row in (
            ('delete_note', _row(title='N', unit_count=1, chunk_count=1)),
            ('delete_entity', _row(canonical_name='E', mention_count=1, model_count=0)),
            ('delete_mental_model', _row(observation_count=3)),
        ):
            api.metastore.sessions = [_FakeMetastoreSession(rows=[row])]
            text = await get_action(action_id).preview(
                api, {}, target_id=str(uuid4()), vault_id=_VAULT
            )
            assert 'NOT reversible' in text, action_id
