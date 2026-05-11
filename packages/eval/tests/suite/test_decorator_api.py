"""Tests for the decorator-based Suite authoring API.

Three surfaces:

1. ``suite.register(...)`` — pure declarative, method call.
2. ``@suite.scenario(...)`` — decorator; function body REPLACES expected.
3. ``@suite.register_class`` — class with overrideable lifecycle methods.

These tests exercise:

- Each surface produces a valid ``Scenario`` Pydantic model.
- The decorator API's ``Suite.build()`` round-trips through the loader.
- ``CustomEvaluate.score()`` correctly invokes sync and async user functions
  and surfaces ``AssertionError`` for ``status='fail'`` semantics.
- ``BaseScenario.evaluate`` default delegates to ``self.expected.score()``;
  subclasses can extend (call super) or replace (skip super).
- ``Scenario.group`` field is preserved across round-trips and the runner's
  group filter (``run_suite(groups=...)``) selects correctly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest

from memex_eval.suite.agents import AgentAnswer
from memex_eval.suite.base import (
    KeywordsAbsent,
    KeywordsPresent,
    Scenario,
    SetupAction,
    SuiteMetadata,
)
from memex_eval.suite.decorator import (
    BaseScenario,
    CustomEvaluate,
    ScenarioContext,
    Suite,
)


def _meta(**overrides: Any) -> SuiteMetadata:
    base = dict(
        name='demo_dec',
        schema_version='1',
        suite_version='1.0.0',
        description='demo',
    )
    base.update(overrides)
    return SuiteMetadata(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 1. suite.register — pure declarative
# ---------------------------------------------------------------------------


class TestRegisterMethod:
    def test_register_appends_scenario(self) -> None:
        s = Suite(metadata=_meta(name='reg_basic'))
        sc = s.register(
            id='a',
            query='Q',
            expected=KeywordsPresent(type='keywords_present', keywords=['x']),
        )
        assert isinstance(sc, Scenario)
        built = s.build()
        assert len(built.scenarios) == 1
        assert built.scenarios[0].id == 'a'
        assert built.scenarios[0].expected.type == 'keywords_present'

    def test_register_passes_through_all_scenario_fields(self) -> None:
        s = Suite(metadata=_meta(name='reg_fields'))
        s.register(
            id='full',
            query='Q',
            expected=KeywordsPresent(type='keywords_present', keywords=['x']),
            description='custom desc',
            group='retrieval',
            top_k=20,
            strategies=['keyword'],
            include_superseded=True,
            include_deprioritized=False,
            setup_actions=[SetupAction(kind='record_outcome', note_key='n')],
            vault_name='alt',
            max_duration_ms=5000.0,
            answer_mode='api',
            expected_failure_modes=['claude-code'],
            requires_nli_classifier=True,
            depends_on_prior_scenarios=[],
        )
        built = s.build()
        sc = built.scenarios[0]
        assert sc.description == 'custom desc'
        assert sc.group == 'retrieval'
        assert sc.top_k == 20
        assert sc.strategies == ['keyword']
        assert sc.include_superseded is True
        assert sc.include_deprioritized is False
        assert len(sc.setup_actions) == 1
        assert sc.vault_name == 'alt'
        assert sc.max_duration_ms == 5000.0
        assert sc.answer_mode == 'api'
        assert sc.expected_failure_modes == ['claude-code']
        assert sc.requires_nli_classifier is True

    def test_register_rejects_duplicate_id(self) -> None:
        s = Suite(metadata=_meta(name='reg_dup'))
        s.register(
            id='a', query='Q', expected=KeywordsPresent(type='keywords_present', keywords=['x'])
        )
        with pytest.raises(ValueError, match='Duplicate scenario id'):
            s.register(
                id='a', query='Q', expected=KeywordsPresent(type='keywords_present', keywords=['y'])
            )


# ---------------------------------------------------------------------------
# 2. @suite.scenario — decorator with function-body-as-evaluator
# ---------------------------------------------------------------------------


class TestScenarioDecorator:
    def test_decorator_appends_with_custom_evaluate_outcome(self) -> None:
        s = Suite(metadata=_meta(name='dec_basic'))

        @s.scenario(id='dec_a', query='Q')
        def _eval(ctx: ScenarioContext) -> None:
            ctx.metrics['pass'] = 1.0

        built = s.build()
        assert len(built.scenarios) == 1
        sc = built.scenarios[0]
        assert sc.id == 'dec_a'
        assert isinstance(sc.expected, CustomEvaluate)

    def test_decorator_returns_original_function(self) -> None:
        """The decorator must return the function unchanged so callers
        can keep their own references."""
        s = Suite(metadata=_meta(name='dec_return'))

        def _eval(ctx: ScenarioContext) -> None:
            ctx.metrics['pass'] = 1.0

        decorated = s.scenario(id='dec_b', query='Q')(_eval)
        assert decorated is _eval

    def test_function_body_runs_via_score(self) -> None:
        s = Suite(metadata=_meta(name='dec_score'))

        @s.scenario(id='dec_score_a', query='Q')
        def _eval(ctx: ScenarioContext) -> None:
            assert ctx.query == 'Q'
            ctx.metrics['pass'] = 1.0
            ctx.metrics['custom'] = 0.7

        built = s.build()
        sc = built.scenarios[0]
        # Drive score() manually with a dummy answer.
        answer = AgentAnswer(backend_name='test')
        result = sc.expected.score(answer, sc, context={})
        assert result == {'pass': 1.0, 'custom': 0.7}

    def test_async_function_body_supported(self) -> None:
        s = Suite(metadata=_meta(name='dec_async'))

        @s.scenario(id='dec_async_a', query='Q')
        async def _eval(ctx: ScenarioContext) -> None:
            ctx.metrics['pass'] = 1.0
            ctx.metrics['async_marker'] = 0.5

        built = s.build()
        sc = built.scenarios[0]
        answer = AgentAnswer(backend_name='test')
        result = sc.expected.score(answer, sc, context={})
        assert result == {'pass': 1.0, 'async_marker': 0.5}

    def test_assertion_error_maps_to_pass_zero(self) -> None:
        """``AssertionError`` from the user function is converted to
        ``ctx.metrics = {'pass': 0.0}`` so the runner records a verdict
        (status='fail'), NOT a runner-side error. Other exceptions still
        propagate so unexpected crashes surface as status='error'."""
        s = Suite(metadata=_meta(name='dec_assert'))

        @s.scenario(id='dec_assert_a', query='Q')
        def _eval(ctx: ScenarioContext) -> None:
            assert False, 'expected this to fail'

        sc = s.build().scenarios[0]
        result = sc.expected.score(AgentAnswer(backend_name='test'), sc, context={})
        assert result == {'pass': 0.0}

    def test_runtime_error_still_propagates(self) -> None:
        """Non-assertion exceptions still propagate so the runner can
        record them as status='error' (true runner-side crashes, not
        verdicts)."""
        s = Suite(metadata=_meta(name='dec_crash'))

        @s.scenario(id='dec_crash_a', query='Q')
        def _eval(ctx: ScenarioContext) -> None:
            raise RuntimeError('not an assertion')

        sc = s.build().scenarios[0]
        with pytest.raises(RuntimeError, match='not an assertion'):
            sc.expected.score(AgentAnswer(backend_name='test'), sc, context={})

    def test_decorator_preserves_group_field(self) -> None:
        s = Suite(metadata=_meta(name='dec_group'))

        @s.scenario(id='dec_grp_a', query='Q', group='extraction')
        def _eval(ctx: ScenarioContext) -> None:
            ctx.metrics['pass'] = 1.0

        sc = s.build().scenarios[0]
        assert sc.group == 'extraction'

    def test_empty_metrics_defaults_to_pass_zero_fail_closed(self) -> None:
        """A user function that returns without populating ``ctx.metrics``
        AND without raising fail-closes to ``{'pass': 0.0}``. Defaulting
        to pass=1.0 silently turned 'forgot to assert' into 'scenario
        passes', which inverts what an evaluation framework should do."""
        s = Suite(metadata=_meta(name='dec_empty'))

        @s.scenario(id='dec_empty_a', query='Q')
        def _eval(ctx: ScenarioContext) -> None:
            pass

        sc = s.build().scenarios[0]
        result = sc.expected.score(AgentAnswer(backend_name='test'), sc, context={})
        assert result == {'pass': 0.0}


