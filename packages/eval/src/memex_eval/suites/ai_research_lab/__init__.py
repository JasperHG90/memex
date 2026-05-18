"""AI Research Lab suite — entity resolution, graph cooccurrence, and edge cases.

Adds quantum-research figures (Rodriguez, Osei) alongside the existing AI/NLP
team-meeting and conference sources to exercise name-variant resolution
(``J. Rodriguez`` vs ``Juan Rodriguez``, ``Dr. Amara Osei`` vs ``Amara Osei``)
and cross-document cooccurrence.
"""

from pathlib import Path

from memex_eval.suite import SuiteMetadata, SuiteSources
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

from .scenarios import SCENARIO_SPECS

for _spec in SCENARIO_SPECS:
    suite.register(**_spec)

SUITE = suite.build()
