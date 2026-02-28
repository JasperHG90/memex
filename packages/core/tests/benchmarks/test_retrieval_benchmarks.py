"""
Performance benchmarks for retrieval and memory processing operations.

Run with: just benchmark
Or: uv run pytest packages/core/tests/benchmarks --benchmark-only -v
"""

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest


def _make_facts(n: int) -> list[dict]:
    """Generate n sample fact dictionaries."""
    base = datetime(2025, 6, 1, tzinfo=timezone.utc)
    return [
        {
            'unit_id': str(uuid4()),
            'text': f'Fact {i}: Entity_{i % 50} performed action_{i % 20} on object_{i % 30}.',
            'fact_type': ['world', 'experience', 'opinion'][i % 3],
            'context': f'Document {i // 10}',
            'event_date': base + timedelta(minutes=i * 5),
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# 1. Embedding format benchmarks
# ---------------------------------------------------------------------------


@pytest.mark.benchmark(group='formatting')
def test_format_for_embedding_single(benchmark):
    """Benchmark single fact formatting for embedding."""
    from memex_core.memory.formatting import format_for_embedding

    benchmark(
        format_for_embedding,
        text='The user discussed project architecture decisions in the team meeting.',
        fact_type='experience',
        context='Architecture Review',
    )


@pytest.mark.benchmark(group='formatting')
def test_format_for_embedding_batch(benchmark):
    """Benchmark batch formatting of 100 facts for embedding."""
    from memex_core.memory.formatting import format_for_embedding

    facts = _make_facts(100)

    def run():
        for f in facts:
            format_for_embedding(
                text=f['text'],
                fact_type=f['fact_type'],
                context=f['context'],
            )

    benchmark(run)


@pytest.mark.benchmark(group='formatting')
def test_format_for_reranking_batch(benchmark):
    """Benchmark batch formatting of 100 facts for reranking."""
    from memex_core.memory.formatting import format_for_reranking

    facts = _make_facts(100)

    def run():
        for f in facts:
            format_for_reranking(
                text=f['text'],
                event_date=f['event_date'],
                fact_type=f['fact_type'],
                context=f['context'],
            )

    benchmark(run)


# ---------------------------------------------------------------------------
# 2. Temporal link computation benchmarks
# ---------------------------------------------------------------------------


@pytest.mark.benchmark(group='link-computation')
def test_compute_temporal_links_small(benchmark):
    """Benchmark temporal link computation with 10 facts and 50 candidates."""
    from memex_core.memory.extraction.entity_links import compute_temporal_links

    base = datetime(2025, 6, 1, tzinfo=timezone.utc)
    facts = _make_facts(10)
    new_units = {f['unit_id']: f['event_date'] for f in facts}
    candidates = [(uuid4(), base + timedelta(hours=i * 2)) for i in range(50)]

    benchmark(compute_temporal_links, new_units, candidates, time_window_hours=24)


@pytest.mark.benchmark(group='link-computation')
def test_compute_temporal_links_large(benchmark):
    """Benchmark temporal link computation with 100 facts and 500 candidates."""
    from memex_core.memory.extraction.entity_links import compute_temporal_links

    base = datetime(2025, 6, 1, tzinfo=timezone.utc)
    facts = _make_facts(100)
    new_units = {f['unit_id']: f['event_date'] for f in facts}
    candidates = [(uuid4(), base + timedelta(minutes=i * 10)) for i in range(500)]

    benchmark(compute_temporal_links, new_units, candidates, time_window_hours=24)


# ---------------------------------------------------------------------------
# 3. Entity graph link generation benchmarks
# ---------------------------------------------------------------------------


@pytest.mark.benchmark(group='link-computation')
def test_generate_entity_graph_links(benchmark):
    """Benchmark entity graph link generation with realistic data."""
    from memex_core.memory.extraction.entity_links import _generate_entity_graph_links

    new_unit_ids = [str(uuid4()) for _ in range(50)]
    entity_to_units: dict[str, list[UUID]] = {}

    for i in range(20):
        entity_id = str(uuid4())
        units = [UUID(new_unit_ids[j % len(new_unit_ids)]) for j in range(i, i + 5)]
        units.extend([uuid4() for _ in range(10)])
        entity_to_units[entity_id] = units

    benchmark(_generate_entity_graph_links, entity_to_units, new_unit_ids, max_links=50)


# ---------------------------------------------------------------------------
# 4. Causal link construction benchmarks
# ---------------------------------------------------------------------------


@pytest.mark.benchmark(group='link-computation')
def test_build_causal_link_data(benchmark):
    """Benchmark causal link data construction from LLM output."""
    from memex_core.memory.extraction.entity_links import _build_causal_link_data

    unit_ids = [str(uuid4()) for _ in range(50)]
    causal_relations = []
    for i in range(50):
        relations = []
        for j in range(3):
            target = (i + j + 1) % 50
            relations.append(
                {
                    'target_fact_index': target,
                    'relation_type': ['causes', 'caused_by', 'enables'][j % 3],
                    'strength': 0.8,
                }
            )
        causal_relations.append(relations)

    benchmark(_build_causal_link_data, unit_ids, causal_relations)


# ---------------------------------------------------------------------------
# 5. LLM entity flattening benchmarks
# ---------------------------------------------------------------------------


@pytest.mark.benchmark(group='extraction')
def test_flatten_llm_entities(benchmark):
    """Benchmark flattening nested LLM entity output."""
    from memex_core.memory.extraction.entity_links import _flatten_llm_entities

    base = datetime(2025, 6, 1, tzinfo=timezone.utc)
    unit_ids = [str(uuid4()) for _ in range(100)]
    llm_entities = [[{'text': f'Entity_{j}'} for j in range(5)] for _ in range(100)]
    fact_dates = [base + timedelta(hours=i) for i in range(100)]

    benchmark(_flatten_llm_entities, unit_ids, llm_entities, fact_dates)


# ---------------------------------------------------------------------------
# 6. RRF fusion benchmarks (pure Python in-memory)
# ---------------------------------------------------------------------------


@pytest.mark.benchmark(group='rrf-fusion')
def test_rrf_fusion_small(benchmark):
    """Benchmark RRF fusion with 3 strategies x 20 results each."""
    k_rrf = 60
    strategy_results: dict[str, list[tuple[UUID, int, float]]] = {}

    for s in ['semantic', 'keyword', 'temporal']:
        strategy_results[s] = [(uuid4(), rank + 1, 1.0 / (rank + 1)) for rank in range(20)]

    def rrf_fuse():
        scores: dict[UUID, float] = {}
        for _, results in strategy_results.items():
            for uid, rank, _ in results:
                scores[uid] = scores.get(uid, 0.0) + 1.0 / (k_rrf + rank)
        return sorted(scores.keys(), key=lambda k: scores[k], reverse=True)[:10]

    benchmark(rrf_fuse)


@pytest.mark.benchmark(group='rrf-fusion')
def test_rrf_fusion_large(benchmark):
    """Benchmark RRF fusion with 5 strategies x 60 results each."""
    k_rrf = 60
    strategies = ['semantic', 'keyword', 'temporal', 'graph', 'mental_model']
    weights = [1.0, 0.8, 0.6, 0.7, 0.5]
    strategy_data: list[tuple[str, float, list[tuple[UUID, int, float]]]] = []

    for s, w in zip(strategies, weights):
        results = [(uuid4(), rank + 1, 1.0 / (rank + 1)) for rank in range(60)]
        strategy_data.append((s, w, results))

    def rrf_fuse_weighted():
        scores: dict[UUID, float] = {}
        for _, weight, results in strategy_data:
            for uid, rank, _ in results:
                scores[uid] = scores.get(uid, 0.0) + weight / (k_rrf + rank)
        return sorted(scores.keys(), key=lambda k: scores[k], reverse=True)[:20]

    benchmark(rrf_fuse_weighted)


# ---------------------------------------------------------------------------
# 7. Embedding format batch benchmarks (using ProcessedFact)
# ---------------------------------------------------------------------------


@pytest.mark.benchmark(group='formatting')
def test_format_facts_for_embedding_batch(benchmark):
    """Benchmark batch fact formatting for embedding (100 ProcessedFact objects)."""
    from memex_core.memory.extraction.embedding_processor import format_facts_for_embedding
    from memex_core.memory.extraction.models import ProcessedFact
    from memex_common.types import FactTypes

    base = datetime(2025, 6, 1, tzinfo=timezone.utc)
    facts = [
        ProcessedFact(
            fact_text=f'Fact {i}: The system observed behavior pattern {i}.',
            fact_type=[FactTypes.WORLD, FactTypes.EXPERIENCE, FactTypes.OPINION][i % 3],
            embedding=[0.0] * 10,
            context=f'Analysis session {i // 10}',
            mentioned_at=base + timedelta(hours=i),
        )
        for i in range(100)
    ]

    benchmark(format_facts_for_embedding, facts)


# ---------------------------------------------------------------------------
# 8. Date parsing benchmarks
# ---------------------------------------------------------------------------


@pytest.mark.benchmark(group='extraction')
def test_parse_datetime_batch(benchmark):
    """Benchmark parsing 100 ISO datetime strings."""
    from memex_core.memory.extraction.utils import parse_datetime

    dates = [f'2025-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}T{i % 24:02d}:00:00Z' for i in range(100)]

    def run():
        for d in dates:
            parse_datetime(d)

    benchmark(run)
