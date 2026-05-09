"""Memex evaluation-suite framework.

Public surface for suite authors:

    from memex_eval.suite import (
        # Core types
        Suite,
        SuiteMetadata,
        SuiteSources,
        SourceNote,
        Scenario,
        SetupAction,
        InlineNote,
        # Built-in outcomes
        KeywordsPresent,
        KeywordsAbsent,
        EntityResolves,
        EntityCooccurs,
        EntityMentionContains,
        GoldUnitIds,
        RankingOrder,
        ExcludedByDefault,
        LLMJudge,
        UsefulAtK,
        LintFindingPresent,
        LLMLintFlagsUnit,
        KvRoundtrip,
        SummaryNonempty,
        UnitMetadataMatches,
        ToolCallContains,
        CompositeOutcome,
        # Extension surface — register custom outcomes / actions / backends
        ExpectedOutcomeBase,
        register_outcome,
        replace_outcome,
        unregister_outcome,
        SetupActionHandler,
        register_setup_action,
        replace_setup_action,
        unregister_setup_action,
        AnswerBackend,
        register_backend,
        replace_backend,
        unregister_backend,
        # Test isolation helper
        isolated_registries,
    )
"""

from memex_eval.suite.agents import (
    AgentAnswer,
    AnswerBackend,
    ClaudeCodeBackend,
    DirectApiBackend,
    HermesBackend,
    get_backend,
    list_backends,
    register_backend,
    replace_backend,
    unregister_backend,
)
from memex_eval.suite.base import (
    CompositeOutcome,
    EntityCooccurs,
    EntityMentionContains,
    EntityResolves,
    ExcludedByDefault,
    ExpectedOutcomeBase,
    ExpectedOutcomeUnion,
    GoldUnitIds,
    InlineNote,
    KeywordsAbsent,
    KeywordsPresent,
    KvRoundtrip,
    LintFindingPresent,
    LLMJudge,
    LLMLintFlagsUnit,
    RankingOrder,
    RunResult,
    Scenario,
    ScenarioOutcome,
    SetupAction,
    SourceNote,
    Suite,
    SuiteMetadata,
    SuiteSources,
    SummaryNonempty,
    ToolCallContains,
    UnitMetadataMatches,
    UsefulAtK,
    get_outcome_class,
    list_outcomes,
    register_outcome,
    replace_outcome,
    unregister_outcome,
)
from memex_eval.suite.loader import (
    SuiteNotFound,
    discover_suite_names,
    discover_suites,
    load_suite,
)
from memex_eval.suite.setup_actions import (
    SetupActionHandler,
    get_setup_action,
    list_setup_actions,
    register_setup_action,
    replace_setup_action,
    unregister_setup_action,
)


def isolated_registries():
    """Context manager: snapshot every framework registry and restore on exit.

    Use in tests that register custom outcomes/actions/backends to keep the
    process-global registries clean across runs::

        with isolated_registries():
            @register_outcome('my_test_outcome')
            class _MyTest(ExpectedOutcomeBase): ...
    """
    import contextlib

    from memex_eval.suite.agents import _BACKEND_REGISTRY
    from memex_eval.suite.base import _OUTCOME_REGISTRY
    from memex_eval.suite.setup_actions import _SETUP_ACTION_REGISTRY

    @contextlib.contextmanager
    def _ctx():
        outcomes = dict(_OUTCOME_REGISTRY)
        actions = dict(_SETUP_ACTION_REGISTRY)
        backends = dict(_BACKEND_REGISTRY)
        try:
            yield
        finally:
            _OUTCOME_REGISTRY.clear()
            _OUTCOME_REGISTRY.update(outcomes)
            _SETUP_ACTION_REGISTRY.clear()
            _SETUP_ACTION_REGISTRY.update(actions)
            _BACKEND_REGISTRY.clear()
            _BACKEND_REGISTRY.update(backends)

    return _ctx()


__all__ = [
    'Suite',
    'SuiteMetadata',
    'SuiteSources',
    'SourceNote',
    'Scenario',
    'SetupAction',
    'InlineNote',
    'KeywordsPresent',
    'KeywordsAbsent',
    'EntityResolves',
    'EntityCooccurs',
    'EntityMentionContains',
    'GoldUnitIds',
    'RankingOrder',
    'ExcludedByDefault',
    'LLMJudge',
    'UsefulAtK',
    'LintFindingPresent',
    'LLMLintFlagsUnit',
    'KvRoundtrip',
    'SummaryNonempty',
    'UnitMetadataMatches',
    'ToolCallContains',
    'CompositeOutcome',
    'ExpectedOutcomeUnion',
    'ScenarioOutcome',
    'RunResult',
    'AgentAnswer',
    'AnswerBackend',
    'DirectApiBackend',
    'ClaudeCodeBackend',
    'HermesBackend',
    'register_backend',
    'replace_backend',
    'unregister_backend',
    'get_backend',
    'list_backends',
    'load_suite',
    'discover_suites',
    'discover_suite_names',
    'SuiteNotFound',
    # Extensibility surface
    'ExpectedOutcomeBase',
    'register_outcome',
    'replace_outcome',
    'unregister_outcome',
    'get_outcome_class',
    'list_outcomes',
    'SetupActionHandler',
    'register_setup_action',
    'replace_setup_action',
    'unregister_setup_action',
    'get_setup_action',
    'list_setup_actions',
    'isolated_registries',
]
