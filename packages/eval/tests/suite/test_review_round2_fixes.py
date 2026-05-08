"""Regression tests for issues found in adversarial review round 2."""

from __future__ import annotations

from typing import Literal

import pytest

from memex_eval.suite import (
    AgentAnswer,
    CompositeOutcome,
    ExpectedOutcomeBase,
    Scenario,
    SetupAction,
    SetupActionHandler,
    register_outcome,
    register_setup_action,
)
from memex_eval.suite.runner import _build_notes_tag


class TestNotesTagBuilder:
    """H-1 + H-5: whitespace handling and false-suffix correctness."""

    def test_none_returns_empty(self) -> None:
        assert _build_notes_tag(None) == ''

    def test_empty_returns_empty(self) -> None:
        assert _build_notes_tag('') == ''

    def test_whitespace_only_returns_empty(self) -> None:
        # H-1: strip() then splitlines()[0] used to IndexError here.
        assert _build_notes_tag('   ') == ''
        assert _build_notes_tag('\n\n') == ''
        assert _build_notes_tag('\t  \n') == ''

    def test_single_short_line_no_suffix(self) -> None:
        # H-5: trailing newline alone must NOT trigger the artifact suffix.
        assert _build_notes_tag('Bumped alpha\n') == 'Bumped alpha'
        assert _build_notes_tag('  Bumped alpha  ') == 'Bumped alpha'

    def test_multi_line_appends_suffix(self) -> None:
        result = _build_notes_tag('Bumped alpha\nDetails on next line')
        assert result.startswith('Bumped alpha')
        assert result.endswith('… (see run_notes.md artifact)')

    def test_long_first_line_truncated_with_suffix(self) -> None:
        body = 'A' * 300
        result = _build_notes_tag(body)
        assert result.startswith('A' * 240)
        assert result.endswith('… (see run_notes.md artifact)')

    def test_short_first_line_with_long_remaining_body_appends_suffix(self) -> None:
        result = _build_notes_tag('Quick summary\nLine 2 with more detail')
        assert 'Quick summary' in result
        assert '… (see run_notes.md artifact)' in result


class TestCompositeFalsePassFix:
    """H-2: composite must not silently pass when child has no `pass` key."""

    def test_composite_with_failing_metric_only_child_fails(self) -> None:
        # GoldUnitIds returns metrics but no `pass` key. Pre-fix, the composite
        # treated this as passing; post-fix it correctly fails when all metrics
        # are zero.
        @register_outcome('test_metric_only')
        class MetricOnly(ExpectedOutcomeBase):
            type: Literal['test_metric_only']

            def score(self, answer, scenario, **_kw):
                return {'recall_at_5': 0.0}

            def metric_keys(self, top_k=None):
                return ['recall_at_5']

        comp = CompositeOutcome(
            type='composite',
            children=[MetricOnly(type='test_metric_only')],
        )
        scenario = Scenario(
            id='comp_check',
            description='d',
            query='q',
            expected=comp,
        )
        result = comp.score(AgentAnswer(units=[]), scenario)
        assert result['pass'] == 0.0

    def test_composite_with_passing_metric_only_child_passes(self) -> None:
        @register_outcome('test_metric_passes')
        class MetricPasses(ExpectedOutcomeBase):
            type: Literal['test_metric_passes']

            def score(self, answer, scenario, **_kw):
                return {'recall_at_5': 0.6}

            def metric_keys(self, top_k=None):
                return ['recall_at_5']

        comp = CompositeOutcome(
            type='composite',
            children=[MetricPasses(type='test_metric_passes')],
        )
        scenario = Scenario(
            id='comp_check2',
            description='d',
            query='q',
            expected=comp,
        )
        result = comp.score(AgentAnswer(units=[]), scenario)
        assert result['pass'] == 1.0


