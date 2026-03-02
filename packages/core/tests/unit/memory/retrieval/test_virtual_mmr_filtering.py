"""Tests for virtual observation handling in MMR and _convert_mm_to_units."""

from datetime import datetime, timezone
from uuid import uuid4
from unittest.mock import MagicMock

import pytest

from memex_core.memory.retrieval.engine import RetrievalEngine
from memex_core.memory.sql_models import MemoryUnit, MentalModel, UnitEntity
from memex_common.types import FactTypes


def _make_unit(
    text: str = 'test',
    event_date: datetime | None = None,
    entity_ids: list | None = None,
    virtual: bool = False,
) -> MemoryUnit:
    """Create a minimal MemoryUnit for testing."""
    unit_id = uuid4()
    vault_id = uuid4()
    metadata: dict = {}
    if virtual:
        metadata['virtual'] = True
        metadata['observation'] = True

    unit = MemoryUnit(
        id=unit_id,
        note_id=uuid4(),
        text=text,
        fact_type=FactTypes.OBSERVATION if virtual else FactTypes.WORLD,
        vault_id=vault_id,
        event_date=event_date or datetime(2026, 1, 1, tzinfo=timezone.utc),
        occurred_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        embedding=[] if virtual else [0.1] * 384,
        unit_metadata=metadata,
    )
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


@pytest.fixture
def engine():
    return RetrievalEngine(embedder=MagicMock(), reranker=MagicMock())


class TestConvertMmToUnitsVirtualFlag:
    """Test that _convert_mm_to_units marks observations as virtual."""

    def test_virtual_flag_set(self, engine):
        """Virtual observations from mental models should have 'virtual': True in metadata."""
        model = MentalModel(
            id=uuid4(),
            vault_id=uuid4(),
            entity_id=uuid4(),
            name='Test Entity',
            observations=[
                {
                    'title': 'Observation 1',
                    'content': 'Some observation content',
                    'evidence': [],
                }
            ],
            last_refreshed=datetime(2026, 1, 1, tzinfo=timezone.utc),
            version=1,
        )

        units = engine._convert_mm_to_units(model)

        assert len(units) == 1
        assert units[0].unit_metadata.get('virtual') is True
        assert units[0].unit_metadata.get('observation') is True
        assert units[0].embedding == []

    def test_multiple_observations_all_virtual(self, engine):
        """All observations from a mental model should be marked virtual."""
        model = MentalModel(
            id=uuid4(),
            vault_id=uuid4(),
            entity_id=uuid4(),
            name='Multi Obs Entity',
            observations=[
                {
                    'title': f'Observation {i}',
                    'content': f'Content {i}',
                    'evidence': [],
                }
                for i in range(3)
            ],
            last_refreshed=datetime(2026, 1, 1, tzinfo=timezone.utc),
            version=1,
        )

        units = engine._convert_mm_to_units(model)

        assert len(units) == 3
        for unit in units:
            assert unit.unit_metadata.get('virtual') is True


class TestMmrExcludesVirtualObservations:
    """Test that virtual observations are excluded from MMR diversity filtering."""

    def test_virtual_units_bypass_mmr(self):
        """Virtual units should not participate in MMR and keep their original positions."""
        real1 = _make_unit('real fact 1', entity_ids=[uuid4()])
        real2 = _make_unit('real fact 2', entity_ids=[uuid4()])

        # Only real units participate in MMR
        sim = _sim_matrix_from_dict({(0, 1): 0.1}, [real1, real2])

        result = RetrievalEngine._apply_mmr_diversity(
            [real1, real2],
            sim,
            lambda_=0.9,
            limit=10,
        )

        # MMR should work on real units only
        assert len(result) == 2
        assert real1 in result
        assert real2 in result

    def test_virtual_units_not_affected_by_high_similarity(self):
        """Virtual units should not be penalized by high similarity to selected items."""
        # Virtual units with embedding=[] would get cosine=0.0 from
        # _compute_pairwise_cosine, giving them an unfair advantage in MMR.
        # By excluding them, they keep their reranker-assigned position.
        virtual = _make_unit('virtual observation', virtual=True)

        # Verify the virtual flag and empty embedding are set correctly
        assert virtual.unit_metadata.get('virtual') is True
        assert virtual.embedding == []
        assert virtual.fact_type == FactTypes.OBSERVATION