# ---------------------------------------------------------------------------
# 3. @suite.register_class — full lifecycle override
# ---------------------------------------------------------------------------


class TestRegisterClass:
    def test_class_with_declarative_expected(self) -> None:
        s = Suite(metadata=_meta(name='cls_decl'))

        @s.register_class
        class _C(BaseScenario):
            id: ClassVar[str] = 'cls_a'
            query: ClassVar[str] = 'Q'
            expected: ClassVar[Any] = KeywordsPresent(type='keywords_present', keywords=['x'])

        built = s.build()
        sc = built.scenarios[0]
        assert sc.id == 'cls_a'
        assert getattr(sc, '_base_scenario_instance', None) is not None
        assert sc.expected.type == 'keywords_present'

    def test_class_without_expected_uses_sentinel(self) -> None:
        """A class without ``expected`` set still produces a valid
        Scenario; the runner detects ``_base_scenario_instance`` and
        dispatches to ``instance.evaluate`` instead of touching
        ``scenario.expected``."""
        s = Suite(metadata=_meta(name='cls_no_exp'))

        @s.register_class
        class _C(BaseScenario):
            id: ClassVar[str] = 'cls_b'
            query: ClassVar[str] = 'Q'

            async def evaluate(self, ctx: ScenarioContext) -> None:
                ctx.metrics['pass'] = 1.0

        sc = s.build().scenarios[0]
        # Sentinel CustomEvaluate placeholder so Scenario.expected stays valid.
        assert isinstance(sc.expected, CustomEvaluate)
        # And the live BaseScenario instance is reachable.
        inst = getattr(sc, '_base_scenario_instance')
        assert inst.id == 'cls_b'

    def test_class_missing_id_raises(self) -> None:
        s = Suite(metadata=_meta(name='cls_no_id'))

        with pytest.raises(TypeError, match='must set class attribute ``id``'):

            @s.register_class
            class _NoId(BaseScenario):
                query: ClassVar[str] = 'Q'

    def test_class_missing_query_raises(self) -> None:
        s = Suite(metadata=_meta(name='cls_no_q'))

        with pytest.raises(TypeError, match='must set class attribute ``query``'):

            @s.register_class
            class _NoQ(BaseScenario):
                id: ClassVar[str] = 'cls_x'

    def test_class_must_inherit_from_base_scenario(self) -> None:
        s = Suite(metadata=_meta(name='cls_bad'))

        with pytest.raises(TypeError, match='expects a BaseScenario subclass'):

            @s.register_class
            class _NotASubclass:  # type: ignore[misc]
                id = 'x'
                query = 'Q'

    @pytest.mark.asyncio
    async def test_default_evaluate_runs_expected_score(self) -> None:
        """``BaseScenario.evaluate`` default delegates to
        ``self.expected.score()``."""

        class _C(BaseScenario):
            id: ClassVar[str] = 'cls_eval_default'
            query: ClassVar[str] = 'Q'
            expected: ClassVar[Any] = KeywordsPresent(type='keywords_present', keywords=['Sarah'])

        inst = _C()
        sc = inst.to_scenario_model()
        ctx = ScenarioContext(query='Q', scenario=sc, api=None, vault_id=None, server_url='')
        ctx.answer = AgentAnswer(backend_name='test', answer_text='Sarah Chen leads project alpha.')
        await inst.evaluate(ctx)
        # KeywordsPresent finds 'Sarah' in answer_text → pass=1.0
        assert ctx.metrics.get('pass') == 1.0

    @pytest.mark.asyncio
    async def test_override_evaluate_skip_super_replaces(self) -> None:
        """Subclass that overrides ``evaluate`` and skips ``super()``
        bypasses the declarative ``expected`` entirely."""

        class _C(BaseScenario):
            id: ClassVar[str] = 'cls_eval_replace'
            query: ClassVar[str] = 'Q'
            expected: ClassVar[Any] = KeywordsPresent(
                type='keywords_present', keywords=['NEVER_MATCHED']
            )

            async def evaluate(self, ctx: ScenarioContext) -> None:
                # Don't call super — declarative is bypassed.
                ctx.metrics['pass'] = 1.0
                ctx.metrics['custom'] = 0.42

        inst = _C()
        sc = inst.to_scenario_model()
        ctx = ScenarioContext(query='Q', scenario=sc, api=None, vault_id=None, server_url='')
        ctx.answer = AgentAnswer(backend_name='test', answer_text='whatever')
        await inst.evaluate(ctx)
        # The declarative expected would have failed (no NEVER_MATCHED in
        # answer), but our override skipped it.
        assert ctx.metrics == {'pass': 1.0, 'custom': 0.42}

    @pytest.mark.asyncio
    async def test_override_evaluate_call_super_extends(self) -> None:
        """Subclass that calls ``super().evaluate()`` extends:
        declarative scoring runs first, then the subclass adds metrics."""

        class _C(BaseScenario):
            id: ClassVar[str] = 'cls_eval_extend'
            query: ClassVar[str] = 'Q'
            expected: ClassVar[Any] = KeywordsPresent(type='keywords_present', keywords=['Sarah'])

            async def evaluate(self, ctx: ScenarioContext) -> None:
                await super().evaluate(ctx)  # populates pass=1.0
                ctx.metrics['extra_check'] = 0.9

        inst = _C()
        sc = inst.to_scenario_model()
        ctx = ScenarioContext(query='Q', scenario=sc, api=None, vault_id=None, server_url='')
        ctx.answer = AgentAnswer(backend_name='test', answer_text='Sarah leads project alpha.')
        await inst.evaluate(ctx)
        assert ctx.metrics.get('pass') == 1.0
        assert ctx.metrics.get('extra_check') == 0.9


# ---------------------------------------------------------------------------
# Loader integration
# ---------------------------------------------------------------------------


class TestLoaderRecognizesDecoratorSuite:
    def test_load_suite_from_path_accepts_decorator_suite(self, tmp_path: Path) -> None:
        """A suite directory whose ``__init__.py`` exports
        ``SUITE: decorator.Suite`` (instead of legacy ``Suite``) is loaded
        via the same path; the loader calls ``.build()`` to materialize."""
        suite_dir = tmp_path / 'tmp_dec_suite'
        suite_dir.mkdir()
        (suite_dir / 'sources').mkdir()
        (suite_dir / 'README.md').write_text('# tmp_dec_suite\n')
        (suite_dir / '__init__.py').write_text(
            'from memex_eval.suite.base import (\n'
            '    KeywordsPresent, SuiteMetadata, SuiteSources\n'
            ')\n'
            'from memex_eval.suite.decorator import Suite\n'
            'from pathlib import Path\n'
            '_ROOT = Path(__file__).parent\n'
            "METADATA = SuiteMetadata(name='tmp_dec_suite', schema_version='1', "
            "suite_version='1.0.0', description='demo')\n"
            "suite = Suite(metadata=METADATA, sources=SuiteSources.from_directory(_ROOT / 'sources'),\n"
            "              readme_path=_ROOT / 'README.md')\n"
            "suite.register(id='alpha', query='Q', "
            "expected=KeywordsPresent(type='keywords_present', keywords=['x']))\n"
            'SUITE = suite\n'
        )

        from memex_eval.suite.loader import load_suite

        loaded = load_suite(suite_dir)
        # Loaded as the legacy Suite (loader called .build() on the decorator Suite).
        assert loaded.name == 'tmp_dec_suite'
        assert len(loaded.scenarios) == 1
        assert loaded.scenarios[0].id == 'alpha'


