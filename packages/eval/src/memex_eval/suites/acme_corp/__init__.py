"""Acme Corp consolidated suite.

Folds eleven legacy scenario groups (basic extraction, temporal,
reflection, scale, assets, outcomes-MW, deprioritization, intent
classification, procedural KV, summarization, vault isolation) into a
single source-doc-organized suite. Every scenario grounds against a
markdown source under ``sources/``; vault-scoped scenarios pin
``vault_name=`` to exercise multi-vault isolation.

Order matters: scenarios that depend on side-effects (setup actions,
ingestion of additional content) appear AFTER the side-effect-free
keyword/entity scenarios that hit the same corpus, so earlier scenarios
see a clean baseline.
"""

from pathlib import Path

from memex_eval.suite import SuiteMetadata, SuiteSources
from memex_eval.suite.decorator import Suite

_ROOT = Path(__file__).parent

METADATA = SuiteMetadata(
    name='acme_corp',
    schema_version='1',
    suite_version='1.0.0',
    description=(
        'Consolidated Acme Corp / TechCo Global suite — covers extraction, '
        'retrieval, entity resolution, reflection, temporal recency, '
        'memory-worth ranking, deprioritization, intent classification, '
        'procedural KV roundtrip, summarization, and multi-vault isolation '
        'over a single source-doc-grounded corpus.'
    ),
    tags=[
        'extraction',
        'retrieval',
        'entities',
        'reflection',
        'temporal',
        'scale',
        'assets',
        'outcomes',
        'deprioritization',
        'intent',
        'kv',
        'summarization',
        'vault-isolation',
    ],
    primary_metrics=['suite.pass_rate'],
    components_under_test=[
        'extraction.semantic_facts',
        'retrieval.keyword',
        'retrieval.semantic',
        'retrieval.temporal',
        'retrieval.mental_model',
        'retrieval.cross_encoder_rerank',
        'memory.entity_resolver',
        'memory.entity_graph',
        'memory.reflection',
        'memory.outcomes',
        'memory.deprioritization',
        'memory.intent_classification',
        'memory.kv_store',
        'memory.summarization',
        'multi_tenancy.vault_scoping',
    ],
    knobs=[
        'server.memory.retrieval.reranking_mw_alpha',
        'server.memory.retrieval.reranking_recency_alpha',
        'server.memory.retrieval.reranking_temporal_alpha',
        'server.memory.entity.resolution_threshold',
    ],
    requires_llm_judge=False,
    default_answer_mode='api',
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
