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
        # 39 = 32 prior + 7 `kv_wakeword_*` scenarios (V4 hard wake-word
        # triggers for KV namespace routing: store_{user,project,global,
        # app,with_ttl} + kv_get + kv_search). They pin the explicit
        # imperative path through agent_surface.RETRIEVAL_ROUTING.
        # 46 = 39 + 7 `procedure_*` scenarios (V4 procedure-routing
        # wakewords: explicit_project_cue, in_this_codebase_cue,
        # defaults_global_no_project_cue, ambiguous_no_explicit_scope,
        # ambiguous_asks_first, standard_practice_ambiguous_asks_first,
        # wakeword_store_{global,project}). They pin the
        # "this is how to do X" → memex_kv_put write-routing path.
        # 45 = 39 (smoke+triage+…+kv-wakewords) + 6 procedural-plane
        # agent scenarios (group='procedural'). The 7 legacy procedure_*
        # KV-routing scenarios were removed (deprecated write path). The
        # 6 procedural scenarios: case_submit routing, plane search,
        # read-before-write probe, two retrieve-first (deploy / release)
        # scenarios, and how-to-to-plane-not-KV. No briefing scenario —
        # pinned cards arrive inside the session briefing, not a tool.
        # longer-horizon procedural flows (group='procedural_lh') —
        # multi-step loops with ToolCallOrder gates.
        # Under the case-only write model the agent-facing procedural writes
        # collapsed to ``case_submit``: ``probe_then_update``,
        # ``probes_identity_before_writing`` and ``search_miss_then_create``
        # were dropped and ``corrects_procedure_via_case`` added — net -2
        # from the V4 tally above → 48.
        assert len(SUITE.scenarios) == 48

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

    The plugin schemas use ``units: list[dict]`` for record_outcome
    (per-unit verb shape — see CRITICAL constraint ``record_outcome_shape``)
    and ``delta: str`` for append_note. Earlier design drafts had
    ``kind`` and ``content`` (and the now-FutureWarning ``success`` shape);
    those bugs must never come back.
    """

    @pytest.mark.parametrize(
        'tool,expected_arg',
        [
            ('memex_record_outcome', 'units'),
            ('memex_append_note', 'delta'),
        ],
    )
    def test_arg_matches_use_correct_schema_field(self, tool: str, expected_arg: str) -> None:
        for outcome in _tool_call_outcomes():
            if (
                isinstance(outcome, ToolCallArgMatches)
                and outcome.tool == tool
                and outcome.arg_name != expected_arg
                # Negative assertions (`expect_absent=True`) intentionally
                # name a legacy/forbidden arg to assert it does NOT appear
                # in the tool call. Those are the only scenarios where a
                # non-canonical arg_name is correct.
                and not outcome.expect_absent
            ):
                pytest.fail(
                    f'ToolCallArgMatches(tool={tool!r}) uses '
                    f'arg_name={outcome.arg_name!r}; schema requires '
                    f'{expected_arg!r}'
                )


class TestMutatingDiscipline:
    _MUTATING_IDS = {
        'feedback_records_success',
        'feedback_clarifies_under_ambiguity',
        'feedback_deprioritize_obsolete',
        'feedback_deprioritize_observation_400_recovery',
        'kv_writes_project_preference',
        'kv_writes_user_preference',
        'kv_writes_global_convention',
        'kv_writes_app_setting',
        'kv_wakeword_store_user',
        'kv_wakeword_store_project',
        'kv_wakeword_store_global',
        'kv_wakeword_store_app',
        'kv_wakeword_store_with_ttl',
        'lifecycle_append_meeting',
        'lifecycle_archive_legacy_warehouse_note',
        'lifecycle_append_parent_remains_retrievable',
        'asset_lifecycle_detach',
        # procedural-plane agent scenarios (group='procedural').
        # The legacy ``procedure_*`` KV-routing scenarios were removed
        # (that write path is deprecated — how-tos go to the plane, not
        # KV). Under the case-only write model the agent's only procedural
        # write is ``case_submit``; ``corrects_procedure_via_case`` files a
        # corrective case. replicates_override=2 is tracked in
        # _OVERRIDE_TWO_IDS below.
        'procedural_files_case_via_case_submit',
        'procedural_corrects_procedure_via_case',
        'procedural_searches_before_deploying',
        'procedural_searches_before_release',
        'procedural_routes_howto_to_plane_not_kv',
        # Longer-horizon multi-step flows (group='procedural_lh').
        'procedural_deploy_then_records_outcome',
        'procedural_files_case_after_enacting',
        'procedural_strategy_fallback_on_novel_task',
    }

    # procedural scenarios use replicates_override=2: the routing
    # decision (search-first / case_submit / plane-write vs KV) is the
    # load-bearing contract and a single replay can pass by luck.
    _OVERRIDE_TWO_IDS = {
        'procedural_files_case_via_case_submit',
        'procedural_corrects_procedure_via_case',
        'procedural_searches_before_deploying',
        'procedural_searches_before_release',
        'procedural_routes_howto_to_plane_not_kv',
        'procedural_deploy_then_records_outcome',
        'procedural_files_case_after_enacting',
        'procedural_strategy_fallback_on_novel_task',
    }

    # The legacy ``procedure_*`` KV-routing scenarios (replicates=3)
    # were removed with the deprecated KV-procedure write path. No
    # scenario currently uses replicates_override=3.
    _OVERRIDE_THREE_IDS: set[str] = set()

    # Override-1 means "use replicates_override=1 instead of the suite
    # default" — applied to scenarios that are LLM-classifier-only
    # (so a single-shot is stable) and don't need statistical
    # robustness. The procedure_* wakeword scenarios need replicates=3
    # for noise dampening, so they're NOT in this set.
    _OVERRIDE_ONE_IDS = (_MUTATING_IDS - _OVERRIDE_THREE_IDS - _OVERRIDE_TWO_IDS) | {
        'kv_retrieves_convention',
        'kv_wakeword_kv_get',
        'kv_wakeword_kv_search',
    }

    def test_declared_mutating_scenarios_set_flag(self) -> None:
        actual = {s.id for s in SUITE.scenarios if s.mutating_scenario}
        assert actual == self._MUTATING_IDS

    def test_override_one_scenarios_have_replicates_one(self) -> None:
        bad = []
        for s in SUITE.scenarios:
            if s.id in self._OVERRIDE_ONE_IDS and s.replicates_override != 1:
                bad.append((s.id, s.replicates_override))
        assert not bad, f'expected replicates_override=1, got: {bad}'

    def test_override_three_scenarios_have_replicates_three(self) -> None:
        """The V4 procedure_* scenarios use ``replicates_override=3``
        to dampen LLM-judge noise on the project/global/ambiguous
        routing choices. A regression that drops the override would
        make the scenarios pass-on-noise."""
        bad = []
        for s in SUITE.scenarios:
            if s.id in self._OVERRIDE_THREE_IDS and s.replicates_override != 3:
                bad.append((s.id, s.replicates_override))
        assert not bad, f'expected replicates_override=3, got: {bad}'

    def test_override_two_scenarios_have_replicates_two(self) -> None:
        bad = []
        for s in SUITE.scenarios:
            if s.id in self._OVERRIDE_TWO_IDS and s.replicates_override != 2:
                bad.append((s.id, s.replicates_override))
        assert not bad, f'expected replicates_override=2, got: {bad}'

    def test_non_override_scenarios_have_no_override(self) -> None:
        allowed = self._OVERRIDE_ONE_IDS | self._OVERRIDE_THREE_IDS | self._OVERRIDE_TWO_IDS
        non_override = [s for s in SUITE.scenarios if s.id not in allowed]
        bad = [
            (s.id, s.replicates_override) for s in non_override if s.replicates_override is not None
        ]
        assert not bad, f'non-override scenarios should not set override: {bad}'


class TestKvNamespaceCoverage:
    """Every kv_write namespace gets a dedicated scenario with a
    precise regex — drift between them (e.g. two scenarios using the
    same regex) would silently weaken the coverage claim."""

    _NAMESPACE_BY_ID = {
        'kv_writes_project_preference': r'^project:.+',
        'kv_writes_user_preference': r'^user:.+',
        'kv_writes_global_convention': r'^global:.+',
        'kv_writes_app_setting': r'^app:.+',
    }

    def test_each_namespace_has_a_scenario(self) -> None:
        ids = {s.id for s in SUITE.scenarios}
        missing = set(self._NAMESPACE_BY_ID) - ids
        assert not missing, f'kv namespace scenarios missing: {missing}'

    def test_each_kv_scenario_uses_its_namespace_regex(self) -> None:
        sc_by_id = {s.id for s in SUITE.scenarios}
        for sc_id, expected_regex in self._NAMESPACE_BY_ID.items():
            assert sc_id in sc_by_id
            sc = next(s for s in SUITE.scenarios if s.id == sc_id)
            arg_matches = [
                outcome
                for outcome in _walk_outcomes(sc.expected)
                if isinstance(outcome, ToolCallArgMatches)
                and outcome.tool == 'memex_kv_put'
                and outcome.arg_name == 'key'
            ]
            assert len(arg_matches) == 1, (
                f'{sc_id}: expected exactly one ToolCallArgMatches on '
                f'memex_kv_put.key; got {len(arg_matches)}'
            )
            assert arg_matches[0].regex == expected_regex, (
                f'{sc_id}: regex drifted to {arg_matches[0].regex!r}; expected {expected_regex!r}'
            )


class TestXfailDiscipline:
    # `review_loop_*` tripwires removed alongside FSRS — see suite docstring.
    _XFAIL_IDS = {
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
            'kafka-batching-strategy.md',
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
        # KeywordsPresent('pytest') + LLMJudge rubric — the navigation
        # path is grounded by keyword presence; semantic correctness is
        # then judged. A third child was considered (`memex_get_nodes`
        # tool call assertion) but dropped as too brittle on top-k≥30.
        assert len(sc.expected.children) == 2

    def test_survey_broad_topic_uses_composite_with_anyof(self) -> None:
        sc = next(s for s in SUITE.scenarios if s.id == 'survey_broad_topic')
        assert isinstance(sc.expected, CompositeOutcome)
        anyof = sc.expected.children[0]
        assert isinstance(anyof, AnyOfOutcomes)


class TestKvDependency:
    def test_kv_retrieves_depends_on_kv_writes_project(self) -> None:
        sc = next(s for s in SUITE.scenarios if s.id == 'kv_retrieves_convention')
        assert 'kv_writes_project_preference' in sc.depends_on_prior_scenarios

    def test_kv_writes_project_declared_before_kv_retrieves(self) -> None:
        order = [s.id for s in SUITE.scenarios]
        assert order.index('kv_writes_project_preference') < order.index('kv_retrieves_convention')