# ---------------------------------------------------------------------------
# Group-filter end-to-end semantics (without server: just the runner's
# included_ids computation via stub).
# ---------------------------------------------------------------------------


class TestComputeFilterInclusion:
    """Direct tests for the runner's filter-inclusion helper.

    The helper walks ``depends_on_prior_scenarios`` for both filter axes,
    so a dependency in a different group / outside the explicit id set
    still runs. Unknown ids/groups raise loud ValueError rather than
    being silently filtered out.
    """

    def _scenarios(self) -> list[Scenario]:
        return [
            Scenario(
                id='setup_x',
                description='-',
                query='Q',
                expected=KeywordsPresent(type='keywords_present', keywords=['x']),
                group='infra',
            ),
            Scenario(
                id='ret_a',
                description='-',
                query='Q',
                expected=KeywordsPresent(type='keywords_present', keywords=['x']),
                group='retrieval',
                depends_on_prior_scenarios=['setup_x'],
            ),
            Scenario(
                id='ret_b',
                description='-',
                query='Q',
                expected=KeywordsPresent(type='keywords_present', keywords=['x']),
                group='retrieval',
            ),
            Scenario(
                id='ext_c',
                description='-',
                query='Q',
                expected=KeywordsPresent(type='keywords_present', keywords=['x']),
                group='extraction',
            ),
        ]

    def test_no_filter_returns_none(self) -> None:
        from memex_eval.suite.runner import _compute_filter_inclusion

        assert _compute_filter_inclusion(self._scenarios(), None, None) is None
        assert _compute_filter_inclusion(self._scenarios(), [], []) is None

    def test_scenario_ids_pulls_in_dependencies(self) -> None:
        from memex_eval.suite.runner import _compute_filter_inclusion

        # Asking for ret_a alone must include its prerequisite ``setup_x``.
        result = _compute_filter_inclusion(self._scenarios(), ['ret_a'], None)
        assert result == {'ret_a', 'setup_x'}

    def test_groups_pulls_in_dependencies(self) -> None:
        from memex_eval.suite.runner import _compute_filter_inclusion

        # group='retrieval' contains ret_a (depends on setup_x) and ret_b.
        # The closure must include setup_x even though it's in 'infra'.
        result = _compute_filter_inclusion(self._scenarios(), None, ['retrieval'])
        assert result == {'ret_a', 'ret_b', 'setup_x'}

    def test_intersection_when_both_filters_set(self) -> None:
        from memex_eval.suite.runner import _compute_filter_inclusion

        # scenario_ids=['ret_a','ret_b'] (closure: {ret_a, ret_b, setup_x})
        # groups=['retrieval']           (closure: {ret_a, ret_b, setup_x})
        # → intersection has all three.
        result = _compute_filter_inclusion(self._scenarios(), ['ret_a', 'ret_b'], ['retrieval'])
        assert result == {'ret_a', 'ret_b', 'setup_x'}

        # scenario_ids=['ext_c']     (closure: {ext_c})
        # groups=['retrieval']       (closure: {ret_a, ret_b, setup_x})
        # → intersection is empty.
        result = _compute_filter_inclusion(self._scenarios(), ['ext_c'], ['retrieval'])
        assert result == set()

    def test_unknown_scenario_id_raises(self) -> None:
        from memex_eval.suite.runner import _compute_filter_inclusion

        with pytest.raises(ValueError, match='Unknown scenario id'):
            _compute_filter_inclusion(self._scenarios(), ['no_such_id'], None)

    def test_unknown_group_raises(self) -> None:
        from memex_eval.suite.runner import _compute_filter_inclusion

        with pytest.raises(ValueError, match='Unknown group'):
            _compute_filter_inclusion(self._scenarios(), None, ['no_such_group'])


class TestGroupFilter:
    def test_groups_field_filters_scenarios(self) -> None:
        s = Suite(metadata=_meta(name='grp_filter'))
        s.register(
            id='a',
            query='Q',
            group='retrieval',
            expected=KeywordsPresent(type='keywords_present', keywords=['x']),
        )
        s.register(
            id='b',
            query='Q',
            group='extraction',
            expected=KeywordsPresent(type='keywords_present', keywords=['x']),
        )
        s.register(
            id='c',
            query='Q',
            group='retrieval',
            expected=KeywordsAbsent(type='keywords_absent', keywords=['y']),
        )
        built = s.build()
        # Pick scenarios in group 'retrieval'.
        in_group = [sc for sc in built.scenarios if sc.group == 'retrieval']
        assert {sc.id for sc in in_group} == {'a', 'c'}


# ---------------------------------------------------------------------------
# Runner-level lifecycle dispatch
# ---------------------------------------------------------------------------


