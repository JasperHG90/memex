"""AI Research Lab suite — entity resolution, graph cooccurrence, and edge cases.

Adds quantum-research figures (Rodriguez, Osei) alongside the existing AI/NLP
team-meeting and conference sources to exercise name-variant resolution
(``J. Rodriguez`` vs ``Juan Rodriguez``, ``Dr. Amara Osei`` vs ``Amara Osei``)
and cross-document cooccurrence.
"""

from pathlib import Path

from memex_eval.suite import (
    EntityCooccurs,
    EntityResolves,
    KeywordsPresent,
    SuiteMetadata,
    SuiteSources,
)
from memex_eval.suite.decorator import Suite

_ROOT = Path(__file__).parent

METADATA = SuiteMetadata(
    name='ai_research_lab',
    schema_version='1',
    suite_version='1.0.0',
    description=(
        'Entity resolution + graph cooccurrence over AI research, NLP, and '
        'quantum-computing source documents — covers diacritics, abbreviated '
        'names, and title-prefix variants.'
    ),
    tags=['entities', 'graph', 'resolution', 'edge-cases'],
    primary_metrics=['suite.pass_rate'],
    components_under_test=[
        'memory.entity_resolver',
        'memory.entity_graph',
        'memory.entity_cooccurrence',
    ],
    knobs=['server.memory.entity.resolution_threshold'],
    requires_llm_judge=False,
)

suite = Suite(
    metadata=METADATA,
    sources=SuiteSources.from_directory(_ROOT / 'sources'),
    readme_path=_ROOT / 'README.md',
)

suite.register(
    id='elena_resolves',
    description='"Dr. Elena Vasquez" and "Elena Vasquez" resolve to the same entity.',
    query='Elena Vasquez',
    top_k=10,
    expected=EntityResolves(
        type='entity_resolves',
        expected_names=['Elena Vasquez'],
    ),
)

suite.register(
    id='ai_research_lab_resolves',
    description='AI Research Lab is recognized as an entity.',
    query='AI Research Lab',
    top_k=10,
    expected=EntityResolves(
        type='entity_resolves',
        expected_names=['AI Research Lab'],
    ),
)

suite.register(
    id='elena_cooccurs_with_raj',
    description='Elena Vasquez cooccurs with Raj Mehta in the entity graph.',
    query='Elena Vasquez',
    top_k=10,
    expected=EntityCooccurs(
        type='entity_cooccurs',
        expected_neighbors=['Raj Mehta'],
    ),
)

suite.register(
    id='cross_doc_facts_about_elena',
    description='Cross-document facts about Elena Vasquez (NLP + NeurIPS keynote) appear.',
    query='What has Elena Vasquez been working on?',
    top_k=10,
    expected=KeywordsPresent(
        type='keywords_present',
        keywords=['NLP', 'transformer'],
    ),
)

# ------------------------------------------------------------------
# Entity edge-case scenarios — quantum-research figures with
# abbreviated forms and titles that must collapse to canonical names.
# Phrased with canonical names ('Juan Rodriguez', 'Amara Osei')
# because EntityResolves uses set equality, not substring matching.
# ------------------------------------------------------------------
suite.register(
    id='abbreviated_name_resolution',
    description='"J. Rodriguez" (symposium) and "Juan Rodriguez" (award) resolve to the same canonical entity.',
    query='Juan Rodriguez',
    top_k=10,
    expected=EntityResolves(
        type='entity_resolves',
        expected_names=['Juan Rodriguez'],
    ),
)

suite.register(
    id='title_variation_resolution',
    description='"Dr. Amara Osei" and "Amara Osei" resolve to the same canonical entity.',
    query='Amara Osei',
    top_k=10,
    expected=EntityResolves(
        type='entity_resolves',
        expected_names=['Amara Osei'],
    ),
)

suite.register(
    id='cross_doc_entity_cooccurrence',
    description='Juan Rodriguez cooccurs with Amara Osei across symposium and profile docs.',
    query='Juan Rodriguez',
    top_k=10,
    expected=EntityCooccurs(
        type='entity_cooccurs',
        expected_neighbors=['Amara Osei'],
    ),
    expected_failure_modes=['claude-code', 'hermes'],
)

suite.register(
    id='quantumtech_labs_entity',
    description='QuantumTech Labs surfaces as an Organization entity.',
    query='QuantumTech Labs',
    top_k=10,
    expected=EntityResolves(
        type='entity_resolves',
        expected_names=['QuantumTech Labs'],
        expected_type='Organization',
    ),
)

suite.register(
    id='cross_doc_facts_rodriguez',
    description='Search connects Rodriguez facts across symposium and award docs.',
    query='What did Juan Rodriguez achieve in quantum computing?',
    top_k=10,
    expected=KeywordsPresent(
        type='keywords_present',
        keywords=['topological', '99.7%'],
    ),
)

SUITE = suite.build()
