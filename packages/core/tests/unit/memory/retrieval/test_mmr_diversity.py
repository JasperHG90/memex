"""Tests for MMR diversity filtering in RetrievalEngine."""

from datetime import datetime, timezone
from uuid import uuid4

from memex_core.memory.retrieval.engine import RetrievalEngine
from memex_core.memory.sql_models import MemoryUnit, UnitEntity
from memex_common.types import FactTypes


def _make_unit(
    text: str = 'test',
    event_date: datetime | None = None,
    entity_ids: list | None = None,
) -> MemoryUnit:
    """Create a minimal MemoryUnit for testing MMR."""
    unit_id = uuid4()
    vault_id = uuid4()
    unit = MemoryUnit(
        id=unit_id,
        note_id=uuid4(),
        text=text,
        fact_type=FactTypes.WORLD,
        vault_id=vault_id,
        event_date=event_date or datetime(2026, 1, 1, tzinfo=timezone.utc),
        occurred_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    # Attach unit_entities for Jaccard computation
    if entity_ids:
        unit.unit_entities = [
            UnitEntity(unit_id=unit_id, entity_id=eid, vault_id=vault_id) for eid in entity_ids
        ]
    else:
        unit.unit_entities = []
    return unit


def _sim_matrix_from_dict(
    pairs: dict[tuple[int, int], float], units: list[MemoryUnit]
) -> dict[tuple, float]:
    """Build a similarity matrix from index-based pairs."""
    matrix: dict[tuple, float] = {}
    for (i, j), sim in pairs.items():
        matrix[(units[i].id, units[j].id)] = sim
        matrix[(units[j].id, units[i].id)] = sim
    return matrix


class TestApplyMmrDiversity:
    """Tests for _apply_mmr_diversity static method."""

    def test_identical_results_collapsed(self):
        """Near-duplicate pair (cosine=1.0, same entities) — one should be removed."""
        e1, e2 = uuid4(), uuid4()
        a = _make_unit('fact A', entity_ids=[e1, e2])
        b = _make_unit('fact B', entity_ids=[e1, e2])
        c = _make_unit('fact C', entity_ids=[uuid4()])

        sim = _sim_matrix_from_dict({(0, 1): 1.0, (0, 2): 0.1, (1, 2): 0.1}, [a, b, c])

        result = RetrievalEngine._apply_mmr_diversity([a, b, c], sim, lambda_=0.7, limit=2)

        assert len(result) == 2
        assert result[0] is a  # top result always first
        # c should be preferred over b due to diversity
        assert result[1] is c

    def test_different_entities_preserved(self):
        """High cosine but disjoint entities — both kept."""
        a = _make_unit('fact A', entity_ids=[uuid4()])
        b = _make_unit('fact B', entity_ids=[uuid4()])

        # High cosine but entity Jaccard is 0 — hybrid sim will be moderate
        sim = _sim_matrix_from_dict({(0, 1): 0.4}, [a, b])

        result = RetrievalEngine._apply_mmr_diversity([a, b], sim, lambda_=0.9, limit=10)

        assert len(result) == 2
        assert result[0] is a
        assert result[1] is b

    def test_temporal_tiebreaker(self):
        """Equal MMR scores — newer event_date wins.

        We construct a scenario where two candidates have identical relevance
        scores and identical similarity to the anchor, so their MMR scores
        are within epsilon and the temporal tiebreaker fires.
        """
        anchor = _make_unit('anchor', event_date=datetime(2026, 1, 1, tzinfo=timezone.utc))
        # older and newer at adjacent positions — but we'll give them equal
        # similarity to anchor so the MMR scores are within epsilon
        older = _make_unit('old', event_date=datetime(2025, 1, 1, tzinfo=timezone.utc))
        newer = _make_unit('new', event_date=datetime(2026, 6, 1, tzinfo=timezone.utc))

        # Use lambda_=0.0 so relevance is zeroed out and only diversity matters.
        # Both have same sim to anchor (0.5), so MMR = -0.5 for both → tiebreaker fires.
        sim = _sim_matrix_from_dict(
            {(0, 1): 0.5, (0, 2): 0.5, (1, 2): 0.0},
            [anchor, older, newer],
        )

        result = RetrievalEngine._apply_mmr_diversity(
            [anchor, older, newer], sim, lambda_=0.0, limit=3
        )

        assert result[0] is anchor
        # newer should beat older due to temporal tiebreaker
        assert result[1] is newer

    def test_disabled_when_lambda_none(self):
        """The pipeline guards with `if mmr_lambda is not None` — verify passthrough."""
        a = _make_unit('a')
        b = _make_unit('b')
        # This test verifies the static method still works when called directly
        # The None guard is in the pipeline, not the method itself
        result = RetrievalEngine._apply_mmr_diversity([a, b], {}, lambda_=0.9, limit=10)
        assert len(result) == 2

    def test_conservative_lambda_preserves_most(self):
        """Lambda=0.9 should only prune near-duplicates, keeping diverse results."""
        units = [_make_unit(f'unit {i}', entity_ids=[uuid4()]) for i in range(5)]

        # Low similarity between all pairs — all should be kept
        pairs = {}
        for i in range(5):
            for j in range(i + 1, 5):
                pairs[(i, j)] = 0.2
        sim = _sim_matrix_from_dict(pairs, units)

        result = RetrievalEngine._apply_mmr_diversity(units, sim, lambda_=0.9, limit=10)

        assert len(result) == 5
        # Order should be mostly preserved since diversity penalty is small
        assert result[0] is units[0]

    def test_empty_input(self):
        """Empty input returns empty output."""
        assert RetrievalEngine._apply_mmr_diversity([], {}, lambda_=0.9, limit=10) == []

    def test_single_item(self):
        """Single item returns as-is."""
        a = _make_unit('only one')
        result = RetrievalEngine._apply_mmr_diversity([a], {}, lambda_=0.9, limit=10)
        assert result == [a]

    def test_limit_respected(self):
        """Output length respects the limit parameter."""
        units = [_make_unit(f'unit {i}') for i in range(5)]
        sim = _sim_matrix_from_dict({(i, j): 0.1 for i in range(5) for j in range(i + 1, 5)}, units)

        result = RetrievalEngine._apply_mmr_diversity(units, sim, lambda_=0.9, limit=3)
        assert len(result) == 3


class TestHybridSimilarityWeights:
    """Tests for _build_hybrid_similarity_matrix."""

    def test_weighted_combination(self):
        """Verify w_emb + w_ent combination produces correct values."""
        id_a, id_b = uuid4(), uuid4()
        cosine = {(id_a, id_b): 0.8, (id_b, id_a): 0.8}
        jaccard = {(id_a, id_b): 0.5, (id_b, id_a): 0.5}

        result = RetrievalEngine._build_hybrid_similarity_matrix(
            cosine, jaccard, w_emb=0.6, w_ent=0.4
        )

        expected = 0.6 * 0.8 + 0.4 * 0.5  # 0.48 + 0.2 = 0.68
        assert abs(result[(id_a, id_b)] - expected) < 1e-9

    def test_missing_cosine_defaults_zero(self):
        """Pairs only in jaccard matrix get cosine=0.0."""
        id_a, id_b = uuid4(), uuid4()
        cosine: dict[tuple, float] = {}
        jaccard = {(id_a, id_b): 1.0}

        result = RetrievalEngine._build_hybrid_similarity_matrix(
            cosine, jaccard, w_emb=0.6, w_ent=0.4
        )

        assert abs(result[(id_a, id_b)] - 0.4) < 1e-9

    def test_missing_jaccard_defaults_zero(self):
        """Pairs only in cosine matrix get jaccard=0.0."""
        id_a, id_b = uuid4(), uuid4()
        cosine = {(id_a, id_b): 1.0}
        jaccard: dict[tuple, float] = {}

        result = RetrievalEngine._build_hybrid_similarity_matrix(
            cosine, jaccard, w_emb=0.6, w_ent=0.4
        )

        assert abs(result[(id_a, id_b)] - 0.6) < 1e-9


class TestComputeEntityJaccard:
    """Tests for _compute_entity_jaccard."""

    def test_identical_entities(self):
        """Same entity set → Jaccard = 1.0."""
        e1, e2 = uuid4(), uuid4()
        a = _make_unit('a', entity_ids=[e1, e2])
        b = _make_unit('b', entity_ids=[e1, e2])

        result = RetrievalEngine._compute_entity_jaccard([a, b])

        assert abs(result[(a.id, b.id)] - 1.0) < 1e-9

    def test_disjoint_entities(self):
        """No overlap → Jaccard = 0.0."""
        a = _make_unit('a', entity_ids=[uuid4()])
        b = _make_unit('b', entity_ids=[uuid4()])

        result = RetrievalEngine._compute_entity_jaccard([a, b])

        assert abs(result[(a.id, b.id)] - 0.0) < 1e-9

    def test_empty_entities(self):
        """Both empty → Jaccard = 0.0 (no division by zero)."""
        a = _make_unit('a')
        b = _make_unit('b')

        result = RetrievalEngine._compute_entity_jaccard([a, b])

        assert abs(result[(a.id, b.id)] - 0.0) < 1e-9

    def test_partial_overlap(self):
        """Partial overlap computes correctly."""
        shared = uuid4()
        a = _make_unit('a', entity_ids=[shared, uuid4()])
        b = _make_unit('b', entity_ids=[shared, uuid4()])

        result = RetrievalEngine._compute_entity_jaccard([a, b])

        # 1 shared / 3 total = 0.333...
        assert abs(result[(a.id, b.id)] - 1 / 3) < 1e-9