class TestRunnerDispatchesLifecycle:
    """Drive ``_execute_scenario`` directly with a mocked backend and a
    BaseScenario subclass; assert the runner calls each lifecycle method
    (setup → act → evaluate → teardown) and that the order is correct.

    Each lifecycle method appends to a shared call log.
    """

    @pytest.mark.asyncio
    async def test_lifecycle_methods_invoked_in_order(self) -> None:
        from unittest.mock import AsyncMock, MagicMock
        from uuid import uuid4

        from memex_eval.suite.agents import (
            AnswerBackend,
            replace_backend,
        )
        from memex_eval.suite.base import Suite as LegacySuite
        from memex_eval.suite.runner import _execute_scenario

        call_log: list[str] = []

        class _StubBackend(AnswerBackend):
            name = 'stub-dispatch'

            async def answer(self, scenario, *, api, vault_id, server_url, judge=None):
                call_log.append('backend.answer')
                return AgentAnswer(backend_name=self.name, answer_text='backend-produced-answer')

        # Register under a unique name so we can override it between tests.
        replace_backend('stub-dispatch')(_StubBackend)

        class _Lifecycle(BaseScenario):
            id: ClassVar[str] = 'lc_dispatch'
            query: ClassVar[str] = 'Q'
            answer_mode: ClassVar[str] = 'stub-dispatch'

            async def setup(self, ctx):
                call_log.append('instance.setup')

            async def act(self, ctx):
                call_log.append('instance.act')
                # Don't replace ctx.answer; the runner keeps the backend's.

            async def evaluate(self, ctx):
                call_log.append('instance.evaluate')
                # Replace declarative score (none set) — force pass.
                ctx.metrics['pass'] = 1.0

            async def teardown(self, ctx):
                call_log.append('instance.teardown')

        suite_inst = Suite(metadata=_meta(name='lc_runner'))
        suite_inst.register_class(_Lifecycle)
        legacy_suite: LegacySuite = suite_inst.build()
        scenario = legacy_suite.scenarios[0]

        api = MagicMock()
        # Methods touched by the runner that aren't relevant here.
        api.list_assets = AsyncMock(return_value=[])

        outcome = await _execute_scenario(
            api=api,
            server_url='http://localhost/api/v1/',
            vault_id=uuid4(),
            scenario=scenario,
            suite=legacy_suite,
            judge=None,
            note_key_to_unit_ids={},
            note_id_by_key={},
            replicate_index=0,
            backend_cache=None,
        )

        assert outcome.status == 'pass'
        assert outcome.metrics == {'pass': 1.0}
        # The runner must hit setup → backend → act → evaluate → teardown
        # in that order. (backend is the runner's call to backend.answer;
        # act is the BaseScenario hook AFTER backend.)
        assert call_log == [
            'instance.setup',
            'backend.answer',
            'instance.act',
            'instance.evaluate',
            'instance.teardown',
        ]

    @pytest.mark.asyncio
    async def test_teardown_runs_even_when_evaluate_raises(self) -> None:
        """An exception in ``evaluate`` must not prevent ``teardown`` from
        running. The scenario reports status='error' but the cleanup still
        fires."""
        from unittest.mock import AsyncMock, MagicMock
        from uuid import uuid4

        from memex_eval.suite.agents import AnswerBackend, replace_backend
        from memex_eval.suite.runner import _execute_scenario

        call_log: list[str] = []

        class _Stub(AnswerBackend):
            name = 'stub-eval-raises'

            async def answer(self, scenario, *, api, vault_id, server_url, judge=None):
                return AgentAnswer(backend_name=self.name)

        replace_backend('stub-eval-raises')(_Stub)

        class _Crashy(BaseScenario):
            id: ClassVar[str] = 'lc_crashy'
            query: ClassVar[str] = 'Q'
            answer_mode: ClassVar[str] = 'stub-eval-raises'

            async def evaluate(self, ctx):
                call_log.append('evaluate-pre-raise')
                raise RuntimeError('evaluate exploded')

            async def teardown(self, ctx):
                call_log.append('teardown-ran')

        suite_inst = Suite(metadata=_meta(name='lc_crashy_suite'))
        suite_inst.register_class(_Crashy)
        legacy_suite = suite_inst.build()
        scenario = legacy_suite.scenarios[0]

        api = MagicMock()
        api.list_assets = AsyncMock(return_value=[])

        outcome = await _execute_scenario(
            api=api,
            server_url='http://localhost/api/v1/',
            vault_id=uuid4(),
            scenario=scenario,
            suite=legacy_suite,
            judge=None,
            note_key_to_unit_ids={},
            note_id_by_key={},
            replicate_index=0,
            backend_cache=None,
        )
        assert outcome.status == 'error'
        # Teardown ran even though evaluate raised.
        assert 'teardown-ran' in call_log
        assert 'evaluate-pre-raise' in call_log

    @pytest.mark.asyncio
    async def test_act_can_replace_answer(self) -> None:
        """``BaseScenario.act`` can mutate ``ctx.answer`` — the runner must
        honor the replacement when scoring."""
        from unittest.mock import AsyncMock, MagicMock
        from uuid import uuid4

        from memex_eval.suite.agents import AnswerBackend, replace_backend
        from memex_eval.suite.runner import _execute_scenario

        class _StubBackend(AnswerBackend):
            name = 'stub-act-replace'

            async def answer(self, scenario, *, api, vault_id, server_url, judge=None):
                return AgentAnswer(backend_name=self.name, answer_text='ORIGINAL')

        replace_backend('stub-act-replace')(_StubBackend)

        class _ReplaceAnswer(BaseScenario):
            id: ClassVar[str] = 'lc_act_replace'
            query: ClassVar[str] = 'Q'
            answer_mode: ClassVar[str] = 'stub-act-replace'
            expected: ClassVar[Any] = KeywordsPresent(
                type='keywords_present', keywords=['REPLACED']
            )

            async def act(self, ctx):
                # Replace the answer with one that contains the keyword.
                ctx.answer = AgentAnswer(
                    backend_name='replaced',
                    answer_text='this contains REPLACED keyword',
                )

        suite_inst = Suite(metadata=_meta(name='lc_act_replace'))
        suite_inst.register_class(_ReplaceAnswer)
        legacy_suite = suite_inst.build()
        scenario = legacy_suite.scenarios[0]

        api = MagicMock()
        api.list_assets = AsyncMock(return_value=[])

        outcome = await _execute_scenario(
            api=api,
            server_url='http://localhost/api/v1/',
            vault_id=uuid4(),
            scenario=scenario,
            suite=legacy_suite,
            judge=None,
            note_key_to_unit_ids={},
            note_id_by_key={},
            replicate_index=0,
            backend_cache=None,
        )
        # If the replacement was honored, KeywordsPresent finds REPLACED → pass.
        assert outcome.status == 'pass'


# ---------------------------------------------------------------------------
# 8. Round-1 review fixes
# ---------------------------------------------------------------------------


class TestMutableDefaultsIsolation:
    """CRITICAL #3: every BaseScenario subclass gets its own deep-copied
    list for setup_actions / inline_notes / depends_on_prior_scenarios /
    expected_failure_modes — no shared mutable state across the class
    hierarchy."""

    def test_subclass_does_not_share_setup_actions_with_baseclass(self) -> None:
        class A(BaseScenario):
            id: ClassVar[str] = 'a'
            query: ClassVar[str] = 'Q'

        class B(BaseScenario):
            id: ClassVar[str] = 'b'
            query: ClassVar[str] = 'Q'

        # Mutate A's list in place.
        A.setup_actions.append(SetupAction(kind='record_outcome'))  # type: ignore[arg-type]
        # B and BaseScenario must not see A's mutation.
        assert B.setup_actions == []
        assert BaseScenario.setup_actions == []

    def test_subclass_does_not_share_depends_on_prior_with_baseclass(self) -> None:
        class A(BaseScenario):
            id: ClassVar[str] = 'a'
            query: ClassVar[str] = 'Q'

        class B(BaseScenario):
            id: ClassVar[str] = 'b'
            query: ClassVar[str] = 'Q'

        A.depends_on_prior_scenarios.append('side_effect')
        assert B.depends_on_prior_scenarios == []
        assert BaseScenario.depends_on_prior_scenarios == []


class TestRegisterClassWithInstance:
    """HIGH #1: register_class accepts a pre-instantiated BaseScenario so
    users can parameterize scenarios via __init__."""

    def test_accepts_pre_instantiated_object(self) -> None:
        class Param(BaseScenario):
            id: ClassVar[str] = 'param_a'
            query: ClassVar[str] = 'Q'
            expected: ClassVar[Any] = KeywordsPresent(type='keywords_present', keywords=['x'])

            def __init__(self, top_k: int = 10) -> None:
                # Per-instance top_k override (data-field shadowing).
                self.top_k = top_k

        s = Suite(metadata=_meta(name='cls_inst'))
        instance = Param(top_k=42)
        returned = s.register_class(instance)
        assert returned is instance
        sc = s.build().scenarios[0]
        assert sc.top_k == 42

    def test_class_with_required_init_fails_with_clear_error(self) -> None:
        class NeedsArg(BaseScenario):
            id: ClassVar[str] = 'needs_arg'
            query: ClassVar[str] = 'Q'

            def __init__(self, *, required_arg: str) -> None:
                self.tag = required_arg

        s = Suite(metadata=_meta(name='cls_needs_arg'))
        with pytest.raises(TypeError, match='no-arg ``__init__``'):
            s.register_class(NeedsArg)

    def test_data_field_method_collision_rejected(self) -> None:
        """A subclass that defines ``def expected(self): ...`` collides
        with the data field of the same name. Reject with a clear
        breadcrumb instead of an opaque Pydantic error."""

        class C(BaseScenario):
            id: ClassVar[str] = 'collision_a'
            query: ClassVar[str] = 'Q'

            def expected(self):  # type: ignore[override]
                return KeywordsPresent(type='keywords_present', keywords=['x'])

        s = Suite(metadata=_meta(name='cls_collision'))
        with pytest.raises(TypeError, match='collides with the BaseScenario data field'):
            s.register_class(C)


class TestDescriptionFallback:
    """MEDIUM #7: when ``description`` is not set, fall back to the
    class docstring's first line, then to id."""

    def test_class_docstring_used_as_description(self) -> None:
        class C(BaseScenario):
            """A useful description for this scenario."""

            id: ClassVar[str] = 'desc_doc'
            query: ClassVar[str] = 'Q'
            expected: ClassVar[Any] = KeywordsPresent(type='keywords_present', keywords=['x'])

        s = Suite(metadata=_meta(name='cls_desc_doc'))
        s.register_class(C)
        sc = s.build().scenarios[0]
        assert sc.description == 'A useful description for this scenario.'

    def test_explicit_description_wins_over_docstring(self) -> None:
        class C(BaseScenario):
            """Docstring."""

            id: ClassVar[str] = 'desc_explicit'
            query: ClassVar[str] = 'Q'
            description: ClassVar[str] = 'Explicit description'
            expected: ClassVar[Any] = KeywordsPresent(type='keywords_present', keywords=['x'])

        s = Suite(metadata=_meta(name='cls_desc_explicit'))
        s.register_class(C)
        sc = s.build().scenarios[0]
        assert sc.description == 'Explicit description'


