"""Basic extraction + retrieval reference suite.

The smoke-test suite for the suite framework. Tests the
extraction → indexing → retrieval pipeline via three Project
Alpha/Beta source notes.
"""

from pathlib import Path

from memex_eval.suite import (
    GoldUnitIds,
    KeywordsPresent,
    LLMJudge,
    Scenario,
    Suite,
    SuiteMetadata,
    SuiteSources,
)

_ROOT = Path(__file__).parent

METADATA = SuiteMetadata(
    name='basic_extraction',
    schema_version='1',
    suite_version='1.0.0',
    description=(
        'Smoke-test suite covering fact extraction, keyword and semantic '
        'retrieval, and entity linking against three Acme Corp project notes.'
    ),
    tags=['extraction', 'retrieval', 'entities', 'smoke'],
    primary_metrics=['suite.pass_rate', 'metric.recall_at_5.mean', 'metric.mrr.mean'],
    components_under_test=[
        'extraction.semantic_facts',
        'retrieval.keyword',
        'retrieval.semantic',
        'retrieval.cross_encoder_rerank',
    ],
    knobs=[
        'server.memory.retrieval.reranking_mw_alpha',
        'server.memory.retrieval.reranking_recency_alpha',
    ],
    requires_llm_judge=False,
    default_answer_mode='api',
)

SCENARIOS = [
    Scenario(
        id='alpha_lead_lookup',
        description='Sarah Chen is named as Project Alpha lead.',
        query='Who leads Project Alpha?',
        top_k=5,
        expected=KeywordsPresent(type='keywords_present', keywords=['Sarah Chen']),
    ),
    Scenario(
        id='alpha_tech_stack_python',
        description='Project Alpha tech stack mentions Python 3.12.',
        query='What language does Project Alpha use?',
        top_k=5,
        expected=KeywordsPresent(type='keywords_present', keywords=['Python']),
    ),
    Scenario(
        id='alpha_top5_recall',
        description='Top-5 retrievals for "Project Alpha" cover both kickoff and update notes.',
        query='Project Alpha at Acme',
        top_k=5,
        expected=GoldUnitIds(
            type='gold_unit_ids',
            note_keys=['project-alpha-kickoff', 'project-alpha-update'],
            metrics_to_compute=['recall_at_k', 'mrr'],
        ),
    ),
    Scenario(
        id='beta_lead_lookup',
        description='Marcus Rivera is named as Project Beta lead.',
        query='Who leads Project Beta?',
        top_k=5,
        expected=KeywordsPresent(type='keywords_present', keywords=['Marcus Rivera']),
    ),
    Scenario(
        id='alpha_lead_judge',
        description='LLM-judge: is the top-1 result a correct answer about the Alpha lead?',
        query='Who leads Project Alpha?',
        top_k=1,
        expected=LLMJudge(
            type='llm_judge',
            rubric='The result must identify Sarah Chen as the Project Alpha lead.',
            threshold=0.75,
        ),
    ),
]

SUITE = Suite(
    metadata=METADATA,
    sources=SuiteSources.from_directory(_ROOT / 'sources'),
    scenarios=SCENARIOS,
    readme_path=_ROOT / 'README.md',
)
