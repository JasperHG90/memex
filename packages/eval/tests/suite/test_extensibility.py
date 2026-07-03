"""Tests for the public extension surface.

Confirms that custom outcomes, custom setup actions, and entry-point
plugin suites all work without editing the framework.
"""

from __future__ import annotations

from typing import Any, Literal
from unittest.mock import patch
from uuid import uuid4

import pytest

from memex_eval.suite import (
    AgentAnswer,
    ExpectedOutcomeBase,
    KeywordsPresent,
    Scenario,
    SetupAction,
    SetupActionHandler,
    Suite,
    SuiteMetadata,
    SuiteSources,
    get_outcome_class,
    get_setup_action,
    list_outcomes,
    list_setup_actions,
    register_outcome,
    register_setup_action,
)


class TestOutcomeRegistry:
    def test_built_in_outcomes_registered(self) -> None:
        names = list_outcomes()
        for expected in (
            'keywords_present',
            'keywords_absent',
            'gold_unit_ids',
            'entity_resolves',
            'composite',
        ):
            assert expected in names, f'{expected!r} should be registered'

    def test_register_custom_outcome_and_resolve(self) -> None:
        @register_outcome('custom_x')
        class CustomX(ExpectedOutcomeBase):
            type: Literal['custom_x']
            target: str

            def score(self, answer, scenario, **_kw) -> dict[str, float]:
                hit = self.target.lower() in (answer.answer_text or '').lower()
                return {'pass': 1.0 if hit else 0.0}

            def metric_keys(self, top_k: int | None = None) -> list[str]:
                return ['pass']

        assert get_outcome_class('custom_x') is CustomX
        assert 'custom_x' in list_outcomes()

    def test_custom_outcome_works_inside_scenario(self) -> None:
        @register_outcome('answer_contains_x')
        class AnswerContainsX(ExpectedOutcomeBase):
            type: Literal['answer_contains_x']
            needle: str

            def score(self, answer, scenario, **_kw) -> dict[str, float]:
                return {
                    'pass': 1.0
                    if self.needle.lower() in (answer.answer_text or '').lower()
                    else 0.0
                }

            def metric_keys(self, top_k: int | None = None) -> list[str]:
                return ['pass']

        sc = Scenario(
            id='ext_check',
            description='d',
            query='q',
            expected=AnswerContainsX(type='answer_contains_x', needle='hello'),
        )
        # And via dict-coerce path (e.g. JSON-loaded suite)
        sc2 = Scenario(
            id='ext_check_dict',
            description='d',
            query='q',
            expected={'type': 'answer_contains_x', 'needle': 'hello'},
        )
        assert isinstance(sc.expected, AnswerContainsX)
        assert isinstance(sc2.expected, AnswerContainsX)

        ans = AgentAnswer(answer_text='hello world')
        assert sc.expected.score(ans, sc) == {'pass': 1.0}

    def test_unknown_outcome_type_in_dict_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown outcome type 'no_such_type'"):
            Scenario(
                id='bad',
                description='d',
                query='q',
                expected={'type': 'no_such_type'},
            )

    def test_round_trip_preserves_subclass_fields(self) -> None:
        """Suite → JSON → Suite must preserve every outcome's fields.

        Without ``SerializeAsAny`` on the union, Pydantic dumps only the
        base class fields and silently drops subclass-specific data
        (e.g. ``keywords``). Both JSON and Python-mode dumps are checked.
        """
        sc = Scenario(
            id='roundtrip',
            description='d',
            query='q',
            expected=KeywordsPresent(type='keywords_present', keywords=['hello', 'world']),
            top_k=7,
        )
        suite = Suite(
            metadata=SuiteMetadata(
                name='roundtrip_suite',
                schema_version='1',
                suite_version='1.0.0',
                description='d',
            ),
            sources=SuiteSources(notes=[]),
            scenarios=[sc],
        )
        # JSON round-trip
        rebuilt = Suite.model_validate_json(suite.model_dump_json())
        assert isinstance(rebuilt.scenarios[0].expected, KeywordsPresent)
        assert rebuilt.scenarios[0].expected.keywords == ['hello', 'world']
        assert rebuilt.scenarios[0].top_k == 7
        # Python-mode dict round-trip — different code path in Pydantic v2.
        rebuilt2 = Suite.model_validate(suite.model_dump())
        assert isinstance(rebuilt2.scenarios[0].expected, KeywordsPresent)
        assert rebuilt2.scenarios[0].expected.keywords == ['hello', 'world']

    def test_register_outcome_rejects_collision(self) -> None:
        @register_outcome('collision_test_one')
        class _First(ExpectedOutcomeBase):
            type: Literal['collision_test_one']

            def score(self, answer, scenario, **_kw):
                return {'pass': 0.0}

            def metric_keys(self, top_k=None):
                return ['pass']

        with pytest.raises(ValueError, match='already registered'):

            @register_outcome('collision_test_one')
            class _Second(ExpectedOutcomeBase):
                type: Literal['collision_test_one']

                def score(self, answer, scenario, **_kw):
                    return {'pass': 1.0}

                def metric_keys(self, top_k=None):
                    return ['pass']

    def test_replace_outcome_allows_explicit_override(self) -> None:
        from memex_eval.suite import replace_outcome

        @register_outcome('explicit_replace_test')
        class _First(ExpectedOutcomeBase):
            type: Literal['explicit_replace_test']

            def score(self, answer, scenario, **_kw):
                return {'pass': 0.0}

            def metric_keys(self, top_k=None):
                return ['pass']

        @replace_outcome('explicit_replace_test')
        class _Second(ExpectedOutcomeBase):
            type: Literal['explicit_replace_test']

            def score(self, answer, scenario, **_kw):
                return {'pass': 1.0}

            def metric_keys(self, top_k=None):
                return ['pass']

        assert get_outcome_class('explicit_replace_test') is _Second


