"""Temporal-reasoning suite — recency boost and time-aware ranking."""

from pathlib import Path

from memex_eval.suite import (
    KeywordsPresent,
    RankingOrder,
    Scenario,
    Suite,
    SuiteMetadata,
    SuiteSources,
)

_ROOT = Path(__file__).parent

METADATA = SuiteMetadata(
    name='temporal',
    schema_version='1',
    suite_version='1.0.0',
    description='Time-filtered queries and recency-aware ranking.',
    tags=['temporal', 'recency', 'retrieval'],
    primary_metrics=['suite.pass_rate'],
    components_under_test=[
        'retrieval.recency_boost',
        'retrieval.temporal_filter',
    ],
    knobs=[
        'server.memory.retrieval.reranking_recency_alpha',
        'server.memory.retrieval.reranking_temporal_alpha',
    ],
    requires_llm_judge=False,
)

SCENARIOS = [
    Scenario(
        id='current_team_size',
        description='Most-recent headcount fact (45 engineers) appears in top results.',
        query='How many engineers does Acme Corp have?',
        top_k=5,
        expected=KeywordsPresent(type='keywords_present', keywords=['45']),
    ),
    Scenario(
        id='recent_above_old',
        description='2025 status update ranks above 2023 historical mention.',
        query='Acme Corp engineering team',
        top_k=10,
        expected=RankingOrder(
            type='ranking_order',
            expected_keyword_order=['45 engineers', '20 engineers'],
        ),
    ),
]

SUITE = Suite(
    metadata=METADATA,
    sources=SuiteSources.from_directory(_ROOT / 'sources'),
    scenarios=SCENARIOS,
    readme_path=_ROOT / 'README.md',
)
