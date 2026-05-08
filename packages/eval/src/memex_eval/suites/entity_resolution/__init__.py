"""Entity resolution + graph cooccurrence suite."""

from pathlib import Path

from memex_eval.suite import (
    EntityCooccurs,
    EntityResolves,
    KeywordsPresent,
    Scenario,
    Suite,
    SuiteMetadata,
    SuiteSources,
)

_ROOT = Path(__file__).parent

METADATA = SuiteMetadata(
    name='entity_resolution',
    schema_version='1',
    suite_version='1.0.0',
    description='Tests name-variant resolution and entity graph cooccurrence ranking.',
    tags=['entities', 'graph', 'resolution'],
    primary_metrics=['suite.pass_rate'],
    components_under_test=['memory.entity_resolver', 'memory.entity_graph'],
    knobs=['server.memory.entity.resolution_threshold'],
    requires_llm_judge=False,
)

SCENARIOS = [
    Scenario(
        id='elena_resolves',
        description='"Dr. Elena Vasquez" and "Elena Vasquez" resolve to the same entity.',
        query='Elena Vasquez',
        top_k=10,
        expected=EntityResolves(
            type='entity_resolves',
            expected_names=['Elena Vasquez'],
        ),
    ),
    Scenario(
        id='ai_research_lab_resolves',
        description='AI Research Lab is recognized as an entity.',
        query='AI Research Lab',
        top_k=10,
        expected=EntityResolves(
            type='entity_resolves',
            expected_names=['AI Research Lab'],
        ),
    ),
    Scenario(
        id='elena_cooccurs_with_raj',
        description='Elena Vasquez cooccurs with Raj Mehta in the entity graph.',
        query='Elena Vasquez',
        top_k=10,
        expected=EntityCooccurs(
            type='entity_cooccurs',
            expected_neighbors=['Raj Mehta'],
        ),
    ),
    Scenario(
        id='cross_doc_facts_about_elena',
        description='Cross-document facts about Elena Vasquez (NLP + NeurIPS keynote) appear.',
        query='What has Elena Vasquez been working on?',
        top_k=10,
        expected=KeywordsPresent(
            type='keywords_present',
            keywords=['NLP', 'transformer'],
        ),
    ),
]

SUITE = Suite(
    metadata=METADATA,
    sources=SuiteSources.from_directory(_ROOT / 'sources'),
    scenarios=SCENARIOS,
    readme_path=_ROOT / 'README.md',
)