class TestSetupActionRegistry:
    def test_built_in_actions_registered(self) -> None:
        names = list_setup_actions()
        for expected in (
            'record_outcome',
            'deprioritize',
            'kv_write',
            'consolidation_tick',
            'trigger_reflections',
            'lint_run',
            'lint_llm_run',
        ):
            assert expected in names

    @pytest.mark.asyncio
    async def test_register_and_dispatch_custom_action(self) -> None:
        captured: dict[str, Any] = {}

        @register_setup_action('snapshot_baseline')
        class _Snapshot(SetupActionHandler):
            async def run(self, api, vault_id, params):
                captured['vault_id'] = vault_id
                captured['params'] = params
                return {'baseline.value': 0.42}

        handler = get_setup_action('snapshot_baseline')
        result = await handler.run(api=None, vault_id=uuid4(), params={'foo': 'bar'})
        assert result == {'baseline.value': 0.42}
        assert captured['params'] == {'foo': 'bar'}

    @pytest.mark.asyncio
    async def test_setup_actions_publish_context_to_score(self) -> None:
        """End-to-end: handler returns dict → runner threads it into score().

        Auto-prefix: a bare ``baseline.memory_worth`` key from the
        ``publish_baseline`` handler lands as
        ``publish_baseline.baseline.memory_worth`` in context.
        """
        from memex_eval.suite.runner import _run_setup_actions

        @register_setup_action('publish_baseline')
        class _Pub(SetupActionHandler):
            async def run(self, api, vault_id, params):
                return {'baseline.memory_worth': 0.7}

        actions = [SetupAction(kind='publish_baseline')]
        ctx = await _run_setup_actions(api=None, vault_id=uuid4(), actions=actions)
        assert ctx['publish_baseline.baseline.memory_worth'] == 0.7
        assert ctx['_setup_failures'] == []

    @pytest.mark.asyncio
    async def test_unknown_action_kind_logged_not_raised(self) -> None:
        """A typo in a setup action's kind logs a warning + records in failures.

        Order-independent: asserts the offending kind plus the eight built-in
        action names appear in the error. Tests that register temporary actions
        may leak into a bulk-run snapshot; checking subset membership instead
        of exact list keeps this test stable across test orderings.
        """
        from memex_eval.suite.runner import _run_setup_actions

        actions = [SetupAction(kind='nonexistent_action_kind_xyz')]
        ctx = await _run_setup_actions(api=None, vault_id=uuid4(), actions=actions)
        assert len(ctx['_setup_failures']) == 1
        failure = ctx['_setup_failures'][0]
        assert failure['kind'] == 'nonexistent_action_kind_xyz'
        error_text = failure['error']
        assert "Unknown setup action 'nonexistent_action_kind_xyz'" in error_text
        for built_in in (
            'clear_hermes_session_notes',
            'consolidation_tick',
            'deprioritize',
            'kv_write',
            'lint_llm_run',
            'lint_run',
            'record_outcome',
            'trigger_reflections',
        ):
            assert built_in in error_text, f'built-in {built_in!r} missing from error'

    @pytest.mark.asyncio
    async def test_returned_context_keys_auto_prefixed_by_handler_name(self) -> None:
        """Handler returns a bare key; runner prefixes it with handler.name."""
        from memex_eval.suite.runner import _run_setup_actions

        @register_setup_action('cap_baseline')
        class _CapBaseline(SetupActionHandler):
            async def run(self, api, vault_id, params):
                # Bare key — runner auto-prefixes with handler.name.
                return {'value': 0.42}

        ctx = await _run_setup_actions(
            api=None, vault_id=uuid4(), actions=[SetupAction(kind='cap_baseline')]
        )
        assert ctx['cap_baseline.value'] == 0.42

    @pytest.mark.asyncio
    async def test_required_setup_action_failure_marks_context(self) -> None:
        """A required handler that raises sets _required_setup_failed."""
        from memex_eval.suite.runner import _run_setup_actions

        @register_setup_action('required_handler')
        class _Required(SetupActionHandler):
            required = True

            async def run(self, api, vault_id, params):
                raise RuntimeError('intentional')

        ctx = await _run_setup_actions(
            api=None, vault_id=uuid4(), actions=[SetupAction(kind='required_handler')]
        )
        assert ctx['_required_setup_failed'] is True
        assert ctx['_setup_failures'][0]['kind'] == 'required_handler'

    @pytest.mark.asyncio
    async def test_required_setup_failure_aborts_remaining_actions(self) -> None:
        """When a required action raises, subsequent actions must NOT run.

        Otherwise a non-required side-effect after the required snapshot
        would leak vault state into the rest of the suite.
        """
        from memex_eval.suite.runner import _run_setup_actions

        executed: list[str] = []

        @register_setup_action('aborting_required')
        class _Aborter(SetupActionHandler):
            required = True

            async def run(self, api, vault_id, params):
                executed.append('aborting_required')
                raise RuntimeError('boom')

        @register_setup_action('would_have_run')
        class _Tail(SetupActionHandler):
            async def run(self, api, vault_id, params):
                executed.append('would_have_run')
                return None

        actions = [
            SetupAction(kind='aborting_required'),
            SetupAction(kind='would_have_run'),
        ]
        ctx = await _run_setup_actions(api=None, vault_id=uuid4(), actions=actions)
        assert ctx['_required_setup_failed'] is True
        assert executed == ['aborting_required']

    @pytest.mark.asyncio
    async def test_runner_reserved_keys_stripped_from_params(self) -> None:
        """The runner pops 'kind' and 'required' before handing off to the handler."""
        from memex_eval.suite.runner import _run_setup_actions

        seen_params: dict[str, Any] = {}

        @register_setup_action('echo_params')
        class _Echo(SetupActionHandler):
            async def run(self, api, vault_id, params):
                seen_params.update(params)
                return None

        action = SetupAction.model_validate(
            {'kind': 'echo_params', 'required': True, 'custom_arg': 42}
        )
        await _run_setup_actions(api=None, vault_id=uuid4(), actions=[action])
        assert 'kind' not in seen_params
        assert 'required' not in seen_params
        assert seen_params['custom_arg'] == 42

    @pytest.mark.asyncio
    async def test_resolve_unit_ids_via_note_key_is_deterministic(self) -> None:
        """``note_key`` resolves to every unit extracted from that note via
        the runner-injected ``_note_key_to_unit_ids`` map. No search call,
        no ambiguity."""
        from memex_eval.suite.setup_actions import _resolve_unit_ids

        nk_map = {
            'widget-lite-discontinued': ['u-lite-1', 'u-lite-2', 'u-lite-3'],
            'widget-pro': ['u-pro-1', 'u-pro-2'],
        }
        params = {
            'note_key': 'widget-lite-discontinued',
            '_note_key_to_unit_ids': nk_map,
        }
        ids = await _resolve_unit_ids(api=None, vault_id=uuid4(), params=params)
        # Exact set; nothing from widget-pro leaks in.
        assert ids == ['u-lite-1', 'u-lite-2', 'u-lite-3']

    @pytest.mark.asyncio
    async def test_resolve_unit_ids_note_key_priority_over_search_query(self) -> None:
        """When both ``note_key`` and ``search_query`` are set, ``note_key``
        wins — the deterministic path takes priority and ``search`` is
        never called."""
        from unittest.mock import AsyncMock, MagicMock

        from memex_eval.suite.setup_actions import _resolve_unit_ids

        api = MagicMock()
        api.search = AsyncMock(return_value=[])  # would be called under search_query
        params = {
            'note_key': 'foo',
            'search_query': 'this should be ignored',
            '_note_key_to_unit_ids': {'foo': ['u-foo-1']},
        }
        ids = await _resolve_unit_ids(api=api, vault_id=uuid4(), params=params)
        assert ids == ['u-foo-1']
        api.search.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolve_unit_ids_unknown_note_key_returns_empty(self) -> None:
        """A note_key that doesn't appear in the runner's map returns []
        (with a warning logged); the handler short-circuits without
        firing destructive ops on random units."""
        from memex_eval.suite.setup_actions import _resolve_unit_ids

        params = {
            'note_key': 'does-not-exist',
            '_note_key_to_unit_ids': {'other-note': ['u1']},
        }
        ids = await _resolve_unit_ids(api=None, vault_id=uuid4(), params=params)
        assert ids == []