class TestNameValidation:
    """M-9 + round-3 M-R3-1/M-R3-2: every registry rejects invalid names."""

    def test_empty_setup_action_name_rejected(self) -> None:
        with pytest.raises(ValueError, match='must match'):

            @register_setup_action('')
            class _Empty(SetupActionHandler):
                async def run(self, api, vault_id, params):
                    return None

    def test_invalid_setup_action_name_rejected(self) -> None:
        with pytest.raises(ValueError, match='must match'):

            @register_setup_action('Invalid-Name')
            class _Bad(SetupActionHandler):
                async def run(self, api, vault_id, params):
                    return None

    def test_empty_outcome_name_rejected(self) -> None:
        with pytest.raises(ValueError, match='must match'):

            @register_outcome('')
            class _BadOutcome(ExpectedOutcomeBase):
                type: Literal['']

                def score(self, answer, scenario, **_kw):
                    return {}

    def test_invalid_outcome_name_rejected(self) -> None:
        with pytest.raises(ValueError, match='must match'):

            @register_outcome('Bad Name')
            class _BadOutcome(ExpectedOutcomeBase):
                type: Literal['Bad Name']

                def score(self, answer, scenario, **_kw):
                    return {}

    def test_empty_backend_name_rejected(self) -> None:
        from memex_eval.suite import AnswerBackend, register_backend

        with pytest.raises(ValueError, match='must match'):

            @register_backend('')
            class _BadBackend(AnswerBackend):
                async def answer(self, scenario, *, api, vault_id, server_url, judge=None):
                    return AgentAnswer()

    def test_built_in_backend_names_pass_validation(self) -> None:
        # 'claude-code' has a hyphen — make sure the backend regex allows it.
        from memex_eval.suite.agents import _BACKEND_NAME_RE

        assert _BACKEND_NAME_RE.match('claude-code')
        assert _BACKEND_NAME_RE.match('hermes')
        assert _BACKEND_NAME_RE.match('api')


class TestBackendRegistryStrict:
    """M-1: register_backend now refuses silent overrides."""

    def test_collision_raises(self) -> None:
        from memex_eval.suite import (
            AnswerBackend,
            register_backend,
        )

        @register_backend('round2_collision_test')
        class _First(AnswerBackend):
            async def answer(self, scenario, *, api, vault_id, server_url, judge=None):
                return AgentAnswer(backend_name=self.name)

        with pytest.raises(ValueError, match='already registered'):

            @register_backend('round2_collision_test')
            class _Second(AnswerBackend):
                async def answer(self, scenario, *, api, vault_id, server_url, judge=None):
                    return AgentAnswer(backend_name=self.name)

    def test_replace_backend_allows_override(self) -> None:
        from memex_eval.suite import (
            AnswerBackend,
            get_backend,
            register_backend,
            replace_backend,
        )

        @register_backend('round2_replace_test')
        class _Origin(AnswerBackend):
            async def answer(self, scenario, *, api, vault_id, server_url, judge=None):
                return AgentAnswer(backend_name=self.name)

        @replace_backend('round2_replace_test')
        class _Override(AnswerBackend):
            async def answer(self, scenario, *, api, vault_id, server_url, judge=None):
                return AgentAnswer(backend_name=self.name, error='from-override')

        assert isinstance(get_backend('round2_replace_test'), _Override)


class TestPerActionRequiredFlag:
    """M-8: per-instance ``required=True`` on SetupAction works via extra='allow'."""

    @pytest.mark.asyncio
    async def test_per_action_required_triggers_short_circuit(self) -> None:
        from memex_eval.suite.runner import _run_setup_actions

        @register_setup_action('non_required_handler')
        class _NonReq(SetupActionHandler):
            # Note: handler-level required is False (default).
            async def run(self, api, vault_id, params):
                raise RuntimeError('boom')

        # Per-action override flips it on.
        action = SetupAction.model_validate({'kind': 'non_required_handler', 'required': True})
        ctx = await _run_setup_actions(
            api=None, vault_id=__import__('uuid').uuid4(), actions=[action]
        )
        assert ctx['_required_setup_failed'] is True
