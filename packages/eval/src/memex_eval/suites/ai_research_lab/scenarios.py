"""Pure-data scenario specs for the ai_research_lab suite.

See ``memex_eval.suite.read_scenario_specs`` for the contract:
this module must be importable WITHOUT triggering the parent
package's ``__init__.py`` (no relative imports, no side effects
beyond appending to ``SCENARIO_SPECS``).
"""

from __future__ import annotations

from memex_eval.suite import (
    EntityCooccurs,
    EntityResolves,
    KeywordsPresent,
)

SCENARIO_SPECS: list[dict] = []


def _register(**kwargs) -> None:
    SCENARIO_SPECS.append(kwargs)


_register(
    id='elena_resolves',
    description='"Dr. Elena Vasquez" and "Elena Vasquez" resolve to the same entity.',
    query='Elena Vasquez',
    top_k=10,
    expected=EntityResolves(
        type='entity_resolves',
        expected_names=['Elena Vasquez'],
    ),
)

_register(
    id='ai_research_lab_resolves',
    description='AI Research Lab is recognized as an entity.',
    query='AI Research Lab',
    top_k=10,
    expected=EntityResolves(
        type='entity_resolves',
        expected_names=['AI Research Lab'],
    ),
)

_register(
    id='elena_cooccurs_with_raj',
    description='Elena Vasquez cooccurs with Raj Mehta in the entity graph.',
    query='Elena Vasquez',
    top_k=10,
    expected=EntityCooccurs(
        type='entity_cooccurs',
        expected_neighbors=['Raj Mehta'],
    ),
)

_register(
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
_register(
    id='abbreviated_name_resolution',
    description='"J. Rodriguez" (symposium) and "Juan Rodriguez" (award) resolve to the same canonical entity.',
    query='Juan Rodriguez',
    top_k=10,
    expected=EntityResolves(
        type='entity_resolves',
        expected_names=['Juan Rodriguez'],
    ),
)

_register(
    id='title_variation_resolution',
    description='"Dr. Amara Osei" and "Amara Osei" resolve to the same canonical entity.',
    query='Amara Osei',
    top_k=10,
    expected=EntityResolves(
        type='entity_resolves',
        expected_names=['Amara Osei'],
    ),
)

_register(
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

_register(
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

_register(
    id='cross_doc_facts_rodriguez',
    description='Search connects Rodriguez facts across symposium and award docs.',
    query='What did Juan Rodriguez achieve in quantum computing?',
    top_k=10,
    expected=KeywordsPresent(
        type='keywords_present',
        keywords=['topological', '99.7%'],
    ),
)