class TestDepOrderingAtRegistration:
    """HIGH #9: scenarios that name an unregistered dep must fail at
    registration (clean stack frame), not at suite build()."""

    def test_unknown_dep_at_registration_raises(self) -> None:
        s = Suite(metadata=_meta(name='dep_order'))
        with pytest.raises(ValueError, match='not been registered yet'):
            s.register(
                id='consumer',
                query='Q',
                expected=KeywordsPresent(type='keywords_present', keywords=['x']),
                depends_on_prior_scenarios=['ghost'],
            )

    def test_dep_in_order_succeeds(self) -> None:
        s = Suite(metadata=_meta(name='dep_order_ok'))
        s.register(
            id='producer',
            query='Q',
            expected=KeywordsPresent(type='keywords_present', keywords=['x']),
        )
        s.register(
            id='consumer',
            query='Q',
            expected=KeywordsPresent(type='keywords_present', keywords=['x']),
            depends_on_prior_scenarios=['producer'],
        )
        assert len(s.build().scenarios) == 2


class TestSidecarBaseScenarioRecovery:
    """HIGH #6: ``decorator.Suite.build()`` populates a sidecar
    ``_base_scenarios_by_id`` so dispatch survives Pydantic re-validation
    that strips the ``_base_scenario_instance`` extra attr."""

    def test_sidecar_populated_on_build(self) -> None:
        s = Suite(metadata=_meta(name='cls_sidecar'))

        @s.register_class
        class _C(BaseScenario):
            id: ClassVar[str] = 'sc_a'
            query: ClassVar[str] = 'Q'
            expected: ClassVar[Any] = KeywordsPresent(type='keywords_present', keywords=['x'])

        legacy = s.build()
        sidecar = getattr(legacy, '_base_scenarios_by_id', None)
        assert sidecar is not None
        assert 'sc_a' in sidecar
        assert isinstance(sidecar['sc_a'], _C)


class TestBaseScenarioEvaluateNoMetricsBreadcrumb:
    """MEDIUM #1: when BaseScenario.evaluate produces no metrics AND
    ``self.expected`` is None (user forgot to override evaluate), the
    runner records status='fail' with a breadcrumb in ``error``."""

    @pytest.mark.asyncio
    async def test_breadcrumb_when_no_metrics_emitted(self) -> None:
        from unittest.mock import AsyncMock, MagicMock
        from uuid import uuid4

        from memex_eval.suite.agents import AnswerBackend, replace_backend
        from memex_eval.suite.runner import _execute_scenario

        class _StubBackend(AnswerBackend):
            name = 'stub-no-metrics'

            async def answer(self, scenario, *, api, vault_id, server_url, judge=None):
                return AgentAnswer(backend_name='stub', answer_text='whatever')

        replace_backend('stub-no-metrics')(_StubBackend)

        class _Forgot(BaseScenario):
            """User forgot to override evaluate or set expected."""

            id: ClassVar[str] = 'forgot_a'
            query: ClassVar[str] = 'Q'
            answer_mode: ClassVar[str | None] = 'stub-no-metrics'

        s = Suite(metadata=_meta(name='cls_forgot'))
        s.register_class(_Forgot)
        legacy = s.build()
        scenario = legacy.scenarios[0]

        api = MagicMock()
        api.list_assets = AsyncMock(return_value=[])
        outcome = await _execute_scenario(
            api=api,
            server_url='http://localhost/api/v1/',
            vault_id=uuid4(),
            scenario=scenario,
            suite=legacy,
            judge=None,
            note_key_to_unit_ids={},
            note_id_by_key={},
            replicate_index=0,
            backend_cache=None,
        )
        assert outcome.status == 'fail', f'got status={outcome.status} error={outcome.error}'
        assert outcome.error is not None
        assert 'produced no metrics' in outcome.error


class TestAssertionErrorInRunnerMapsToFail:
    """The runner's outer except now catches AssertionError and records
    ``status='fail'`` (not ``'error'``). The user's MLflow trace gets
    the assertion in ``count.failed``, not ``count.errored``."""

    @pytest.mark.asyncio
    async def test_assertion_in_evaluate_records_fail_not_error(self) -> None:
        from unittest.mock import AsyncMock, MagicMock
        from uuid import uuid4

        from memex_eval.suite.agents import AnswerBackend, replace_backend
        from memex_eval.suite.runner import _execute_scenario

        class _StubBackend(AnswerBackend):
            name = 'stub-assert-fail'

            async def answer(self, scenario, *, api, vault_id, server_url, judge=None):
                return AgentAnswer(backend_name='stub', answer_text='nope')

        replace_backend('stub-assert-fail')(_StubBackend)

        class _AssertNope(BaseScenario):
            id: ClassVar[str] = 'assert_a'
            query: ClassVar[str] = 'Q'
            answer_mode: ClassVar[str | None] = 'stub-assert-fail'

            async def evaluate(self, ctx):
                assert 'YES' in (ctx.answer.answer_text or ''), 'no YES in answer'

        s = Suite(metadata=_meta(name='cls_assert'))
        s.register_class(_AssertNope)
        legacy = s.build()
        scenario = legacy.scenarios[0]

        api = MagicMock()
        api.list_assets = AsyncMock(return_value=[])
        outcome = await _execute_scenario(
            api=api,
            server_url='http://localhost/api/v1/',
            vault_id=uuid4(),
            scenario=scenario,
            suite=legacy,
            judge=None,
            note_key_to_unit_ids={},
            note_id_by_key={},
            replicate_index=0,
            backend_cache=None,
        )
        assert outcome.status == 'fail'
        assert outcome.metrics == {'pass': 0.0}
        # pytest's assertion rewriter may expand the message; just check
        # the prefix is right.
        assert outcome.error is not None
        assert outcome.error.startswith('AssertionError: ')
        assert 'no YES in answer' in outcome.error


class TestTeardownGatedOnSetupRan:
    """HIGH #7: BaseScenario.teardown only runs if BaseScenario.setup
    actually ran (returned without raising). A setup that raises must
    NOT trigger teardown observing un-initialized state — that's the
    classic asymmetric-lifecycle bug."""

    @pytest.mark.asyncio
    async def test_teardown_skipped_when_setup_raises(self) -> None:
        from unittest.mock import AsyncMock, MagicMock
        from uuid import uuid4

        from memex_eval.suite.agents import AnswerBackend, replace_backend
        from memex_eval.suite.runner import _execute_scenario

        class _StubBackend(AnswerBackend):
            name = 'stub-td-gated'

            async def answer(self, scenario, *, api, vault_id, server_url, judge=None):
                return AgentAnswer(backend_name='stub', answer_text='ok')

        replace_backend('stub-td-gated')(_StubBackend)

        lifecycle: list[str] = []

        class _C(BaseScenario):
            id: ClassVar[str] = 'td_gated_a'
            query: ClassVar[str] = 'Q'
            answer_mode: ClassVar[str | None] = 'stub-td-gated'

            async def setup(self, ctx):
                lifecycle.append('setup_started')
                raise RuntimeError('setup blew up before init')

            async def teardown(self, ctx):
                # Should NEVER run because setup raised.
                lifecycle.append('teardown_ran')

        s = Suite(metadata=_meta(name='cls_td_gated'))
        s.register_class(_C)
        legacy = s.build()
        scenario = legacy.scenarios[0]

        api = MagicMock()
        api.list_assets = AsyncMock(return_value=[])
        outcome = await _execute_scenario(
            api=api,
            server_url='http://localhost/api/v1/',
            vault_id=uuid4(),
            scenario=scenario,
            suite=legacy,
            judge=None,
            note_key_to_unit_ids={},
            note_id_by_key={},
            replicate_index=0,
            backend_cache=None,
        )
        assert outcome.status == 'error'
        assert lifecycle == ['setup_started']  # teardown did NOT fire


