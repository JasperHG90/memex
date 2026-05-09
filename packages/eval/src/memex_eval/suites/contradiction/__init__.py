"""Contradiction & supersession suite — verifies updated facts win over older ones."""

from pathlib import Path

from memex_eval.suite import (
    InlineNote,
    KeywordsPresent,
    LLMJudge,
    RankingOrder,
    Scenario,
    Suite,
    SuiteMetadata,
    SuiteSources,
)

_ROOT = Path(__file__).parent

METADATA = SuiteMetadata(
    name='contradiction',
    schema_version='1',
    suite_version='1.0.0',
    description='Contradiction detection + supersession ranking on Project Nexus tech-stack updates.',
    tags=['contradiction', 'supersession', 'retrieval'],
    primary_metrics=['suite.pass_rate'],
    components_under_test=[
        'memory.contradiction',
        'retrieval.cross_encoder_rerank',
        'retrieval.supersession',
    ],
    knobs=['server.memory.retrieval.confidence_alpha'],
    requires_llm_judge=False,
)

SCENARIOS = [
    Scenario(
        id='current_python_version',
        description='Latest Python version (3.12) should appear in results.',
        query='What Python version does Project Nexus use?',
        top_k=10,
        expected=KeywordsPresent(type='keywords_present', keywords=['Python 3.12']),
    ),
    Scenario(
        id='current_framework_fastapi',
        description='Current framework is FastAPI (post-migration).',
        query='What web framework does Project Nexus use?',
        top_k=10,
        expected=KeywordsPresent(type='keywords_present', keywords=['FastAPI']),
    ),
    Scenario(
        id='current_database_postgres',
        description='Current database is PostgreSQL 16.',
        query='What database does Project Nexus use?',
        top_k=10,
        expected=KeywordsPresent(type='keywords_present', keywords=['PostgreSQL 16']),
    ),
    Scenario(
        id='superseded_excluded_by_default',
        description='Default search hides older Jenkins facts in favor of GitHub Actions.',
        query='What CI/CD system does Project Nexus use?',
        top_k=10,
        include_superseded=False,
        expected=KeywordsPresent(type='keywords_present', keywords=['GitHub Actions']),
    ),
    Scenario(
        id='superseded_included_when_asked',
        description='With include_superseded=True, both Jenkins and GitHub Actions surface.',
        query='What CI/CD system does Project Nexus use?',
        top_k=10,
        include_superseded=True,
        expected=KeywordsPresent(type='keywords_present', keywords=['GitHub Actions', 'Jenkins']),
    ),
    Scenario(
        id='ranking_new_above_old',
        description='Top results should mention 3.12 before 3.11 (newer wins ranking).',
        query='Project Nexus Python version',
        top_k=10,
        expected=RankingOrder(
            type='ranking_order',
            expected_keyword_order=['Python 3.12', 'Python 3.11'],
        ),
    ),
    Scenario(
        id='migration_summary_judge',
        description='LLM judge: does the response correctly describe the 2025 migration?',
        query='Describe the Project Nexus 2025 tech-stack migration.',
        top_k=5,
        expected=LLMJudge(
            type='llm_judge',
            rubric=(
                'The result must mention the migration from Django to FastAPI, '
                'MySQL to PostgreSQL, Jenkins to GitHub Actions, and the Python '
                '3.11 → 3.12 upgrade.'
            ),
            threshold=0.5,
        ),
    ),
    # ------------------------------------------------------------------
    # Inline-note scenarios — each declares its own per-scenario note that
    # contradicts a claim in the shared tech-stack-v2.md source. Because
    # inline notes persist in the suite vault for the rest of the run
    # (see the framework's how-to §1.3), these scenarios run LAST so the
    # earlier supersession scenarios above are not contaminated by the
    # third-generation updates introduced here.
    # ------------------------------------------------------------------
    Scenario(
        id='inline_circleci_supersedes_gh_actions',
        description=(
            'Inline note announcing a Q4 2025 CI/CD migration must surface '
            'CircleCI as the current system, superseding GitHub Actions '
            'from tech-stack-v2.md.'
        ),
        query='What CI/CD system does Project Nexus use?',
        top_k=10,
        inline_notes=[
            InlineNote(
                note_key='ci-cd-q4-2025-update',
                title='Project Nexus — CI/CD update (Q4 2025)',
                content=(
                    '# Project Nexus CI/CD update — Q4 2025\n\n'
                    'Effective October 2025, Project Nexus has migrated its '
                    'CI/CD pipelines from GitHub Actions to CircleCI. The '
                    "switch was driven by CircleCI's superior support for "
                    'parallelized integration tests and our enterprise '
                    'contract with their security tier.\n\n'
                    'GitHub Actions usage is being wound down over Q4 2025; '
                    'all new pipelines target CircleCI. Project Nexus uses '
                    'CircleCI for build, test, and deployment.\n'
                ),
                tags=['tech-stack', 'project-nexus', 'ci-cd', 'migration'],
            ),
        ],
        expected=KeywordsPresent(type='keywords_present', keywords=['CircleCI']),
    ),
    Scenario(
        id='inline_python_313_outranks_312',
        description=(
            'Inline note announcing Python 3.13 upgrade ranks above the '
            '3.12 mention in tech-stack-v2.md.'
        ),
        query='Project Nexus Python version',
        top_k=10,
        inline_notes=[
            InlineNote(
                note_key='python-313-upgrade',
                title='Project Nexus — Python 3.13 upgrade (Nov 2025)',
                content=(
                    '# Project Nexus Python 3.13 upgrade\n\n'
                    'Project Nexus completed its Python 3.12 → Python 3.13 '
                    'upgrade in November 2025. All services now run on '
                    'Python 3.13. The 3.13 release brings the experimental '
                    'free-threaded build and improved error messages, both '
                    'of which we exercise in production.\n'
                ),
                tags=['tech-stack', 'project-nexus', 'python', 'upgrade'],
            ),
        ],
        expected=RankingOrder(
            type='ranking_order',
            expected_keyword_order=['Python 3.13', 'Python 3.12'],
        ),
    ),
]

SUITE = Suite(
    metadata=METADATA,
    sources=SuiteSources.from_directory(_ROOT / 'sources'),
    scenarios=SCENARIOS,
    readme_path=_ROOT / 'README.md',
)
