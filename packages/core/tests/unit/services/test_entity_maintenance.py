"""Unit tests for entity_maintenance helpers (clustering, cohesion guard).

These tests exercise the pure-Python helpers in
``services/entity_maintenance.py`` without touching a DB:

- ``_connected_components`` groups transitive pairs into clusters.
- ``_min_pairwise_similarity`` returns (min, max) across all pairs.
- ``_composition_hash`` is stable under member reordering.
- ``_suggested_winner`` picks by mention_count then first_seen.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from unittest.mock import MagicMock

from memex_core.services.entity_maintenance import (
    _composition_hash,
    _connected_components,
    _min_pairwise_similarity,
    _suggested_winner,
    scan_collapse_clusters,
)


def test_connected_components_groups_three_pairs_into_one_cluster():
    nodes = ['A', 'B', 'C', 'D']
    edges = [('A', 'B'), ('B', 'C')]
    clusters = _connected_components(nodes, edges)
    assert len(clusters) == 1
    assert sorted(clusters[0]) == ['A', 'B', 'C']


def test_connected_components_returns_independent_clusters():
    nodes = ['A', 'B', 'C', 'D']
    edges = [('A', 'B'), ('C', 'D')]
    clusters = sorted(_connected_components(nodes, edges), key=lambda c: c[0])
    assert clusters == [['A', 'B'], ['C', 'D']]


def test_connected_components_filters_singletons():
    nodes = ['A', 'B', 'C']
    edges = [('A', 'B')]
    clusters = _connected_components(nodes, edges)
    assert clusters == [['A', 'B']]


def test_composition_hash_stable_under_reorder():
    h1 = _composition_hash(['A', 'B', 'C'])
    h2 = _composition_hash(['C', 'A', 'B'])
    h3 = _composition_hash(['B', 'A', 'C'])
    assert h1 == h2 == h3


def test_composition_hash_differs_by_membership():
    h1 = _composition_hash(['A', 'B', 'C'])
    h2 = _composition_hash(['A', 'B', 'D'])
    assert h1 != h2


def test_min_pairwise_similarity_returns_min_max():
    sims = {
        ('A', 'B'): 0.9,
        ('A', 'C'): 0.8,
        ('B', 'C'): 0.6,
    }
    lo, hi = _min_pairwise_similarity(['A', 'B', 'C'], sims)
    assert lo == pytest.approx(0.6)
    assert hi == pytest.approx(0.9)


def test_min_pairwise_similarity_handles_pair():
    sims = {('A', 'B'): 0.95}
    lo, hi = _min_pairwise_similarity(['A', 'B'], sims)
    assert lo == hi == pytest.approx(0.95)


def test_min_pairwise_similarity_rope_drift_detection():
    """Chain graph with weak bridge: min stays below threshold even though all
    edges are above pair_threshold individually (the missing pair is what
    triggers rope-drift rejection)."""
    sims = {
        ('A', 'B'): 0.9,
        ('B', 'C'): 0.9,
        # A<->C pair missing from edge set; treated as 0.0 in min
        ('A', 'C'): 0.4,
    }
    lo, _ = _min_pairwise_similarity(['A', 'B', 'C'], sims)
    assert lo == pytest.approx(0.4)


def test_suggested_winner_picks_highest_mention_count():
    members = [
        {'id': 'a', 'mention_count': 3, 'first_seen': datetime(2026, 1, 1, tzinfo=timezone.utc)},
        {'id': 'b', 'mention_count': 5, 'first_seen': datetime(2026, 1, 1, tzinfo=timezone.utc)},
        {'id': 'c', 'mention_count': 1, 'first_seen': datetime(2026, 1, 1, tzinfo=timezone.utc)},
    ]
    assert _suggested_winner(members) == 'b'


def test_suggested_winner_breaks_ties_by_first_seen():
    members = [
        {
            'id': 'newer',
            'mention_count': 5,
            'first_seen': datetime(2026, 5, 1, tzinfo=timezone.utc),
        },
        {
            'id': 'older',
            'mention_count': 5,
            'first_seen': datetime(2026, 1, 1, tzinfo=timezone.utc),
        },
    ]
    assert _suggested_winner(members) == 'older'


def test_suggested_winner_handles_none_first_seen():
    members = [
        {
            'id': 'no-first-seen',
            'mention_count': 5,
            'first_seen': None,
        },
        {
            'id': 'oldest',
            'mention_count': 5,
            'first_seen': datetime(2026, 1, 1, tzinfo=timezone.utc),
        },
        {
            'id': 'middle',
            'mention_count': 5,
            'first_seen': datetime(2026, 3, 1, tzinfo=timezone.utc),
        },
    ]
    assert _suggested_winner(members) == 'oldest'


@pytest.mark.asyncio
async def test_scan_collapse_rejects_inverted_thresholds():
    """Runtime overrides bypass EntityMaintenanceConfig's model_validator, so the
    scan must independently reject cluster_min_threshold > pair_threshold (which
    would otherwise reject every cluster as below-cohesion)."""
    from memex_common.config import EntityMaintenanceConfig, MemexConfig

    config = MemexConfig()
    config.server.memory.entity_maintenance = EntityMaintenanceConfig(
        scan_enabled=True,
        top_n=100,
        scan_cooldown_days=0,
        pair_threshold=0.85,
        cluster_min_threshold=0.7,
    )
    api = MagicMock()
    api.config = config

    with pytest.raises(ValueError, match='cluster_min_threshold'):
        await scan_collapse_clusters(
            api,
            cluster_min_threshold=0.95,
            pair_threshold=0.7,
        )