class TestGroupFilterValidatesPreVault:
    """MEDIUM #10: typoed --group raises BEFORE any vault setup.
    Validated via direct ``_compute_filter_inclusion`` call."""

    def test_unknown_group_raises_with_helpful_message(self) -> None:
        from memex_eval.suite.runner import _compute_filter_inclusion

        s = Suite(metadata=_meta(name='filter_validate'))
        s.register(
            id='a',
            query='Q',
            group='extraction',
            expected=KeywordsPresent(type='keywords_present', keywords=['x']),
        )
        legacy = s.build()
        with pytest.raises(ValueError, match='Unknown group'):
            _compute_filter_inclusion(legacy.scenarios, None, ['typo'])

    def test_no_groups_in_suite_raises_helpful_message(self) -> None:
        from memex_eval.suite.runner import _compute_filter_inclusion

        s = Suite(metadata=_meta(name='no_groups'))
        s.register(
            id='a',
            query='Q',
            expected=KeywordsPresent(type='keywords_present', keywords=['x']),
        )
        legacy = s.build()
        with pytest.raises(ValueError, match='No scenarios in this suite declare a ``group``'):
            _compute_filter_inclusion(legacy.scenarios, None, ['anything'])


class TestAsyncSuiteScenarioInRunner:
    """CRITICAL #1: ``@suite.scenario`` with an ``async def`` body must
    work end-to-end through the runner — the runner detects
    CustomEvaluate-with-async-fn and awaits directly instead of going
    through sync ``score()`` (which raises inside a running loop)."""

    @pytest.mark.asyncio
    async def test_async_fn_routed_through_async_dispatch(self) -> None:
        from unittest.mock import AsyncMock, MagicMock
        from uuid import uuid4

        from memex_eval.suite.agents import AnswerBackend, replace_backend
        from memex_eval.suite.runner import _execute_scenario

        class _StubBackend(AnswerBackend):
            name = 'stub-async-deco'

            async def answer(self, scenario, *, api, vault_id, server_url, judge=None):
                return AgentAnswer(backend_name='stub', answer_text='hello')

        replace_backend('stub-async-deco')(_StubBackend)

        s = Suite(metadata=_meta(name='dec_async_runner'))

        @s.scenario(id='async_runner_a', query='Q', answer_mode='stub-async-deco')
        async def _eval(ctx: ScenarioContext) -> None:
            # Just to prove this is the async path, await something trivial.
            import asyncio

            await asyncio.sleep(0)
            ctx.metrics['pass'] = 1.0

        legacy = s.build()
        scenario = legacy.scenarios[0]

        api = MagicMock()
        api.list_assets = AsyncMock(return_value=[])
        outcome = await _execute_scenario(
            api=api,
            server_url='http://localhost/api/v1/',
            vault_id=uuid4(),
            scenario=scenario,
            suite=legacy,
            judge=None,
            note_key_to_unit_ids={},
            note_id_by_key={},
            replicate_index=0,
            backend_cache=None,
        )
        assert outcome.status == 'pass'
        assert outcome.metrics == {'pass': 1.0}

    @pytest.mark.asyncio
    async def test_async_fn_assertion_records_fail(self) -> None:
        from unittest.mock import AsyncMock, MagicMock
        from uuid import uuid4

        from memex_eval.suite.agents import AnswerBackend, replace_backend
        from memex_eval.suite.runner import _execute_scenario

        class _StubBackend(AnswerBackend):
            name = 'stub-async-assert'

            async def answer(self, scenario, *, api, vault_id, server_url, judge=None):
                return AgentAnswer(backend_name='stub', answer_text='nope')

        replace_backend('stub-async-assert')(_StubBackend)

        s = Suite(metadata=_meta(name='dec_async_assert'))

        @s.scenario(id='async_assert_a', query='Q', answer_mode='stub-async-assert')
        async def _eval(ctx: ScenarioContext) -> None:
            assert 'YES' in (ctx.answer.answer_text or ''), 'expected YES'

        legacy = s.build()
        scenario = legacy.scenarios[0]

        api = MagicMock()
        api.list_assets = AsyncMock(return_value=[])
        outcome = await _execute_scenario(
            api=api,
            server_url='http://localhost/api/v1/',
            vault_id=uuid4(),
            scenario=scenario,
            suite=legacy,
            judge=None,
            note_key_to_unit_ids={},
            note_id_by_key={},
            replicate_index=0,
            backend_cache=None,
        )
        assert outcome.status == 'fail'
        assert outcome.error is not None
        assert outcome.error.startswith('AssertionError: ')
        assert 'expected YES' in outcome.error
        assert outcome.metrics == {'pass': 0.0}


# ---------------------------------------------------------------------------
# 9. Round-2 review fixes
# ---------------------------------------------------------------------------


class TestAssertionContractByPhase:
    """Round-2 HIGH-2: AssertionError mapping is phase-sensitive.
    Evaluator-phase asserts → status='fail'. Setup / act / backend
    asserts → status='error' (those are infrastructure phases, not
    eval verdicts). Verifies the narrow inner ``except AssertionError``
    only wraps the evaluator call sites."""

    @pytest.mark.asyncio
    async def test_setup_assertion_records_error_not_fail(self) -> None:
        from unittest.mock import AsyncMock, MagicMock
        from uuid import uuid4

        from memex_eval.suite.agents import AnswerBackend, replace_backend
        from memex_eval.suite.runner import _execute_scenario

        class _StubBackend(AnswerBackend):
            name = 'stub-setup-assert'

            async def answer(self, scenario, *, api, vault_id, server_url, judge=None):
                return AgentAnswer(backend_name='stub', answer_text='ok')

        replace_backend('stub-setup-assert')(_StubBackend)

        class _C(BaseScenario):
            id: ClassVar[str] = 'setup_assert_a'
            query: ClassVar[str] = 'Q'
            answer_mode: ClassVar[str | None] = 'stub-setup-assert'

            async def setup(self, ctx):
                # An assert in setup is an infrastructure precondition,
                # not an eval verdict — must surface as 'error'.
                assert False, 'setup precondition violated'

        s = Suite(metadata=_meta(name='cls_setup_assert'))
        s.register_class(_C)
        legacy = s.build()
        scenario = legacy.scenarios[0]

        api = MagicMock()
        api.list_assets = AsyncMock(return_value=[])
        outcome = await _execute_scenario(
            api=api,
            server_url='http://localhost/api/v1/',
            vault_id=uuid4(),
            scenario=scenario,
            suite=legacy,
            judge=None,
            note_key_to_unit_ids={},
            note_id_by_key={},
            replicate_index=0,
            backend_cache=None,
        )
        assert outcome.status == 'error'
        assert outcome.error is not None
        assert 'AssertionError' in outcome.error

    @pytest.mark.asyncio
    async def test_act_assertion_records_error_not_fail(self) -> None:
        from unittest.mock import AsyncMock, MagicMock
        from uuid import uuid4

        from memex_eval.suite.agents import AnswerBackend, replace_backend
        from memex_eval.suite.runner import _execute_scenario

        class _StubBackend(AnswerBackend):
            name = 'stub-act-assert'

            async def answer(self, scenario, *, api, vault_id, server_url, judge=None):
                return AgentAnswer(backend_name='stub', answer_text='ok')

        replace_backend('stub-act-assert')(_StubBackend)

        class _C(BaseScenario):
            id: ClassVar[str] = 'act_assert_a'
            query: ClassVar[str] = 'Q'
            answer_mode: ClassVar[str | None] = 'stub-act-assert'

            async def act(self, ctx):
                # Asserts during the multi-step retrieval phase are still
                # infrastructure (couldn't fetch a precondition), not the
                # eval's own verdict.
                assert False, 'act phase blew up'

        s = Suite(metadata=_meta(name='cls_act_assert'))
        s.register_class(_C)
        legacy = s.build()
        scenario = legacy.scenarios[0]

        api = MagicMock()
        api.list_assets = AsyncMock(return_value=[])
        outcome = await _execute_scenario(
            api=api,
            server_url='http://localhost/api/v1/',
            vault_id=uuid4(),
            scenario=scenario,
            suite=legacy,
            judge=None,
            note_key_to_unit_ids={},
            note_id_by_key={},
            replicate_index=0,
            backend_cache=None,
        )
        assert outcome.status == 'error'
        assert outcome.error is not None
        assert 'AssertionError' in outcome.error


