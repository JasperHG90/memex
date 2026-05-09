"""Project Nexus suite — contradiction, supersession, and lint findings."""

from pathlib import Path

from memex_eval.suite import (
    InlineNote,
    KeywordsPresent,
    LLMJudge,
    LLMLintFlagsUnit,
    LintFindingPresent,
    RankingOrder,
    Scenario,
    SetupAction,
    Suite,
    SuiteMetadata,
    SuiteSources,
)

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
    # Lint scenarios — exercise the maintenance-proposals lint pipeline.
    # The two api-version-* sources contradict each other; consolidation
    # surfaces a finding via the surprise-gated LLM rule. These scenarios
    # run before the inline-note scenarios so the suite-level state is
    # not contaminated by inline-note ingestion. The api backend is
    # required (the lint outcomes inspect server-side findings, which
    # agent backends cannot reproduce from text-only output).
    # ------------------------------------------------------------------
    Scenario(
        id='lint_findings_after_consolidation',
        description=(
            'Consolidation tick + LLM lint pass produces a contradiction '
            'finding on the api-version corpus. We assert against the LLM '
            'rule (``llm_semantic_contradiction``) rather than a V1 '
            'structural rule because the corpus shape — two units making '
            'mutually-exclusive policy claims — is exactly what the LLM '
            'pipeline detects, not what V1_RULES targets. V1_RULES handle '
            'structural problems (orphan mental models, cold low-MW units, '
            'sensitive unreviewed units, dangling entity refs, composite '
            'deprioritize candidates); the LLM ruleset (lint_llm/checks.py) '
            'handles semantic problems (contradictions, surprise, '
            'redundancy). They co-exist by design — V1 is cheap and runs '
            'inline; LLM is expensive and runs on a separate quota-limited '
            'queue.'
        ),
        query='API versioning policy',
        top_k=10,
        setup_actions=[
            SetupAction(kind='consolidation_tick'),
            # The LLM lint pass is what fires on this corpus. lint_run is
            # kept too so V1 rules also get a turn; harmless if no V1 rule
            # fires.
            SetupAction(kind='lint_run'),
            SetupAction(kind='lint_llm_run'),
        ],
        requires_nli_classifier=True,
        expected=LintFindingPresent(
            type='lint_finding_present',
            expected_rule_name='llm_semantic_contradiction',
        ),
        expected_failure_modes=['claude-code', 'hermes'],
    ),
    Scenario(
        id='llm_lint_flags_contradiction',
        description='LLM-gated lint flags the contradiction between API versioning policies.',
        query='API versioning contradiction',
        top_k=10,
        # Explicitly trigger the LLM-gated lint pass so the assertion
        # observes a deterministic post-lint state. ``requires_nli_classifier``
        # gates the scenario when polarity is config-disabled.
        setup_actions=[SetupAction(kind='lint_llm_run')],
        requires_nli_classifier=True,
        expected=LLMLintFlagsUnit(
            type='llm_lint_flags_unit',
            target_keywords=['header-based versioning', 'URL-based'],
        ),
        expected_failure_modes=['claude-code', 'hermes'],
    ),
    # ------------------------------------------------------------------
    # Inline-note scenarios — each declares its own per-scenario note that
    # contradicts a claim in the shared tech-stack-v2.md source. Because
    # inline notes persist in the suite vault for the rest of the run
    # (see the framework's how-to §1.3), these scenarios run LAST so the
    # earlier supersession + lint scenarios above are not contaminated by
    # the third-generation updates introduced here.
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
