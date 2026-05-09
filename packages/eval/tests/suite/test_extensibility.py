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
        """A typo in a setup action's kind logs a warning + records in failures."""
        from memex_eval.suite.runner import _run_setup_actions

        actions = [SetupAction(kind='nonexistent_action_kind_xyz')]
        ctx = await _run_setup_actions(api=None, vault_id=uuid4(), actions=actions)
        assert ctx['_setup_failures'] == [
            {
                'kind': 'nonexistent_action_kind_xyz',
                'error': (
                    "\"Unknown setup action 'nonexistent_action_kind_xyz'. Registered: ['"
                    "consolidation_tick', 'deprioritize', 'kv_write', 'record_outcome', "
                    "'trigger_reflections']\""
                ),
            }
        ]

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
        assert result == {'unit_ids': ['u1', 'u2']}
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