class TestRegisterClassWithPerInstanceId:
    """Round-2 HIGH-1: when a pre-instantiated BaseScenario sets ``id``
    or ``query`` per-instance via ``__init__``, the validator must
    consult the instance, not just the class MRO."""

    def test_per_instance_id_accepted(self) -> None:
        class Param(BaseScenario):
            query: ClassVar[str] = 'Q'
            expected: ClassVar[Any] = KeywordsPresent(type='keywords_present', keywords=['x'])

            def __init__(self, *, custom_id: str) -> None:
                self.id = custom_id

        s = Suite(metadata=_meta(name='cls_per_inst_id'))
        s.register_class(Param(custom_id='param_per_inst_a'))
        sc = s.build().scenarios[0]
        assert sc.id == 'param_per_inst_a'


class TestSidecarRecoveryAfterRevalidation:
    """Round-2 MEDIUM-4: the sidecar must be USED by the runner when
    ``Scenario._base_scenario_instance`` has been stripped (e.g. by
    ``Scenario.model_validate(sc.model_dump())``)."""

    @pytest.mark.asyncio
    async def test_runner_uses_sidecar_after_revalidation_strips_attr(self) -> None:
        from unittest.mock import AsyncMock, MagicMock
        from uuid import uuid4

        from memex_eval.suite.agents import AnswerBackend, replace_backend
        from memex_eval.suite.runner import _execute_scenario

        class _StubBackend(AnswerBackend):
            name = 'stub-sidecar-roundtrip'

            async def answer(self, scenario, *, api, vault_id, server_url, judge=None):
                return AgentAnswer(backend_name='stub', answer_text='hello')

        replace_backend('stub-sidecar-roundtrip')(_StubBackend)

        evaluate_calls: list[str] = []

        class _C(BaseScenario):
            id: ClassVar[str] = 'sidecar_a'
            query: ClassVar[str] = 'Q'
            answer_mode: ClassVar[str | None] = 'stub-sidecar-roundtrip'

            async def evaluate(self, ctx):
                evaluate_calls.append('ran')
                ctx.metrics['pass'] = 1.0

        s = Suite(metadata=_meta(name='cls_sidecar_recovery'))
        s.register_class(_C)
        legacy = s.build()

        # Simulate a Pydantic flow that drops the stashed attr: take a
        # fresh ``model_copy`` and explicitly delete the extra attr the
        # decorator put there via ``object.__setattr__``. ``model_copy``
        # preserves Pydantic fields but not arbitrary extras. Reattach
        # the cleaned scenario in the suite's scenarios list.
        original = legacy.scenarios[0]
        cleaned = original.model_copy(deep=False)
        # Force-strip the bypassed attr so the runner has to fall back
        # to the sidecar lookup.
        try:
            object.__delattr__(cleaned, '_base_scenario_instance')
        except AttributeError:
            pass
        assert getattr(cleaned, '_base_scenario_instance', None) is None
        legacy.scenarios[0] = cleaned
        scenario = legacy.scenarios[0]

        api = MagicMock()
        api.list_assets = AsyncMock(return_value=[])
        outcome = await _execute_scenario(
            api=api,
            server_url='http://localhost/api/v1/',
            vault_id=uuid4(),
            scenario=scenario,
            suite=legacy,
            judge=None,
            note_key_to_unit_ids={},
            note_id_by_key={},
            replicate_index=0,
            backend_cache=None,
        )
        # The runner recovered the BaseScenario instance from the sidecar
        # (since the stashed attr was stripped) and dispatched evaluate.
        assert evaluate_calls == ['ran']
        assert outcome.status == 'pass'


class TestAsyncCallableInstanceDetection:
    """Round-2 MEDIUM-1: ``_custom_eval_has_async_fn`` must detect
    callable instances whose ``__call__`` is ``async def``, not just
    plain ``async def`` functions."""

    def test_async_callable_instance_detected(self) -> None:
        from memex_eval.suite.runner import _custom_eval_has_async_fn

        class AsyncCallable:
            async def __call__(self, ctx):
                ctx.metrics['pass'] = 1.0

        ac = AsyncCallable()
        outcome = CustomEvaluate(fn=ac)
        assert _custom_eval_has_async_fn(outcome) is True

    def test_sync_callable_instance_not_detected(self) -> None:
        from memex_eval.suite.runner import _custom_eval_has_async_fn

        class SyncCallable:
            def __call__(self, ctx):
                ctx.metrics['pass'] = 1.0

        sc = SyncCallable()
        outcome = CustomEvaluate(fn=sc)
        assert _custom_eval_has_async_fn(outcome) is False


class TestPartialMetricsPreservedOnAssertion:
    """Round-3 MEDIUM-2: an evaluator that populates metrics BEFORE
    asserting must NOT lose them when the assertion fires. The fail
    record carries the partial gradient signal so longitudinal tracking
    can spot regressions even on failed runs."""

    def test_sync_score_preserves_partial_metrics_on_assertion(self) -> None:
        s = Suite(metadata=_meta(name='dec_partial'))

        @s.scenario(id='partial_a', query='Q')
        def _eval(ctx: ScenarioContext) -> None:
            ctx.metrics['recall'] = 0.6
            ctx.metrics['mrr'] = 0.4
            assert ctx.metrics['recall'] >= 0.8, 'recall too low'

        sc = s.build().scenarios[0]
        result = sc.expected.score(AgentAnswer(backend_name='test'), sc, context={})
        # Partial metrics preserved; pass=0.0 overlaid.
        assert result == {'recall': 0.6, 'mrr': 0.4, 'pass': 0.0}

    @pytest.mark.asyncio
    async def test_class_based_evaluate_preserves_partial_metrics(self) -> None:
        from unittest.mock import AsyncMock, MagicMock
        from uuid import uuid4

        from memex_eval.suite.agents import AnswerBackend, replace_backend
        from memex_eval.suite.runner import _execute_scenario

        class _StubBackend(AnswerBackend):
            name = 'stub-partial-class'

            async def answer(self, scenario, *, api, vault_id, server_url, judge=None):
                return AgentAnswer(backend_name='stub', answer_text='whatever')

        replace_backend('stub-partial-class')(_StubBackend)

        class _C(BaseScenario):
            id: ClassVar[str] = 'partial_class_a'
            query: ClassVar[str] = 'Q'
            answer_mode: ClassVar[str | None] = 'stub-partial-class'

            async def evaluate(self, ctx):
                ctx.metrics['recall'] = 0.6
                ctx.metrics['mrr'] = 0.4
                assert ctx.metrics['recall'] >= 0.8, 'class evaluator regression'

        s = Suite(metadata=_meta(name='cls_partial'))
        s.register_class(_C)
        legacy = s.build()
        scenario = legacy.scenarios[0]

        api = MagicMock()
        api.list_assets = AsyncMock(return_value=[])
        outcome = await _execute_scenario(
            api=api,
            server_url='http://localhost/api/v1/',
            vault_id=uuid4(),
            scenario=scenario,
            suite=legacy,
            judge=None,
            note_key_to_unit_ids={},
            note_id_by_key={},
            replicate_index=0,
            backend_cache=None,
        )
        assert outcome.status == 'fail'
        # The recall=0.6 / mrr=0.4 gradient signal must survive in the
        # outcome metrics so MLflow can track regression vs prior runs.
        assert outcome.metrics == {'recall': 0.6, 'mrr': 0.4, 'pass': 0.0}

    @pytest.mark.asyncio
    async def test_async_decorator_preserves_partial_metrics(self) -> None:
        from unittest.mock import AsyncMock, MagicMock
        from uuid import uuid4

        from memex_eval.suite.agents import AnswerBackend, replace_backend
        from memex_eval.suite.runner import _execute_scenario

        class _StubBackend(AnswerBackend):
            name = 'stub-partial-async'

            async def answer(self, scenario, *, api, vault_id, server_url, judge=None):
                return AgentAnswer(backend_name='stub', answer_text='whatever')

        replace_backend('stub-partial-async')(_StubBackend)

        s = Suite(metadata=_meta(name='dec_partial_async'))

        @s.scenario(id='partial_async_a', query='Q', answer_mode='stub-partial-async')
        async def _eval(ctx: ScenarioContext) -> None:
            import asyncio

            await asyncio.sleep(0)
            ctx.metrics['recall'] = 0.7
            ctx.metrics['ndcg'] = 0.5
            assert ctx.metrics['ndcg'] >= 0.9, 'ndcg below threshold'

        legacy = s.build()
        scenario = legacy.scenarios[0]

        api = MagicMock()
        api.list_assets = AsyncMock(return_value=[])
        outcome = await _execute_scenario(
            api=api,
            server_url='http://localhost/api/v1/',
            vault_id=uuid4(),
            scenario=scenario,
            suite=legacy,
            judge=None,
            note_key_to_unit_ids={},
            note_id_by_key={},
            replicate_index=0,
            backend_cache=None,
        )
        assert outcome.status == 'fail'
        # async @suite.scenario routes through _invoke_custom_evaluate_async
        # which carries partial metrics on exc.partial_metrics; the runner's
        # outer AssertionError handler recovers them onto the outcome.
        assert outcome.metrics == {'recall': 0.7, 'ndcg': 0.5, 'pass': 0.0}


