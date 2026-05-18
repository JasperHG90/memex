"""Project Nexus suite — contradiction, supersession, and lint findings."""

from pathlib import Path

from memex_eval.suite import SuiteMetadata, SuiteSources
from memex_eval.suite.decorator import Suite

_ROOT = Path(__file__).parent

METADATA = SuiteMetadata(
    name='project_nexus',
    schema_version='1',
    suite_version='1.0.0',
    description=(
        'Project Nexus tech-stack history — contradiction detection, '
        'supersession-aware ranking, and lint findings on conflicting '
        'API-versioning policy notes.'
    ),
    tags=['contradiction', 'supersession', 'retrieval', 'lint'],
    primary_metrics=['suite.pass_rate'],
    components_under_test=[
        'memory.contradiction',
        'retrieval.cross_encoder_rerank',
        'retrieval.supersession',
        'services.lint',
        'services.lint_llm',
    ],
    knobs=[
        'server.memory.retrieval.confidence_alpha',
        'server.lint.surprise_threshold',
    ],
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