class TestBuiltInSetupActionDispatch:
    """Dispatch tests for the built-in handlers (record_outcome, deprioritize,
    kv_write, consolidation_tick). Each test injects a mock RemoteMemexAPI
    and asserts the right method was called with the right args."""

    @pytest.mark.asyncio
    async def test_record_outcome_dispatches_with_resolved_unit_ids(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from memex_eval.suite.setup_actions import get_setup_action

        api = MagicMock()
        api.record_outcome = AsyncMock(return_value=None)
        vault_id = uuid4()
        handler = get_setup_action('record_outcome')
        result = await handler.run(
            api=api,
            vault_id=vault_id,
            params={
                'note_key': 'k1',
                '_note_key_to_unit_ids': {'k1': ['u1', 'u2']},
                'success': True,
                'count': 2,
                'reason': 'because',
            },
        )
        # Pre-state capture is best-effort (DB unavailable in unit test); the
        # handler still returns the dispatch summary plus an empty prev_state
        # and the audit-low timestamp it would have used for log cleanup.
        assert result['unit_ids'] == ['u1', 'u2']
        assert result['stamped_success'] == 2
        assert result['stamped_failure'] == 0
        assert 'audit_ts_low' in result
        assert isinstance(result['audit_ts_low'], str)
        assert 'prev_state' in result
        assert isinstance(result['prev_state'], dict)
        # 2 calls (count=2), each with both unit_ids passed in one shot.
        assert api.record_outcome.call_count == 2
        for call in api.record_outcome.call_args_list:
            assert call.kwargs['unit_ids'] == ['u1', 'u2']
            assert call.kwargs['success'] is True
            assert call.kwargs['vault_id'] == str(vault_id)
            assert call.kwargs['reason'] == 'because'

    @pytest.mark.asyncio
    async def test_deprioritize_dispatches_per_unit(self) -> None:
        from unittest.mock import AsyncMock, MagicMock
        from uuid import UUID

        from memex_eval.suite.setup_actions import get_setup_action

        api = MagicMock()
        api.deprioritize_memory_unit = AsyncMock(return_value=None)
        u1 = uuid4()
        u2 = uuid4()
        vault_id = uuid4()
        handler = get_setup_action('deprioritize')
        result = await handler.run(
            api=api,
            vault_id=vault_id,
            params={
                'note_key': 'k1',
                '_note_key_to_unit_ids': {'k1': [str(u1), str(u2)]},
                'reason': 'eol',
            },
        )
        assert result == {'unit_ids': [str(u1), str(u2)]}
        assert api.deprioritize_memory_unit.call_count == 2
        called_ids = {
            call.kwargs['unit_id'] for call in api.deprioritize_memory_unit.call_args_list
        }
        assert called_ids == {UUID(str(u1)), UUID(str(u2))}
        for call in api.deprioritize_memory_unit.call_args_list:
            assert call.kwargs['reason'] == 'eol'
            assert call.kwargs['vault_id'] == vault_id

    @pytest.mark.asyncio
    async def test_kv_write_dispatches_to_api_kv_put(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from memex_eval.suite.setup_actions import get_setup_action

        api = MagicMock()
        api.kv_put = AsyncMock(return_value=None)
        handler = get_setup_action('kv_write')
        result = await handler.run(
            api=api,
            vault_id=uuid4(),
            params={'kv_key': 'project:demo:lead', 'kv_value': 'Sarah'},
        )
        api.kv_put.assert_called_once_with(value='Sarah', key='project:demo:lead')
        assert result == {'kv_key': 'project:demo:lead'}

    @pytest.mark.asyncio
    async def test_kv_write_rejects_empty_key(self) -> None:
        from memex_eval.suite.setup_actions import get_setup_action

        handler = get_setup_action('kv_write')
        with pytest.raises(ValueError, match='non-empty kv_key'):
            await handler.run(
                api=None,
                vault_id=uuid4(),
                params={'kv_key': '', 'kv_value': 'v'},
            )
        with pytest.raises(ValueError, match='non-empty kv_key'):
            await handler.run(
                api=None,
                vault_id=uuid4(),
                params={'kv_key': '   ', 'kv_value': 'v'},
            )

    @pytest.mark.asyncio
    async def test_kv_write_rejects_none_value(self) -> None:
        from memex_eval.suite.setup_actions import get_setup_action

        handler = get_setup_action('kv_write')
        with pytest.raises(ValueError, match='kv_value to be set explicitly'):
            await handler.run(
                api=None,
                vault_id=uuid4(),
                params={'kv_key': 'k', 'kv_value': None},
            )

    @pytest.mark.asyncio
    async def test_consolidation_tick_dispatches_with_vault_id(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from memex_eval.suite.setup_actions import get_setup_action

        api = MagicMock()
        api.consolidation_tick = AsyncMock(return_value=None)
        vault_id = uuid4()
        handler = get_setup_action('consolidation_tick')
        result = await handler.run(api=api, vault_id=vault_id, params={})
        api.consolidation_tick.assert_called_once_with(vault_id=vault_id)
        assert result is None

    @pytest.mark.asyncio
    async def test_lint_run_dispatches_to_run_lint_rules(self) -> None:
        from unittest.mock import AsyncMock, MagicMock
        from memex_eval.suite.setup_actions import get_setup_action

        api = MagicMock()
        api.run_lint_rules = AsyncMock(
            return_value={
                'vault_id': '...',
                'total_findings': 3,
                'rules': [
                    {'name': 'rule-a', 'findings_emitted': 2},
                    {'name': 'rule-b', 'findings_emitted': 1},
                ],
            }
        )
        vault_id = uuid4()
        handler = get_setup_action('lint_run')
        result = await handler.run(api=api, vault_id=vault_id, params={})
        api.run_lint_rules.assert_called_once_with(vault_id)
        assert result == {'total_findings': 3, 'rules_run': 2}

    @pytest.mark.asyncio
    async def test_lint_llm_run_dispatches_to_run_lint_llm(self) -> None:
        from unittest.mock import AsyncMock, MagicMock
        from memex_eval.suite.setup_actions import get_setup_action

        api = MagicMock()
        api.run_lint_llm = AsyncMock(
            return_value={
                'vault_id': '...',
                'summaries': [
                    {
                        'check': 'semantic_contradiction',
                        'evaluated': 5,
                        'emitted': 1,
                        'deferred': 0,
                    },
                    {'check': 'schema_drift', 'evaluated': 5, 'emitted': 0, 'deferred': 0},
                ],
            }
        )
        vault_id = uuid4()
        handler = get_setup_action('lint_llm_run')
        result = await handler.run(api=api, vault_id=vault_id, params={})
        api.run_lint_llm.assert_called_once_with(vault_id)
        assert result['findings_emitted'] == 1
        assert len(result['summaries']) == 2

    @pytest.mark.asyncio
    async def test_trigger_reflections_raises_when_all_reflect_calls_fail(self) -> None:
        """If every entity's reflect() raises, the handler must surface a
        RuntimeError so a downstream scenario doesn't silently score against
        an un-reflected vault."""
        from unittest.mock import AsyncMock, MagicMock

        from memex_eval.suite.setup_actions import get_setup_action

        api = MagicMock()
        ent = MagicMock()
        ent.id = uuid4()
        ent.name = 'Acme'
        api.get_top_entities = AsyncMock(return_value=[ent])
        api.reflect = AsyncMock(side_effect=RuntimeError('upstream'))
        handler = get_setup_action('trigger_reflections')
        with pytest.raises(RuntimeError, match='all 1 reflect'):
            await handler.run(api=api, vault_id=uuid4(), params={'count': 1, 'timeout_s': 0})

    @pytest.mark.asyncio
    async def test_trigger_reflections_reports_partial_failure(self) -> None:
        """Some reflect() calls succeed, some fail — handler returns a context
        dict with succeeded count + failed entity names so the runner can
        surface partial degradation."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from memex_eval.suite.setup_actions import get_setup_action

        api = MagicMock()
        ents = []
        for nm in ('Acme', 'Beta', 'Gamma'):
            e = MagicMock()
            e.id = uuid4()
            e.name = nm
            ents.append(e)
        api.get_top_entities = AsyncMock(return_value=ents)

        async def _reflect(req):
            if 'Beta' in str(req):  # entity_id is a UUID, so this never hits
                raise RuntimeError('beta-broken')
            return None

        # Use side_effect with a list to inject one failure deterministically.
        api.reflect = AsyncMock(side_effect=[None, RuntimeError('beta-broken'), None])
        # Make probe-search return a hit immediately so we don't loop.
        api.search = AsyncMock(return_value=[MagicMock()])

        handler = get_setup_action('trigger_reflections')
        ctx = await handler.run(api=api, vault_id=uuid4(), params={'count': 3, 'timeout_s': 5})
        assert ctx['reflected_count'] == 2
        assert ctx['failed_count'] == 1
        assert ctx['failed_entities'] == ['Beta']
        assert ctx['requested_count'] == 3
        # Drain pending coroutines created by AsyncMock
        await asyncio.sleep(0)

    @pytest.mark.asyncio
    async def test_trigger_reflections_target_entity_not_in_top_n_is_resolved(self) -> None:
        """A ``target_entity_names`` value that doesn't make the top-N by
        mention_count is resolved via ``search_entities`` and prepended
        to the reflection queue, so the consumer scenario gets a
        guaranteed reflect on it."""
        from unittest.mock import AsyncMock, MagicMock

        from memex_eval.suite.setup_actions import get_setup_action

        # Top-N excludes Sarah Chen.
        top = []
        for nm in ('TechCo Global', 'Department Head', 'PostgreSQL 16'):
            e = MagicMock()
            e.id = uuid4()
            e.name = nm
            top.append(e)

        sarah = MagicMock()
        sarah.id = uuid4()
        sarah.name = 'Sarah Chen'

        api = MagicMock()
        api.get_top_entities = AsyncMock(return_value=top)
        api.search_entities = AsyncMock(return_value=[sarah])
        api.reflect = AsyncMock()
        # First poll iteration succeeds — break the loop fast.
        api.search = AsyncMock(return_value=[MagicMock(), MagicMock()])

        handler = get_setup_action('trigger_reflections')
        ctx = await handler.run(
            api=api,
            vault_id=uuid4(),
            params={
                'count': 3,
                'target_entity_names': ['Sarah Chen'],
                'timeout_s': 30,
                'min_mental_model_hits': 2,
            },
        )

        # Sarah Chen must have been reflected on.
        called_ids = [c.args[0].entity_id for c in api.reflect.call_args_list]
        assert sarah.id in called_ids, f'Sarah Chen never reflected. Called with: {called_ids}'
        # First call is target_entities[0] = Sarah Chen (prepended to queue).
        assert called_ids[0] == sarah.id
        assert ctx['probe_entities'] == ['Sarah Chen']

    @pytest.mark.asyncio
    async def test_trigger_reflections_target_name_whitespace_normalised(self) -> None:
        """Extraction sometimes canonicalises 'Sarah Chen' as '  Sarah  Chen  '
        (extra/double whitespace). The post-filter must accept it for the
        target 'Sarah Chen' rather than dropping the target."""
        from unittest.mock import AsyncMock, MagicMock

        from memex_eval.suite.setup_actions import get_setup_action

        sarah_padded = MagicMock()
        sarah_padded.id = uuid4()
        sarah_padded.name = '  Sarah  Chen  '

        api = MagicMock()
        api.get_top_entities = AsyncMock(return_value=[])
        api.search_entities = AsyncMock(return_value=[sarah_padded])
        api.reflect = AsyncMock()
        api.search = AsyncMock(return_value=[MagicMock()])

        handler = get_setup_action('trigger_reflections')
        ctx = await handler.run(
            api=api,
            vault_id=uuid4(),
            params={
                'count': 3,
                'target_entity_names': ['Sarah Chen'],
                'timeout_s': 30,
            },
        )

        called_ids = [c.args[0].entity_id for c in api.reflect.call_args_list]
        assert sarah_padded.id in called_ids
        assert ctx.get('dropped_targets', []) == []

    @pytest.mark.asyncio
    async def test_trigger_reflections_target_name_case_insensitive(self) -> None:
        """search_entities returns 'sarah chen' (lowercase canonical form);
        the post-filter must accept it for the target 'Sarah Chen' rather
        than silently dropping the target."""
        from unittest.mock import AsyncMock, MagicMock

        from memex_eval.suite.setup_actions import get_setup_action

        sarah_lower = MagicMock()
        sarah_lower.id = uuid4()
        sarah_lower.name = 'sarah chen'  # extraction-canonical lowercase

        api = MagicMock()
        api.get_top_entities = AsyncMock(return_value=[])
        api.search_entities = AsyncMock(return_value=[sarah_lower])
        api.reflect = AsyncMock()
        api.search = AsyncMock(return_value=[MagicMock()])

        handler = get_setup_action('trigger_reflections')
        ctx = await handler.run(
            api=api,
            vault_id=uuid4(),
            params={
                'count': 3,
                'target_entity_names': ['Sarah Chen'],
                'timeout_s': 30,
            },
        )

        called_ids = [c.args[0].entity_id for c in api.reflect.call_args_list]
        assert sarah_lower.id in called_ids
        assert ctx.get('dropped_targets', []) == []

    @pytest.mark.asyncio
    async def test_trigger_reflections_per_target_polling_doesnt_fake_ready(self) -> None:
        """Without ``probe_query``, polling probes per-target. If one target
        has zero hits while another has many, the zero-hit target stays
        pending until timeout — it must NOT be declared ready by the other
        target's results."""
        from unittest.mock import AsyncMock, MagicMock

        from memex_eval.suite.setup_actions import get_setup_action

        api = MagicMock()
        e1 = MagicMock()
        e1.id = uuid4()
        e1.name = 'Alpha'
        e2 = MagicMock()
        e2.id = uuid4()
        e2.name = 'Beta'
        api.get_top_entities = AsyncMock(return_value=[e1, e2])
        api.reflect = AsyncMock()

        # Per-target probe: Alpha has 5 hits, Beta has 0. Beta should
        # remain pending and the run should time out with Beta listed.
        async def _search(query, **_kw):
            if query == 'Alpha':
                return [MagicMock() for _ in range(5)]
            return []

        api.search = AsyncMock(side_effect=_search)

        handler = get_setup_action('trigger_reflections')
        ctx = await handler.run(
            api=api,
            vault_id=uuid4(),
            params={
                'count': 2,
                'target_entity_names': ['Alpha', 'Beta'],
                'min_mental_model_hits': 5,
                'timeout_s': 4,  # short — the loop sleeps 3s between checks
            },
        )

        assert ctx.get('timed_out') is True
        assert ctx.get('unmaterialized_targets') == ['Beta']
        assert ctx.get('probe_mode') == 'per-target'

    @pytest.mark.asyncio
    async def test_trigger_reflections_shared_probe_documented_behaviour(self) -> None:
        """With ``probe_query`` set, polling uses one shared search and gates
        ALL targets on it (documented as 'shared probe'). Test asserts the
        shared-probe semantics are consistent: one search per loop, every
        target moves ready/pending together."""
        from unittest.mock import AsyncMock, MagicMock

        from memex_eval.suite.setup_actions import get_setup_action

        api = MagicMock()
        e1 = MagicMock()
        e1.id = uuid4()
        e1.name = 'Alpha'
        e2 = MagicMock()
        e2.id = uuid4()
        e2.name = 'Beta'
        api.get_top_entities = AsyncMock(return_value=[e1, e2])
        api.reflect = AsyncMock()

        # Shared probe returns 5 hits — both targets ready in one pass.
        api.search = AsyncMock(return_value=[MagicMock() for _ in range(5)])

        handler = get_setup_action('trigger_reflections')
        ctx = await handler.run(
            api=api,
            vault_id=uuid4(),
            params={
                'count': 2,
                'target_entity_names': ['Alpha', 'Beta'],
                'min_mental_model_hits': 5,
                'probe_query': 'shared query',
                'timeout_s': 30,
            },
        )

        assert ctx.get('timed_out') is not True
        assert ctx.get('unmaterialized_targets') is None
        assert ctx.get('probe_mode') == 'shared'
        # Shared probe should issue ONE search per polling iteration, not N.
        assert api.search.call_count == 1
        # All calls used the shared probe query.
        assert api.search.call_args.kwargs.get('query') == 'shared query'


class TestTeardown:
    """P4: each setup_action gets an optional teardown(); the runner
    invokes teardown after the scenario score completes (regardless of
    pass/fail/error) so the next scenario starts clean.

    Per-handler isolation: a single failing teardown does not abort
    later teardowns. Skip teardowns whose run() never executed.
    """

    @pytest.mark.asyncio
    async def test_default_teardown_is_noop(self) -> None:
        """Subclass that doesn't override teardown gets a no-op default."""
        from memex_eval.suite.setup_actions import get_setup_action

        @register_setup_action('noop_handler_teardown_default')
        class _Noop(SetupActionHandler):
            async def run(self, api, vault_id, params):
                return None

        handler = get_setup_action('noop_handler_teardown_default')
        # Should not raise.
        await handler.teardown(api=None, vault_id=uuid4(), params={}, setup_context=None)

    @pytest.mark.asyncio
    async def test_record_outcome_teardown_flips_to_cancel_success(self) -> None:
        """record_outcome teardown issues N inverse failure outcomes for
        every N successes the run() stamped, zeroing the MW differential
        on the touched units."""
        from unittest.mock import AsyncMock, MagicMock

        from memex_eval.suite.setup_actions import get_setup_action

        api = MagicMock()
        api.record_outcome = AsyncMock(return_value=None)
        handler = get_setup_action('record_outcome')
        await handler.teardown(
            api=api,
            vault_id=uuid4(),
            params={'note_key': 'k', 'success': True, 'count': 3},
            setup_context={
                'record_outcome.unit_ids': ['u1', 'u2'],
                'record_outcome.stamped_success': 3,
                'record_outcome.stamped_failure': 0,
            },
        )
        # 3 inverse failure outcomes — one per stamped success.
        assert api.record_outcome.call_count == 3
        for call in api.record_outcome.call_args_list:
            assert call.kwargs['success'] is False
            assert call.kwargs['unit_ids'] == ['u1', 'u2']

    @pytest.mark.asyncio
    async def test_record_outcome_teardown_flips_to_cancel_failure(self) -> None:
        """Mirror case: stamped failures get cancelled by inverse successes."""
        from unittest.mock import AsyncMock, MagicMock

        from memex_eval.suite.setup_actions import get_setup_action

        api = MagicMock()
        api.record_outcome = AsyncMock(return_value=None)
        handler = get_setup_action('record_outcome')
        await handler.teardown(
            api=api,
            vault_id=uuid4(),
            params={'note_key': 'k', 'success': False, 'count': 2},
            setup_context={
                'record_outcome.unit_ids': ['u1'],
                'record_outcome.stamped_success': 0,
                'record_outcome.stamped_failure': 2,
            },
        )
        assert api.record_outcome.call_count == 2
        for call in api.record_outcome.call_args_list:
            assert call.kwargs['success'] is True

    @pytest.mark.asyncio
    async def test_record_outcome_teardown_noop_strategy(self) -> None:
        """Suites that need pristine MW counters can opt out of flip-cancel
        via ``teardown_strategy='noop'``."""
        from unittest.mock import AsyncMock, MagicMock

        from memex_eval.suite.setup_actions import get_setup_action

        api = MagicMock()
        api.record_outcome = AsyncMock(return_value=None)
        handler = get_setup_action('record_outcome')
        await handler.teardown(
            api=api,
            vault_id=uuid4(),
            params={
                'note_key': 'k',
                'success': True,
                'count': 3,
                'teardown_strategy': 'noop',
            },
            setup_context={
                'record_outcome.unit_ids': ['u1'],
                'record_outcome.stamped_success': 3,
                'record_outcome.stamped_failure': 0,
            },
        )
        api.record_outcome.assert_not_called()

    @pytest.mark.asyncio
    async def test_deprioritize_teardown_calls_restore(self) -> None:
        """deprioritize teardown restores every unit from setup context."""
        from unittest.mock import AsyncMock, MagicMock
        from memex_eval.suite.setup_actions import get_setup_action

        api = MagicMock()
        api.restore_memory_unit = AsyncMock(return_value=None)
        u1, u2 = uuid4(), uuid4()
        handler = get_setup_action('deprioritize')
        await handler.teardown(
            api=api,
            vault_id=uuid4(),
            params={'note_key': 'k'},
            setup_context={'deprioritize.unit_ids': [str(u1), str(u2)]},
        )
        assert api.restore_memory_unit.call_count == 2
        called_ids = {c.kwargs['unit_id'] for c in api.restore_memory_unit.call_args_list}
        assert called_ids == {u1, u2}

    @pytest.mark.asyncio
    async def test_kv_write_teardown_calls_kv_delete(self) -> None:
        from unittest.mock import AsyncMock, MagicMock
        from memex_eval.suite.setup_actions import get_setup_action

        api = MagicMock()
        api.kv_delete = AsyncMock(return_value=None)
        handler = get_setup_action('kv_write')
        await handler.teardown(
            api=api,
            vault_id=uuid4(),
            params={'kv_key': 'project:demo:lead', 'kv_value': 'Sarah'},
            setup_context={'kv_write.kv_key': 'project:demo:lead'},
        )
        api.kv_delete.assert_called_once_with(key='project:demo:lead')

    @pytest.mark.asyncio
    async def test_consolidation_tick_teardown_is_noop(self) -> None:
        from unittest.mock import AsyncMock, MagicMock
        from memex_eval.suite.setup_actions import get_setup_action

        api = MagicMock()
        api.consolidation_tick = AsyncMock(return_value=None)
        handler = get_setup_action('consolidation_tick')
        await handler.teardown(
            api=api,
            vault_id=uuid4(),
            params={},
            setup_context={},
        )
        api.consolidation_tick.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_setup_actions_tracks_executed_kinds(self) -> None:
        """_run_setup_actions records every successfully-run kind in
        _executed_action_kinds — used by the teardown loop to skip handlers
        whose run() never executed."""
        from memex_eval.suite.runner import _run_setup_actions

        @register_setup_action('exec_tracker_a')
        class _A(SetupActionHandler):
            async def run(self, api, vault_id, params):
                return None

        @register_setup_action('exec_tracker_b')
        class _B(SetupActionHandler):
            async def run(self, api, vault_id, params):
                return None

        ctx = await _run_setup_actions(
            api=None,
            vault_id=uuid4(),
            actions=[SetupAction(kind='exec_tracker_a'), SetupAction(kind='exec_tracker_b')],
        )
        assert ctx['_executed_action_kinds'] == ['exec_tracker_a', 'exec_tracker_b']

    @pytest.mark.asyncio
    async def test_run_setup_actions_does_not_track_failed_kinds(self) -> None:
        """A handler that raises is NOT added to _executed_action_kinds."""
        from memex_eval.suite.runner import _run_setup_actions

        @register_setup_action('exec_tracker_raises')
        class _R(SetupActionHandler):
            async def run(self, api, vault_id, params):
                raise RuntimeError('boom')

        ctx = await _run_setup_actions(
            api=None,
            vault_id=uuid4(),
            actions=[SetupAction(kind='exec_tracker_raises')],
        )
        assert 'exec_tracker_raises' not in ctx['_executed_action_kinds']

    @pytest.mark.asyncio
    async def test_run_setup_teardowns_skips_unexecuted(self) -> None:
        """Teardown is NOT called for handlers whose setup never ran."""
        from memex_eval.suite.runner import _run_setup_teardowns

        teardown_calls: list[str] = []

        @register_setup_action('teardown_skip_a')
        class _A(SetupActionHandler):
            async def run(self, api, vault_id, params):
                return None

            async def teardown(self, api, vault_id, params, setup_context):
                teardown_calls.append('a')

        @register_setup_action('teardown_skip_b')
        class _B(SetupActionHandler):
            async def run(self, api, vault_id, params):
                return None

            async def teardown(self, api, vault_id, params, setup_context):
                teardown_calls.append('b')

        # Only 'a' executed — 'b' should NOT have its teardown invoked.
        await _run_setup_teardowns(
            api=None,
            vault_id=uuid4(),
            actions=[SetupAction(kind='teardown_skip_a'), SetupAction(kind='teardown_skip_b')],
            setup_context={'_executed_action_kinds': ['teardown_skip_a']},
        )
        assert teardown_calls == ['a']

    @pytest.mark.asyncio
    async def test_run_setup_teardowns_per_handler_isolation(self) -> None:
        """A failing teardown does NOT abort later teardowns."""
        from memex_eval.suite.runner import _run_setup_teardowns

        teardown_calls: list[str] = []

        @register_setup_action('teardown_isolation_first')
        class _First(SetupActionHandler):
            async def run(self, api, vault_id, params):
                return None

            async def teardown(self, api, vault_id, params, setup_context):
                teardown_calls.append('first')
                raise RuntimeError('first teardown explosion')

        @register_setup_action('teardown_isolation_second')
        class _Second(SetupActionHandler):
            async def run(self, api, vault_id, params):
                return None

            async def teardown(self, api, vault_id, params, setup_context):
                teardown_calls.append('second')

        await _run_setup_teardowns(
            api=None,
            vault_id=uuid4(),
            actions=[
                SetupAction(kind='teardown_isolation_first'),
                SetupAction(kind='teardown_isolation_second'),
            ],
            setup_context={
                '_executed_action_kinds': [
                    'teardown_isolation_first',
                    'teardown_isolation_second',
                ]
            },
        )
        # Both teardowns ran; first raised but second still executed.
        assert teardown_calls == ['first', 'second']

    @pytest.mark.asyncio
    async def test_record_outcome_teardown_uses_db_when_available(self) -> None:
        """When the DB session yields, teardown should run the SQL UPDATE/DELETE
        statements (full reset) and NOT fall back to API flip-cancel."""
        from contextlib import asynccontextmanager
        from unittest.mock import AsyncMock, MagicMock, patch

        from memex_eval.suite.setup_actions import get_setup_action

        api = MagicMock()
        api.record_outcome = AsyncMock(return_value=None)

        # Mock asyncpg connection: every execute() succeeds, transaction is a
        # no-op async context manager. Capture executed SQL for assertion.
        executed_sql: list[str] = []

        async def _record_sql(sql: str, *args: Any) -> str:
            executed_sql.append(sql)
            return ''

        conn = MagicMock()
        conn.execute = AsyncMock(side_effect=_record_sql)

        @asynccontextmanager
        async def _txn():
            yield None

        conn.transaction = MagicMock(side_effect=lambda: _txn())

        @asynccontextmanager
        async def _fake_session():
            yield conn

        u1, u2 = str(uuid4()), str(uuid4())
        handler = get_setup_action('record_outcome')
        with patch('memex_eval.suite.db_teardown.eval_db_session', _fake_session):
            await handler.teardown(
                api=api,
                vault_id=uuid4(),
                params={'note_key': 'k', 'success': True, 'count': 2},
                setup_context={
                    # Per-action context (un-prefixed). The new safety
                    # preconditions are: dsn_validated, full prev_state,
                    # audit_ts_low. All present → DB path runs.
                    'unit_ids': [u1, u2],
                    'stamped_success': 2,
                    'stamped_failure': 0,
                    'audit_ts_low': '2026-05-09T00:00:00+00:00',
                    'prev_state': {
                        u1: {'success_co_count': 0, 'failure_co_count': 0, 'last_outcome_at': None},
                        u2: {'success_co_count': 0, 'failure_co_count': 0, 'last_outcome_at': None},
                    },
                    'dsn_validated': True,
                },
            )
        # DB path must NOT have triggered API flip-cancel.
        api.record_outcome.assert_not_called()
        # Confirm the expected SQL statements ran (per-unit decrement,
        # propagation to unit_entities + mental_models, audit cleanup).
        joined = ' '.join(executed_sql)
        assert 'UPDATE memory_units' in joined
        assert 'UPDATE unit_entities' in joined
        assert 'UPDATE mental_models' in joined
        assert 'DELETE FROM audit_logs' in joined

    @pytest.mark.asyncio
    async def test_record_outcome_teardown_db_failure_falls_back_to_api(self) -> None:
        """When the DB-direct path fails or is refused (DSN unvalidated, missing
        prev_state, etc.), teardown falls back to API-level flip-cancel so the
        MW differential is at least zeroed.

        Asserts BOTH directions of cancellation: stamped_success → inverse
        failures, stamped_failure → inverse successes. Total call_count must
        equal stamped_success + stamped_failure (review round-1 MEDIUM #11)."""
        from unittest.mock import AsyncMock, MagicMock

        from memex_eval.suite.setup_actions import get_setup_action

        api = MagicMock()
        api.record_outcome = AsyncMock(return_value=None)

        # Trigger fallback by leaving ``dsn_validated`` False — the new
        # safety preconditions refuse the DB-direct path.
        handler = get_setup_action('record_outcome')
        await handler.teardown(
            api=api,
            vault_id=uuid4(),
            params={'note_key': 'k', 'success': True, 'count': 3},
            setup_context={
                'unit_ids': ['u1', 'u2'],
                'stamped_success': 2,
                'stamped_failure': 3,
                'audit_ts_low': '2026-05-09T00:00:00+00:00',
                'prev_state': {},
                'dsn_validated': False,
            },
        )
        # 2 inverse-failure + 3 inverse-success = 5 total calls.
        assert api.record_outcome.call_count == 5
        success_calls = [
            c for c in api.record_outcome.call_args_list if c.kwargs['success'] is True
        ]
        failure_calls = [
            c for c in api.record_outcome.call_args_list if c.kwargs['success'] is False
        ]
        # 2 stamped successes are cancelled by 2 inverse-failure calls.
        assert len(failure_calls) == 2
        # 3 stamped failures are cancelled by 3 inverse-success calls.
        assert len(success_calls) == 3
        for call in api.record_outcome.call_args_list:
            assert call.kwargs['unit_ids'] == ['u1', 'u2']

    @pytest.mark.asyncio
    async def test_two_record_outcome_actions_get_isolated_teardown_contexts(self) -> None:
        """Two ``record_outcome`` actions in one scenario must each see their
        OWN run() return at teardown — not the merged ``record_outcome.unit_ids``
        key which the second action would clobber on the first.

        This is the bug that left ``Project Zeta Achievement`` units at
        success_co_count=3 after the suite finished: the first action stamped
        the achievement units, the second stamped the incident units, but at
        teardown both handlers read the SAME merged key (incident-only),
        so the achievement stamp was never reverted.
        """
        from contextlib import asynccontextmanager
        from unittest.mock import AsyncMock, MagicMock, patch

        from memex_eval.suite.runner import _run_setup_actions, _run_setup_teardowns

        api = MagicMock()
        api.record_outcome = AsyncMock(return_value=None)

        # Each call records what unit_ids were passed (proxies the SQL).
        executed_against: list[list[str]] = []

        async def _record_sql(sql: str, *args: Any) -> str:
            # Capture the unit_ids array from any UPDATE memory_units call.
            if 'UPDATE memory_units' in sql and 'success_co_count' in sql:
                # args[3] is the unit_id (per-row UPDATE) for the prev_state path.
                if len(args) >= 4:
                    executed_against.append([str(args[3])])
            elif 'UPDATE unit_entities' in sql:
                # args[2] is the unit_ids array.
                if len(args) >= 3:
                    executed_against.append([str(u) for u in args[2]])
            return ''

        # ``conn.fetch`` returns one row per requested unit_id so the
        # run-time DSN sanity check (len(rows)==len(ids)) passes for both
        # actions; the row carries a baseline counter snapshot.
        async def _fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
            ids_arg = args[0] if args else []
            return [
                {
                    'id': str(uid),
                    'success_co_count': 0,
                    'failure_co_count': 0,
                    'last_outcome_at': None,
                }
                for uid in ids_arg
            ]

        from datetime import datetime, timezone

        async def _fetchval(sql: str, *args: Any) -> Any:
            # PG-side timestamp for audit_ts_low capture.
            if 'now()' in sql:
                return datetime.now(timezone.utc)
            return None

        conn = MagicMock()
        conn.fetch = AsyncMock(side_effect=_fetch)
        conn.fetchval = AsyncMock(side_effect=_fetchval)
        conn.execute = AsyncMock(side_effect=_record_sql)

        # Each call to ``conn.transaction()`` must return a FRESH async
        # context manager — a single _txn() is exhausted after one entry.
        @asynccontextmanager
        async def _txn():
            yield None

        conn.transaction = MagicMock(side_effect=lambda: _txn())

        @asynccontextmanager
        async def _fake_session():
            yield conn

        actions = [
            SetupAction(
                kind='record_outcome',
                note_key='achievement',
                success=True,
                count=3,
            ),
            SetupAction(
                kind='record_outcome',
                note_key='incident',
                success=False,
                count=3,
            ),
        ]
        # Distinct unit_ids per note_key — the bug only surfaces when the
        # two actions stamp DIFFERENT units (otherwise the overwrite is a
        # no-op at teardown time).
        nk_map = {
            'achievement': ['11111111-1111-1111-1111-111111111111'],
            'incident': ['22222222-2222-2222-2222-222222222222'],
        }

        with patch('memex_eval.suite.db_teardown.eval_db_session', _fake_session):
            ctx = await _run_setup_actions(
                api=api,
                vault_id=uuid4(),
                actions=actions,
                note_key_to_unit_ids=nk_map,
            )

            # The runner must store per-action results, NOT just merged context.
            per = ctx.get('_per_action_results') or []
            assert len(per) == 2
            assert per[0] is not None and per[1] is not None
            assert per[0]['unit_ids'] == ['11111111-1111-1111-1111-111111111111']
            assert per[1]['unit_ids'] == ['22222222-2222-2222-2222-222222222222']
            assert per[0]['stamped_success'] == 3
            assert per[0]['stamped_failure'] == 0
            assert per[1]['stamped_success'] == 0
            assert per[1]['stamped_failure'] == 3

            await _run_setup_teardowns(
                api=api,
                vault_id=uuid4(),
                actions=actions,
                setup_context=ctx,
            )

        # Both achievement AND incident unit_ids were operated on by the
        # teardown SQL — proves the per-action context isolation worked.
        # (Pre-fix, only the incident unit_ids would have appeared because
        # both teardowns shared the merged ``record_outcome.unit_ids`` key.)
        all_targeted: set[str] = set()
        for ids in executed_against:
            all_targeted.update(ids)
        assert '11111111-1111-1111-1111-111111111111' in all_targeted
        assert '22222222-2222-2222-2222-222222222222' in all_targeted


class TestSetupActionExtraFields:
    def test_setup_action_accepts_arbitrary_extra_fields(self) -> None:
        """Custom actions can carry their own params via extra='allow'."""
        action = SetupAction.model_validate(
            {'kind': 'snapshot_baseline', 'target_keywords': ['Sarah Chen'], 'window_s': 30}
        )
        dumped = action.model_dump()
        assert dumped['target_keywords'] == ['Sarah Chen']
        assert dumped['window_s'] == 30


class TestOutcomeContextThreaded:
    """The outcome's score(context=...) gets the dict published by setup."""

    @pytest.mark.asyncio
    async def test_delta_outcome_reads_baseline_from_context(self) -> None:
        @register_outcome('baseline_delta_demo')
        class _Demo(ExpectedOutcomeBase):
            type: Literal['baseline_delta_demo']
            min_delta: float

            def score(self, answer, scenario, *, context=None, **_kw) -> dict[str, float]:
                ctx = context or {}
                before = float(ctx.get('baseline.value', 0.0))
                after = float(ctx.get('post.value', 0.0))
                delta = after - before
                return {'delta': delta, 'pass': 1.0 if delta >= self.min_delta else 0.0}

            def metric_keys(self, top_k: int | None = None) -> list[str]:
                return ['delta', 'pass']

        outcome = _Demo(type='baseline_delta_demo', min_delta=0.1)
        scenario = Scenario(
            id='delta_check',
            description='d',
            query='q',
            expected=outcome,
        )
        # Simulate the runner threading context into score()
        result = outcome.score(
            AgentAnswer(),
            scenario,
            context={'baseline.value': 0.3, 'post.value': 0.5},
        )
        assert result == {'delta': pytest.approx(0.2), 'pass': 1.0}


class TestEntryPointDiscovery:
    def test_entry_point_plugin_is_discoverable(self) -> None:
        """A plugin published via entry_points should appear in discover_suite_names()."""
        from memex_eval.suite.loader import discover_suite_names

        # Build a fake module that exposes SUITE
        from types import SimpleNamespace

        fake_suite = Suite(
            metadata=SuiteMetadata(
                name='ext_plugin_suite',
                schema_version='1',
                suite_version='1.0.0',
                description='d',
            ),
            sources=SuiteSources(notes=[]),
            scenarios=[],
        )
        fake_module = SimpleNamespace(SUITE=fake_suite, __name__='fake.mod')

        class FakeEntryPoint:
            name = 'ext_plugin_suite'
            value = 'fake.mod'

            def load(self_inner):
                return fake_module

        with patch(
            'memex_eval.suite.loader.importlib.metadata.entry_points',
            return_value=[FakeEntryPoint()],
        ):
            assert 'ext_plugin_suite' in discover_suite_names()

    def test_entry_point_plugin_can_be_loaded(self) -> None:
        from memex_eval.suite.loader import load_suite
        from types import SimpleNamespace

        fake_suite = Suite(
            metadata=SuiteMetadata(
                name='ep_loaded',
                schema_version='1',
                suite_version='1.0.0',
                description='d',
            ),
            sources=SuiteSources(notes=[]),
            scenarios=[],
        )
        fake_module = SimpleNamespace(SUITE=fake_suite, __name__='fake.mod')

        class FakeEntryPoint:
            name = 'ep_loaded'
            value = 'fake.mod'

            def load(self_inner):
                return fake_module

        with patch(
            'memex_eval.suite.loader.importlib.metadata.entry_points',
            return_value=[FakeEntryPoint()],
        ):
            suite = load_suite('ep_loaded')
            assert suite.name == 'ep_loaded'

    def test_built_in_takes_priority_over_entry_point(self) -> None:
        """When a built-in name collides with an entry-point, built-in wins."""
        from memex_eval.suite.loader import load_suite

        class FakeEntryPoint:
            name = 'acme_corp'
            value = 'fake.mod'

            def load(self_inner):
                raise AssertionError('Should not be reached — built-in must win')

        with patch(
            'memex_eval.suite.loader.importlib.metadata.entry_points',
            return_value=[FakeEntryPoint()],
        ):
            suite = load_suite('acme_corp')
            assert suite.name == 'acme_corp'


class TestCanonicalUUIDNoteId:
    """``_ingest_sources`` normalises ``IngestResponse.note_id`` so the
    score functions' string compare against ``MemoryUnitDTO.note_id``
    works regardless of which form the wire used. The runner-level fix
    must:

    1. Round-trip a 32-char dashless hex into canonical dashed form.
    2. Round-trip an already-canonical UUID unchanged.
    3. Tolerate a non-UUID literal (future server change) — log warning,
       store raw, do not crash.
    """

    def test_canonical_uuid_normalises_dashless_hex(self) -> None:
        from memex_eval.suite.runner import _canonical_uuid

        result = _canonical_uuid('602ccf5012c8d27e7132e8c0c2f84c64', 'project-zeta-achievement')
        assert result == '602ccf50-12c8-d27e-7132-e8c0c2f84c64'

    def test_canonical_uuid_passes_through_canonical(self) -> None:
        from memex_eval.suite.runner import _canonical_uuid

        result = _canonical_uuid('602ccf50-12c8-d27e-7132-e8c0c2f84c64', 'note-key')
        assert result == '602ccf50-12c8-d27e-7132-e8c0c2f84c64'

    def test_canonical_uuid_falls_back_on_non_uuid(self, caplog) -> None:
        """Future server returns an opaque 16-char idempotency key that
        isn't a UUID literal — the harness must log + degrade rather
        than crash mid-ingest."""
        import logging

        from memex_eval.suite import runner as _runner

        # Reset the once-per-session warning set so the test's caplog sees
        # the warning (other tests may have populated it).
        _runner._canonical_uuid_warned.clear()

        with caplog.at_level(logging.WARNING, logger='memex_eval.suite.runner'):
            result = _runner._canonical_uuid('not-a-uuid', 'note-key')
        assert result == 'not-a-uuid'
        assert any('not a UUID literal' in r.message for r in caplog.records)

    def test_canonical_uuid_rate_limits_repeated_warning(self, caplog) -> None:
        """A wire-format drift hits every note in the suite; emitting an
        identical WARNING per note drowns the actionable signal. The
        warning must fire once per distinct value per session."""
        import logging

        from memex_eval.suite import runner as _runner

        _runner._canonical_uuid_warned.clear()
        with caplog.at_level(logging.WARNING, logger='memex_eval.suite.runner'):
            for i in range(5):
                _runner._canonical_uuid('opaque-format', f'note-{i}')
        warnings = [r for r in caplog.records if 'not a UUID literal' in r.message]
        assert len(warnings) == 1, f'expected 1 warning, got {len(warnings)}'

    def test_canonical_uuid_rejects_empty(self) -> None:
        """An empty string would be silently coerced to '' and never match
        any UUID — that mode is the original silent mis-attribute. Raise
        so the caller's truthy-guard is the single point of truth."""
        from memex_eval.suite.runner import _canonical_uuid

        with pytest.raises(ValueError, match='empty value'):
            _canonical_uuid('', 'note-key')


class TestLintEnrichment:
    """``LLMLintFlagsUnit`` scores via substring match on
    ``finding.unit_text``, but the ``/lint/findings`` API returns no
    such field — only ``target_id``. The agent backend resolves
    ``target_id`` -> memory unit and grafts ``unit_text`` onto the
    finding. Adversarial tests:

    1. Successful resolution surfaces the unit text on every finding.
    2. A failed resolution is logged at WARNING (not DEBUG) so the
       run-level summary surfaces the silent-fail mode.
    3. Findings whose ``target_type`` is not ``memory_unit`` are passed
       through without an enrichment attempt.
    4. Resolutions are issued concurrently (asyncio.gather), not N+1.
    """

    @pytest.mark.asyncio
    async def test_lint_enrichment_grafts_unit_text(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from memex_eval.suite.agents import DirectApiBackend
        from memex_eval.suite.base import LLMLintFlagsUnit, Scenario

        api = MagicMock()
        api.lint_findings = AsyncMock(
            return_value={
                'findings': [
                    {
                        'id': 'f1',
                        'target_type': 'memory_unit',
                        'target_id': 'u1',
                        'rule_name': 'llm_semantic_contradiction',
                    },
                ]
            }
        )
        unit = MagicMock()
        unit.text = 'API uses URL-based versioning'
        api.get_memory_unit = AsyncMock(return_value=unit)

        scenario = Scenario(
            id='s1',
            description='d',
            query='q',
            expected=LLMLintFlagsUnit(
                type='llm_lint_flags_unit',
                target_keywords=['URL-based'],
            ),
        )
        backend = DirectApiBackend()
        answer = await backend.answer(
            scenario, api=api, vault_id=uuid4(), server_url='http://localhost:8000/api/v1/'
        )
        assert len(answer.lint_findings) == 1
        f = answer.lint_findings[0]
        assert getattr(f, 'unit_text', None) == 'API uses URL-based versioning'

    @pytest.mark.asyncio
    async def test_lint_enrichment_logs_failure_at_warning(self, caplog) -> None:
        import logging
        from unittest.mock import AsyncMock, MagicMock

        from memex_eval.suite.agents import DirectApiBackend
        from memex_eval.suite.base import LLMLintFlagsUnit, Scenario

        api = MagicMock()
        api.lint_findings = AsyncMock(
            return_value={
                'findings': [
                    {
                        'id': 'f1',
                        'target_type': 'memory_unit',
                        'target_id': 'u1',
                        'rule_name': 'llm_semantic_contradiction',
                    },
                ]
            }
        )
        api.get_memory_unit = AsyncMock(side_effect=RuntimeError('not found'))

        scenario = Scenario(
            id='s1',
            description='d',
            query='q',
            expected=LLMLintFlagsUnit(
                type='llm_lint_flags_unit',
                target_keywords=['x'],
            ),
        )
        backend = DirectApiBackend()
        with caplog.at_level(logging.WARNING, logger='memex_eval.suite.agents'):
            answer = await backend.answer(
                scenario, api=api, vault_id=uuid4(), server_url='http://localhost:8000/api/v1/'
            )
        assert len(answer.lint_findings) == 1
        assert getattr(answer.lint_findings[0], 'unit_text', None) == ''
        assert any('could not resolve' in r.message for r in caplog.records)
        assert any('get_memory_unit' in r.message for r in caplog.records)
        # Round-3 MEDIUM 7: the failure count must surface on AgentAnswer
        # so the runner can thread it into actual_summary (where MLflow /
        # JSON artifacts read it from). A regression that reset the
        # counter or dropped the surfacing line would otherwise pass.
        assert answer.lint_enrichment_failures == 1
        assert answer.lint_enrichment_attempted == 1

    @pytest.mark.asyncio
    async def test_lint_enrichment_skips_non_memory_unit_targets(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from memex_eval.suite.agents import DirectApiBackend
        from memex_eval.suite.base import LLMLintFlagsUnit, Scenario

        api = MagicMock()
        api.lint_findings = AsyncMock(
            return_value={
                'findings': [
                    {
                        'id': 'f1',
                        'target_type': 'note',  # not memory_unit
                        'target_id': 'n1',
                        'rule_name': 'some_rule',
                    },
                ]
            }
        )
        api.get_memory_unit = AsyncMock()  # must NOT be called

        scenario = Scenario(
            id='s1',
            description='d',
            query='q',
            expected=LLMLintFlagsUnit(
                type='llm_lint_flags_unit',
                target_keywords=['x'],
            ),
        )
        backend = DirectApiBackend()
        await backend.answer(
            scenario, api=api, vault_id=uuid4(), server_url='http://localhost:8000/api/v1/'
        )
        api.get_memory_unit.assert_not_called()

    @pytest.mark.asyncio
    async def test_lint_enrichment_resolutions_run_concurrently(self) -> None:
        """Three findings should produce three concurrent get_memory_unit
        calls — measured wall-clock should be ~max(latencies), not the
        sum."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from memex_eval.suite.agents import DirectApiBackend
        from memex_eval.suite.base import LLMLintFlagsUnit, Scenario

        api = MagicMock()
        api.lint_findings = AsyncMock(
            return_value={
                'findings': [
                    {'id': f'f{i}', 'target_type': 'memory_unit', 'target_id': f'u{i}'}
                    for i in range(3)
                ]
            }
        )

        async def _slow_get(uid):
            await asyncio.sleep(0.1)
            mu = MagicMock()
            mu.text = f'text-{uid}'
            return mu

        api.get_memory_unit = AsyncMock(side_effect=_slow_get)

        scenario = Scenario(
            id='s1',
            description='d',
            query='q',
            expected=LLMLintFlagsUnit(type='llm_lint_flags_unit', target_keywords=['x']),
        )
        backend = DirectApiBackend()
        t0 = asyncio.get_event_loop().time()
        await backend.answer(
            scenario, api=api, vault_id=uuid4(), server_url='http://localhost:8000/api/v1/'
        )
        elapsed = asyncio.get_event_loop().time() - t0
        # Sequential would be ≥0.3s; concurrent should be ≤0.2s with overhead.
        assert elapsed < 0.25, f'lint resolution was sequential: {elapsed}s'


class TestIngestSourcesDuplicateTitle:
    """Round-3 MEDIUM 5 + round-4 CRITICAL: round-2 added a duplicate-title
    ``RuntimeError`` guard, but the matching predicate was reading
    ``n.name`` / ``n.id`` against a ``FindNoteResult`` schema that has
    only ``.title`` / ``.note_id``. ``MagicMock`` auto-attrs hid this
    in tests; in production every match was empty and the raise was
    unreachable. Test uses the real schema so a future regression bites
    here, not in production.
    """

    @pytest.mark.asyncio
    async def test_idempotent_skip_with_duplicate_titles_raises(self) -> None:
        import datetime as dt
        from pathlib import Path
        from unittest.mock import AsyncMock, MagicMock

        from memex_common.schemas import FindNoteResult, IngestResponse
        from memex_eval.suite.base import Suite, SuiteMetadata
        from memex_eval.suite.runner import _ingest_sources
        from memex_eval.suite.sources import SourceNote, SuiteSources

        target_vault = uuid4()
        existing1 = FindNoteResult(
            note_id=uuid4(),
            title='Engineering Department Overview',
            score=1.0,
            vault_id=target_vault,
            created_at=dt.datetime(2026, 5, 9, tzinfo=dt.timezone.utc),
            status='active',
        )
        existing2 = FindNoteResult(
            note_id=uuid4(),
            title='Engineering Department Overview',
            score=1.0,
            vault_id=target_vault,
            created_at=dt.datetime(2026, 5, 9, tzinfo=dt.timezone.utc),
            status='active',
        )

        api = MagicMock()
        api.ingest = AsyncMock(
            return_value=IngestResponse(status='skipped', note_id=None, unit_ids=[])
        )
        api.find_notes_by_title = AsyncMock(return_value=[existing1, existing2])

        note = SourceNote(
            path=Path('/tmp/dept-engineering.md'),
            note_key='dept-engineering',
            content='body',
            title='Engineering Department Overview',
        )
        suite = Suite(
            metadata=SuiteMetadata(
                name='dup_title_probe',
                schema_version='1',
                suite_version='0.1.0',
                description='probe',
            ),
            sources=SuiteSources(notes=[note]),
            scenarios=[],
        )

        with pytest.raises(RuntimeError, match='refuse to silently pick one'):
            await _ingest_sources(
                api,
                vault_id_default=target_vault,
                vault_map={None: target_vault},
                suite=suite,
            )

    @pytest.mark.asyncio
    async def test_idempotent_skip_unique_title_resolves_via_note_id(self) -> None:
        """Single match must populate ``note_id_by_key`` with
        ``FindNoteResult.note_id`` (NOT a non-existent ``.id`` attr)."""
        import datetime as dt
        from pathlib import Path
        from unittest.mock import AsyncMock, MagicMock

        from memex_common.schemas import FindNoteResult, IngestResponse
        from memex_eval.suite.base import Suite, SuiteMetadata
        from memex_eval.suite.runner import _ingest_sources
        from memex_eval.suite.sources import SourceNote, SuiteSources

        target_vault = uuid4()
        existing_id = uuid4()
        existing = FindNoteResult(
            note_id=existing_id,
            title='Engineering Department Overview',
            score=1.0,
            vault_id=target_vault,
            created_at=dt.datetime(2026, 5, 9, tzinfo=dt.timezone.utc),
            status='active',
        )

        api = MagicMock()
        api.ingest = AsyncMock(
            return_value=IngestResponse(status='skipped', note_id=None, unit_ids=[])
        )
        api.find_notes_by_title = AsyncMock(return_value=[existing])

        note = SourceNote(
            path=Path('/tmp/dept-engineering.md'),
            note_key='dept-engineering',
            content='body',
            title='Engineering Department Overview',
        )
        suite = Suite(
            metadata=SuiteMetadata(
                name='single_match_probe',
                schema_version='1',
                suite_version='0.1.0',
                description='probe',
            ),
            sources=SuiteSources(notes=[note]),
            scenarios=[],
        )

        result = await _ingest_sources(
            api,
            vault_id_default=target_vault,
            vault_map={None: target_vault},
            suite=suite,
        )
        assert result == {'dept-engineering': str(existing_id)}


class TestDependsOnPriorScenariosValidator:
    """Round-3 MEDIUM 6: ``Scenario.depends_on_prior_scenarios`` is the
    declarative form of round-1's HIGH-1 fix. The Suite validator
    rejects forward refs / self-deps / typos so authors can't write a
    scenario that observes state never stamped by a predecessor.
    """

    def test_validator_accepts_correct_dep_order(self) -> None:
        from memex_eval.suite.base import (
            KeywordsPresent,
            Scenario,
            SetupAction,
            Suite,
            SuiteMetadata,
        )
        from memex_eval.suite.sources import SuiteSources

        suite = Suite(
            metadata=SuiteMetadata(
                name='dep_probe',
                schema_version='1',
                suite_version='0.1.0',
                description='probe',
            ),
            sources=SuiteSources(notes=[]),
            scenarios=[
                Scenario(
                    id='dep',
                    description='d',
                    query='q',
                    setup_actions=[
                        SetupAction(kind='record_outcome', search_query='x', success=True)
                    ],
                    expected=KeywordsPresent(type='keywords_present', keywords=['x']),
                ),
                Scenario(
                    id='consumer',
                    description='d',
                    query='q',
                    depends_on_prior_scenarios=['dep'],
                    expected=KeywordsPresent(type='keywords_present', keywords=['x']),
                ),
            ],
        )
        assert suite.scenarios[1].depends_on_prior_scenarios == ['dep']

    def test_validator_rejects_forward_reference(self) -> None:
        from memex_eval.suite.base import (
            KeywordsPresent,
            Scenario,
            Suite,
            SuiteMetadata,
        )
        from memex_eval.suite.sources import SuiteSources

        with pytest.raises(ValueError, match='does not appear earlier'):
            Suite(
                metadata=SuiteMetadata(
                    name='fwd_probe',
                    schema_version='1',
                    suite_version='0.1.0',
                    description='probe',
                ),
                sources=SuiteSources(notes=[]),
                scenarios=[
                    Scenario(
                        id='consumer',
                        description='d',
                        query='q',
                        depends_on_prior_scenarios=['dep'],
                        expected=KeywordsPresent(type='keywords_present', keywords=['x']),
                    ),
                    Scenario(
                        id='dep',
                        description='d',
                        query='q',
                        expected=KeywordsPresent(type='keywords_present', keywords=['x']),
                    ),
                ],
            )

    def test_validator_rejects_self_dep(self) -> None:
        from memex_eval.suite.base import (
            KeywordsPresent,
            Scenario,
            Suite,
            SuiteMetadata,
        )
        from memex_eval.suite.sources import SuiteSources

        with pytest.raises(ValueError, match='cannot depend on itself'):
            Suite(
                metadata=SuiteMetadata(
                    name='self_probe',
                    schema_version='1',
                    suite_version='0.1.0',
                    description='probe',
                ),
                sources=SuiteSources(notes=[]),
                scenarios=[
                    Scenario(
                        id='self_dep',
                        description='d',
                        query='q',
                        depends_on_prior_scenarios=['self_dep'],
                        expected=KeywordsPresent(type='keywords_present', keywords=['x']),
                    ),
                ],
            )


class TestOwnSkipReason:
    """Round-3 MEDIUM 6 + round-4 MEDIUM 2: ``_compute_own_skip_reason``
    is the single source of truth for whether a scenario should skip
    based on its own config. The runner uses it both for the scenario
    itself AND its declared ``depends_on_prior_scenarios`` predecessors,
    so a regression that re-narrows the helper to one skip-kind would
    silently re-introduce the round-1 silent-skip bug for transitive deps.
    """

    @staticmethod
    def _build_suite(scenarios: list, *, requires_nli: bool = False):
        from memex_eval.suite.base import Suite, SuiteMetadata
        from memex_eval.suite.sources import SuiteSources

        return Suite(
            metadata=SuiteMetadata(
                name='skip_probe',
                schema_version='1',
                suite_version='0.1.0',
                description='probe',
                requires_nli_classifier=requires_nli,
            ),
            sources=SuiteSources(notes=[]),
            scenarios=scenarios,
        )

    def test_setup_action_not_reusable_under_reuse_vault(self) -> None:
        from memex_eval.suite.base import (
            KeywordsPresent,
            Scenario,
            SetupAction,
        )
        from memex_eval.suite.runner import _compute_own_skip_reason

        sc = Scenario(
            id='dep',
            description='d',
            query='q',
            setup_actions=[SetupAction(kind='record_outcome', search_query='x', success=True)],
            expected=KeywordsPresent(type='keywords_present', keywords=['x']),
        )
        suite = self._build_suite([sc])
        assert (
            _compute_own_skip_reason(
                sc,
                suite=suite,
                reuse_vault='label',
                config_snapshot_available=True,
                nli_available=True,
            )
            == 'setup_action_not_reusable'
        )

    def test_setup_action_not_reusable_only_under_reuse_vault(self) -> None:
        """Same scenario with no reuse_vault must NOT skip — fresh-ingest
        path always re-runs setup actions."""
        from memex_eval.suite.base import (
            KeywordsPresent,
            Scenario,
            SetupAction,
        )
        from memex_eval.suite.runner import _compute_own_skip_reason

        sc = Scenario(
            id='dep',
            description='d',
            query='q',
            setup_actions=[SetupAction(kind='record_outcome', search_query='x', success=True)],
            expected=KeywordsPresent(type='keywords_present', keywords=['x']),
        )
        suite = self._build_suite([sc])
        assert (
            _compute_own_skip_reason(
                sc,
                suite=suite,
                reuse_vault=None,
                config_snapshot_available=True,
                nli_available=True,
            )
            is None
        )

    def test_nli_disabled_when_required(self) -> None:
        """A scenario that requires NLI gets skipped when /system/config
        reports the polarity classifier is off. Round-2 generalised
        transitive skip across this case — must be visible to deps."""
        from memex_eval.suite.base import KeywordsPresent, Scenario
        from memex_eval.suite.runner import _compute_own_skip_reason

        sc = Scenario(
            id='dep',
            description='d',
            query='q',
            requires_nli_classifier=True,
            expected=KeywordsPresent(type='keywords_present', keywords=['x']),
        )
        suite = self._build_suite([sc])
        assert (
            _compute_own_skip_reason(
                sc,
                suite=suite,
                reuse_vault=None,
                config_snapshot_available=True,
                nli_available=False,
            )
            == 'nli_disabled'
        )

    def test_nli_unknown_does_not_skip(self) -> None:
        """When config_snapshot is unavailable (admin auth missing), the
        runner runs the scenario anyway — the helper must return None."""
        from memex_eval.suite.base import KeywordsPresent, Scenario
        from memex_eval.suite.runner import _compute_own_skip_reason

        sc = Scenario(
            id='dep',
            description='d',
            query='q',
            requires_nli_classifier=True,
            expected=KeywordsPresent(type='keywords_present', keywords=['x']),
        )
        suite = self._build_suite([sc])
        assert (
            _compute_own_skip_reason(
                sc,
                suite=suite,
                reuse_vault=None,
                config_snapshot_available=False,
                nli_available=None,
            )
            is None
        )

    def test_transitive_propagation_via_helper(self) -> None:
        """The runner's transitive-skip path reads the dep's own skip
        reason and propagates with prefix ``depends_on_prior_scenario_skipped``.
        Test the components that would be combined: the helper must
        return the SAME reason whether called for the dep directly or
        as a precursor to a dependent's skip propagation.
        """
        from memex_eval.suite.base import (
            KeywordsPresent,
            Scenario,
            SetupAction,
        )
        from memex_eval.suite.runner import _compute_own_skip_reason

        dep = Scenario(
            id='dep',
            description='d',
            query='q',
            setup_actions=[SetupAction(kind='record_outcome', search_query='x', success=True)],
            expected=KeywordsPresent(type='keywords_present', keywords=['x']),
        )
        consumer = Scenario(
            id='consumer',
            description='d',
            query='q',
            depends_on_prior_scenarios=['dep'],
            expected=KeywordsPresent(type='keywords_present', keywords=['x']),
        )
        suite = self._build_suite([dep, consumer])
        # Under reuse_vault, dep is skipped (non-reusable handler) AND
        # consumer's own check returns None (no own setup_actions). The
        # runner combines these to mark consumer skipped on the dep's
        # reason — verified by the runner test's behavior, but the helper
        # itself must produce the right per-scenario verdicts:
        assert (
            _compute_own_skip_reason(
                dep,
                suite=suite,
                reuse_vault='label',
                config_snapshot_available=True,
                nli_available=True,
            )
            == 'setup_action_not_reusable'
        )
        assert (
            _compute_own_skip_reason(
                consumer,
                suite=suite,
                reuse_vault='label',
                config_snapshot_available=True,
                nli_available=True,
            )
            is None
        )


class TestSourceNoteWireContent:
    """Round-3 HIGH 1: ``frontmatter.load()`` strips the YAML block from
    ``post.content``, so `publish_date` etc. never reach the server.
    ``SourceNote.wire_content()`` re-emits the forwarded keys.
    """

    def test_wire_content_re_emits_publish_date(self) -> None:
        from pathlib import Path

        from memex_eval.suite.sources import SourceNote

        note = SourceNote(
            path=Path('/tmp/x.md'),
            note_key='x',
            content='Body of the note.',
            extra_metadata={'publish_date': '2026-05-09'},
        )
        wire = note.wire_content()
        assert wire.startswith('---\npublish_date: 2026-05-09\n---\n\n')
        assert 'Body of the note.' in wire

    def test_wire_content_unchanged_when_no_metadata(self) -> None:
        from pathlib import Path

        from memex_eval.suite.sources import SourceNote

        note = SourceNote(
            path=Path('/tmp/x.md'),
            note_key='x',
            content='Just a body.',
        )
        assert note.wire_content() == 'Just a body.'

    def test_wire_content_with_date_object_round_trips(self) -> None:
        """``frontmatter.load`` (PyYAML) coerces YAML date scalars to
        ``datetime.date``. ``str(date)`` yields ``'2026-05-09'``, which
        the server's ``yaml.safe_load`` re-parses to the same date.
        Test the actual loader output type, not just str."""
        import datetime as dt
        from pathlib import Path

        from memex_eval.suite.sources import SourceNote

        note = SourceNote(
            path=Path('/tmp/x.md'),
            note_key='x',
            content='Body',
            extra_metadata={'publish_date': dt.date(2026, 5, 9)},
        )
        wire = note.wire_content()
        # Manual emission produces 'publish_date: 2026-05-09\n'; YAML
        # parses this back to a date scalar identically.
        assert 'publish_date: 2026-05-09' in wire
        assert wire.startswith('---\n')
        assert '\n---\n\nBody' in wire


class TestInlineNoteTeardown:
    """Inline notes ingested by a scenario must be DELETEd after the
    scenario scores so the next scenario starts with a clean vault.
    Defer-on-dependency: when another scenario lists this one in
    ``depends_on_prior_scenarios``, the delete waits until the
    dependent has run.
    """

    @pytest.mark.asyncio
    async def test_run_inline_note_teardowns_deletes_each_id(self) -> None:
        from unittest.mock import AsyncMock, MagicMock
        from uuid import UUID

        from memex_eval.suite.runner import _run_inline_note_teardowns

        api = MagicMock()
        api.delete_note = AsyncMock(return_value=True)

        n1, n2 = uuid4(), uuid4()
        await _run_inline_note_teardowns(
            api=api,
            setup_context={
                '_inline_note_ids': {'inline-a': str(n1), 'inline-b': str(n2)},
            },
        )
        assert api.delete_note.call_count == 2
        called = {c.args[0] for c in api.delete_note.call_args_list}
        assert called == {UUID(str(n1)), UUID(str(n2))}

    @pytest.mark.asyncio
    async def test_run_inline_note_teardowns_noop_without_ids(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from memex_eval.suite.runner import _run_inline_note_teardowns

        api = MagicMock()
        api.delete_note = AsyncMock()
        await _run_inline_note_teardowns(api=api, setup_context={'_inline_note_ids': {}})
        api.delete_note.assert_not_called()
        # Also: missing key entirely is a no-op (no KeyError).
        await _run_inline_note_teardowns(api=api, setup_context={})
        api.delete_note.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_inline_note_teardowns_per_note_isolation(self) -> None:
        """A single delete failure does not block subsequent deletes."""
        from unittest.mock import AsyncMock, MagicMock

        from memex_eval.suite.runner import _run_inline_note_teardowns

        api = MagicMock()
        # First call raises, second succeeds.
        api.delete_note = AsyncMock(
            side_effect=[RuntimeError('boom'), True],
        )
        n1, n2 = uuid4(), uuid4()
        await _run_inline_note_teardowns(
            api=api,
            setup_context={
                '_inline_note_ids': {'k1': str(n1), 'k2': str(n2)},
            },
        )
        assert api.delete_note.call_count == 2