class TestNonNumericMetricsCoerced:
    """Round-4 MEDIUM-2: evaluators that stick a string breadcrumb into
    ``ctx.metrics`` (a natural debugging instinct) must NOT crash
    ScenarioOutcome construction. Non-numeric values are silently
    dropped (with a warning log) so the verdict survives."""

    def test_sync_score_drops_string_value_on_assertion(self) -> None:
        s = Suite(metadata=_meta(name='dec_nonnumeric_assert'))

        @s.scenario(id='nn_a', query='Q')
        def _eval(ctx: ScenarioContext) -> None:
            ctx.metrics['recall'] = 0.6
            ctx.metrics['note'] = 'recall too low'  # non-numeric breadcrumb
            assert ctx.metrics['recall'] >= 0.8

        sc = s.build().scenarios[0]
        result = sc.expected.score(AgentAnswer(backend_name='test'), sc, context={})
        # 'note' is dropped; 'recall' preserved; 'pass' overlaid.
        assert result == {'recall': 0.6, 'pass': 0.0}

    def test_sync_score_drops_string_value_on_success(self) -> None:
        s = Suite(metadata=_meta(name='dec_nonnumeric_pass'))

        @s.scenario(id='nn_b', query='Q')
        def _eval(ctx: ScenarioContext) -> None:
            ctx.metrics['recall'] = 0.9
            ctx.metrics['note'] = 'all good'
            ctx.metrics['pass'] = 1.0

        sc = s.build().scenarios[0]
        result = sc.expected.score(AgentAnswer(backend_name='test'), sc, context={})
        assert result == {'recall': 0.9, 'pass': 1.0}

    @pytest.mark.asyncio
    async def test_class_evaluate_drops_string_value_on_assertion(self) -> None:
        from unittest.mock import AsyncMock, MagicMock
        from uuid import uuid4

        from memex_eval.suite.agents import AnswerBackend, replace_backend
        from memex_eval.suite.runner import _execute_scenario

        class _StubBackend(AnswerBackend):
            name = 'stub-nn-class'

            async def answer(self, scenario, *, api, vault_id, server_url, judge=None):
                return AgentAnswer(backend_name='stub', answer_text='whatever')

        replace_backend('stub-nn-class')(_StubBackend)

        class _C(BaseScenario):
            id: ClassVar[str] = 'nn_class_a'
            query: ClassVar[str] = 'Q'
            answer_mode: ClassVar[str | None] = 'stub-nn-class'

            async def evaluate(self, ctx):
                ctx.metrics['recall'] = 0.6
                ctx.metrics['note'] = 'fyi'  # non-numeric
                assert ctx.metrics['recall'] >= 0.8

        s = Suite(metadata=_meta(name='cls_nn'))
        s.register_class(_C)
        legacy = s.build()
        scenario = legacy.scenarios[0]

        api = MagicMock()
        api.list_assets = AsyncMock(return_value=[])
        outcome = await _execute_scenario(
            api=api,
            server_url='http://localhost/api/v1/',
            vault_id=uuid4(),
            scenario=scenario,
            suite=legacy,
            judge=None,
            note_key_to_unit_ids={},
            note_id_by_key={},
            replicate_index=0,
            backend_cache=None,
        )
        assert outcome.status == 'fail'
        # Verdict survives despite the non-numeric value.
        assert outcome.metrics == {'recall': 0.6, 'pass': 0.0}

    @pytest.mark.asyncio
    async def test_legacy_outcome_path_coerces_non_numeric_metrics(self) -> None:
        """Round-5: a third-party ``register_outcome`` subclass that
        returns a dict with non-numeric values must not crash the
        ScenarioOutcome validator. The legacy ``expected.score()``
        path is also routed through ``_coerce_numeric_metrics``."""
        from unittest.mock import AsyncMock, MagicMock
        from typing import Literal as _Literal
        from uuid import uuid4

        from memex_eval.suite.agents import AnswerBackend, replace_backend
        from memex_eval.suite.base import ExpectedOutcomeBase, register_outcome
        from memex_eval.suite.runner import _execute_scenario

        class _StubBackend(AnswerBackend):
            name = 'stub-legacy-coerce'

            async def answer(self, scenario, *, api, vault_id, server_url, judge=None):
                return AgentAnswer(backend_name='stub', answer_text='ok')

        replace_backend('stub-legacy-coerce')(_StubBackend)

        @register_outcome('legacy_with_breadcrumb')
        class _LegacyOutcome(ExpectedOutcomeBase):
            type: _Literal['legacy_with_breadcrumb'] = 'legacy_with_breadcrumb'

            def score(
                self,
                answer,
                scenario,
                *,
                note_key_to_unit_ids=None,
                judge=None,
                context=None,
            ):
                # User-style return: numeric verdict + non-numeric note.
                return {'recall': 0.7, 'note': 'partial pass', 'pass': 1.0}

            def metric_keys(self, top_k=None):
                return ['recall', 'pass']

        s = Suite(metadata=_meta(name='legacy_coerce'))
        s.register(
            id='legacy_a',
            query='Q',
            answer_mode='stub-legacy-coerce',
            expected=_LegacyOutcome(type='legacy_with_breadcrumb'),
        )
        legacy = s.build()
        scenario = legacy.scenarios[0]

        api = MagicMock()
        api.list_assets = AsyncMock(return_value=[])
        outcome = await _execute_scenario(
            api=api,
            server_url='http://localhost/api/v1/',
            vault_id=uuid4(),
            scenario=scenario,
            suite=legacy,
            judge=None,
            note_key_to_unit_ids={},
            note_id_by_key={},
            replicate_index=0,
            backend_cache=None,
        )
        # Without coercion, ScenarioOutcome would raise ValidationError
        # and the verdict would be erased into status='error'.
        assert outcome.status == 'pass'
        assert outcome.metrics == {'recall': 0.7, 'pass': 1.0}
