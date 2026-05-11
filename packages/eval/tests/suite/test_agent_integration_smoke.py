"""Smoke tests for the agent_integration suite (2.0.0).

Structural checks that don't require running the suite:
- the suite definition parses + builds
- every scenario id is unique, snake_case
- every tool name in every outcome has the ``memex_`` prefix
- the ``ToolCallArgMatches`` arg names match the actual plugin schemas
  (guards against wrong arg names like ``record_outcome(kind=...)``
  or ``append_note(content=...)``)
- mutating scenarios all set ``replicates_override=1`` so the run-time
  loop collapses to a single replicate
- xfail scenarios carry ``expected_failure_modes=['hermes']``
- the corpus contains the notes the rubrics reference
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memex_eval.suite.base import (
    AnyOfOutcomes,
    CompositeOutcome,
    ExpectedOutcomeBase,
    ToolCallArgMatches,
    ToolCallContains,
    ToolCallCountAcross,
)
from memex_eval.suites.agent_integration import METADATA, SUITE

_CORPUS_DIR = (
    Path(__file__).parent.parent.parent
    / 'src'
    / 'memex_eval'
    / 'suites'
    / 'agent_integration'
    / 'sources'
)


def _walk_outcomes(outcome: ExpectedOutcomeBase):
    yield outcome
    children = getattr(outcome, 'children', None)
    if children is not None:
        for child in children:
            yield from _walk_outcomes(child)


def _all_outcomes():
    for sc in SUITE.scenarios:
        yield from _walk_outcomes(sc.expected)


def _tool_call_outcomes():
    for o in _all_outcomes():
        if isinstance(o, (ToolCallContains, ToolCallArgMatches, ToolCallCountAcross)):
            yield o


class TestSuiteStructure:
    def test_scenarios_count(self) -> None:
        assert len(SUITE.scenarios) == 26

    def test_scenario_ids_unique(self) -> None:
        ids = [s.id for s in SUITE.scenarios]
        assert len(ids) == len(set(ids))

    def test_metadata_version_bumped(self) -> None:
        assert METADATA.suite_version == '2.0.0'

    def test_metadata_requires_llm_judge(self) -> None:
        assert METADATA.requires_llm_judge is True

    def test_default_answer_mode_is_hermes(self) -> None:
        assert METADATA.default_answer_mode == 'hermes'

    def test_every_scenario_has_max_duration(self) -> None:
        bad = [s.id for s in SUITE.scenarios if s.max_duration_ms is None]
        assert not bad, f'scenarios missing max_duration_ms: {bad}'

    def test_every_scenario_has_group(self) -> None:
        bad = [s.id for s in SUITE.scenarios if not s.group]
        assert not bad, f'scenarios missing group: {bad}'

    def test_smoke_scenarios_kept(self) -> None:
        ids = {s.id for s in SUITE.scenarios}
        for required in (
            'agent_finds_alpha_lead',
            'agent_keywords_in_answer',
            'agent_calls_memex_search',
        ):
            assert required in ids, f'smoke scenario missing: {required}'


class TestToolNamePrefix:
    def test_all_tool_names_have_memex_prefix(self) -> None:
        bad: list[tuple[str, str]] = []
        for sc in SUITE.scenarios:
            for outcome in _walk_outcomes(sc.expected):
                tools = []
                if isinstance(outcome, ToolCallContains):
                    tools = outcome.expected_tools
                elif isinstance(outcome, ToolCallCountAcross):
                    tools = outcome.expected_tools
                elif isinstance(outcome, ToolCallArgMatches):
                    tools = [outcome.tool]
                for t in tools:
                    if not t.startswith('memex_'):
                        bad.append((sc.id, t))
        assert not bad, f'tool names missing memex_ prefix: {bad}'


class TestArgNameSchemaGuards:
    """Guards against the round-2 wire-level regressions.

    The plugin schemas use ``success: bool`` for record_outcome and
    ``delta: str`` for append_note. Earlier design drafts had
    ``kind`` and ``content``; those bugs must never come back.
    """

    @pytest.mark.parametrize(
        'tool,expected_arg',
        [
            ('memex_record_outcome', 'success'),
            ('memex_append_note', 'delta'),
        ],
    )
    def test_arg_matches_use_correct_schema_field(self, tool: str, expected_arg: str) -> None:
        for outcome in _tool_call_outcomes():
            if (
                isinstance(outcome, ToolCallArgMatches)
                and outcome.tool == tool
                and outcome.arg_name != expected_arg
            ):
                pytest.fail(
                    f'ToolCallArgMatches(tool={tool!r}) uses '
                    f'arg_name={outcome.arg_name!r}; schema requires '
                    f'{expected_arg!r}'
                )


class TestMutatingDiscipline:
    _MUTATING_IDS = {
        'feedback_records_success',
        'feedback_deprioritize_obsolete',
        'kv_writes_preference',
        'lifecycle_append_meeting',
        'review_loop_drives_due',
        'review_loop_records_rating',
        'asset_lifecycle_detach',
    }

    _OVERRIDE_ONE_IDS = _MUTATING_IDS | {'kv_retrieves_convention'}

    def test_declared_mutating_scenarios_set_flag(self) -> None:
        actual = {s.id for s in SUITE.scenarios if s.mutating_scenario}
        assert actual == self._MUTATING_IDS

    def test_override_one_scenarios_have_replicates_one(self) -> None:
        bad = []
        for s in SUITE.scenarios:
            if s.id in self._OVERRIDE_ONE_IDS and s.replicates_override != 1:
                bad.append((s.id, s.replicates_override))
        assert not bad, f'expected replicates_override=1, got: {bad}'

    def test_non_override_scenarios_have_no_override(self) -> None:
        non_override = [s for s in SUITE.scenarios if s.id not in self._OVERRIDE_ONE_IDS]
        bad = [
            (s.id, s.replicates_override) for s in non_override if s.replicates_override is not None
        ]
        assert not bad, f'non-override scenarios should not set override: {bad}'


class TestXfailDiscipline:
    _XFAIL_IDS = {
        'review_loop_drives_due',
        'review_loop_records_rating',
        'asset_lifecycle_detach',
    }

    def test_xfail_scenarios_declare_hermes_mode(self) -> None:
        actual = {s.id for s in SUITE.scenarios if 'hermes' in s.expected_failure_modes}
        assert actual == self._XFAIL_IDS


class TestCorpus:
    def test_required_notes_present(self) -> None:
        required = {
            'project-alpha-kickoff.md',
            'project-alpha-q3-update.md',
            'tech-stack-decision-record.md',
            'engineering_handbook.md',
            'incident-2025-08-redis.md',
            'team-retro-q3.md',
            'team-coding-standards.md',
            'architecture-overview.md',
            'legacy-warehouse-deprecated.md',
            'quarterly-revenue-q1.md',
            'quarterly-revenue-q2.md',
            'quarterly-revenue-q3.md',
            'march-meeting-notes.md',
            'sarah-chen-profile.md',
        }
        present = {p.name for p in _CORPUS_DIR.iterdir() if p.suffix == '.md'}
        missing = required - present
        assert not missing, f'corpus missing: {missing}'

    def test_required_notes_non_empty(self) -> None:
        for p in _CORPUS_DIR.iterdir():
            if p.suffix == '.md':
                assert p.read_text(encoding='utf-8').strip(), f'empty note: {p.name}'


class TestComposition:
    def test_navigation_via_page_index_uses_composite(self) -> None:
        sc = next(s for s in SUITE.scenarios if s.id == 'navigation_via_page_index')
        assert isinstance(sc.expected, CompositeOutcome)
        assert len(sc.expected.children) == 3

    def test_survey_broad_topic_uses_composite_with_anyof(self) -> None:
        sc = next(s for s in SUITE.scenarios if s.id == 'survey_broad_topic')
        assert isinstance(sc.expected, CompositeOutcome)
        anyof = sc.expected.children[0]
        assert isinstance(anyof, AnyOfOutcomes)


class TestKvDependency:
    def test_kv_retrieves_depends_on_kv_writes(self) -> None:
        sc = next(s for s in SUITE.scenarios if s.id == 'kv_retrieves_convention')
        assert 'kv_writes_preference' in sc.depends_on_prior_scenarios

    def test_kv_writes_declared_before_kv_retrieves(self) -> None:
        order = [s.id for s in SUITE.scenarios]
        assert order.index('kv_writes_preference') < order.index('kv_retrieves_convention')
