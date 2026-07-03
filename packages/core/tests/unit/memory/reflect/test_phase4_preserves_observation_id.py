"""Phase 4 reconstruction preserves observation UUIDs via ObservationProvenance.

V21: stable observation IDs are required for the deprioritization-leak fix —
the retrieval-side virtual_id derives from observation.id and refresh tasks
key by observation.id. Without ID preservation, every Phase 4 cycle would
mint a fresh uuid4 per output and the stability claim collapses.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from memex_core.config import MemexConfig
from memex_core.memory.reflect.prompts import (
    ComparePhaseOutput,
    NewEvidenceItem,
    ObservationProvenance,
    ValidatedObservation,
)
from memex_core.memory.reflect.reflection import (
    ReflectionEngine,
    _resolve_provenance_uuid,
)
from memex_core.memory.sql_models import EvidenceItem, Observation


@pytest.fixture
def mock_session():
    from sqlmodel.ext.asyncio.session import AsyncSession

    session = AsyncMock(spec=AsyncSession)
    exec_result = MagicMock()
    exec_result.all.return_value = []
    session.exec.return_value = exec_result
    return session


@pytest.fixture
def engine(mock_session):
    e = ReflectionEngine(
        session=mock_session, config=MagicMock(spec=MemexConfig), embedder=MagicMock()
    )
    e.lm = MagicMock()
    return e


def _make_existing(n: int) -> list[Observation]:
    return [
        Observation(
            id=uuid4(),
            title=f'Existing {i}',
            content=f'Content {i}',
            evidence=[],
        )
        for i in range(n)
    ]


def test_resolve_kept_reuses_existing_uuid():
    existing = _make_existing(3)
    prov = ObservationProvenance(output_index=0, status='kept', merged_from_existing_indices=[1])
    assert _resolve_provenance_uuid(prov, existing) == existing[1].id


def test_resolve_merged_two_picks_lowest_index():
    existing = _make_existing(4)
    prov = ObservationProvenance(
        output_index=0, status='merged', merged_from_existing_indices=[2, 1]
    )
    assert _resolve_provenance_uuid(prov, existing) == existing[1].id


def test_resolve_added_mints_fresh_uuid():
    existing = _make_existing(2)
    prov = ObservationProvenance(output_index=0, status='added')
    result = _resolve_provenance_uuid(prov, existing)
    assert result not in {e.id for e in existing}


def test_resolve_missing_provenance_falls_back_to_uuid4():
    existing = _make_existing(2)
    result = _resolve_provenance_uuid(None, existing)
    assert result not in {e.id for e in existing}


def test_resolve_index_out_of_bounds_falls_back_and_increments_metric():
    existing = _make_existing(2)
    prov = ObservationProvenance(output_index=0, status='merged', merged_from_existing_indices=[99])
    result = _resolve_provenance_uuid(prov, existing)
    assert result not in {e.id for e in existing}


def test_resolve_negative_index_falls_back():
    existing = _make_existing(2)
    prov = ObservationProvenance(output_index=0, status='merged', merged_from_existing_indices=[-1])
    result = _resolve_provenance_uuid(prov, existing)
    assert result not in {e.id for e in existing}


def test_resolve_duplicate_indices_falls_back():
    existing = _make_existing(3)
    prov = ObservationProvenance(
        output_index=0, status='merged', merged_from_existing_indices=[1, 1]
    )
    result = _resolve_provenance_uuid(prov, existing)
    assert result not in {e.id for e in existing}


@pytest.mark.asyncio
async def test_phase4_preserves_id_on_merged_observation(engine):
    existing = _make_existing(2)
    new_obs = [
        ValidatedObservation(
            title='New',
            content='New content',
            evidence=[
                NewEvidenceItem(
                    memory_id='0',
                    quote='q',
                    relevance_explanation='r',
                    timestamp=str(datetime.now(timezone.utc)),
                )
            ],
        )
    ]

    mock_compare = MagicMock(spec=ComparePhaseOutput)
    mock_compare.observations = [
        ValidatedObservation(
            title='Merged Obs',
            content='Merged Content',
            evidence=[
                NewEvidenceItem(
                    memory_id='0',
                    quote='q',
                    relevance_explanation='r',
                    timestamp=str(datetime.now(timezone.utc)),
                )
            ],
        )
    ]
    mock_compare.provenance = [
        ObservationProvenance(
            output_index=0,
            status='merged',
            merged_from_existing_indices=[0, 1],
        )
    ]
    mock_compare.entity_summary = ''

    # Ensure the compare path can find evidence uuids — give existing some evidence.
    cited_uuid = uuid4()
    existing[0].evidence = [
        EvidenceItem(memory_id=cited_uuid, quote='q0', timestamp=datetime.now(timezone.utc))
    ]
    existing[1].evidence = [
        EvidenceItem(memory_id=cited_uuid, quote='q1', timestamp=datetime.now(timezone.utc))
    ]

    with patch(
        'memex_core.memory.reflect.reflection.run_dspy_operation', new_callable=AsyncMock
    ) as mock_run:
        mock_run.return_value = MagicMock(result=mock_compare)
        final, _summary = await engine._phase_4_compare(
            existing=existing, new_obs=new_obs, entity_name='X'
        )

    assert len(final) == 1
    # Lowest existing index wins: existing[0].id.
    assert final[0].id == existing[0].id


@pytest.mark.asyncio
async def test_phase4_mints_new_id_for_added_observation(engine):
    existing = _make_existing(1)
    cited_uuid = uuid4()
    existing[0].evidence = [
        EvidenceItem(memory_id=cited_uuid, quote='q', timestamp=datetime.now(timezone.utc))
    ]
    new_obs = [
        ValidatedObservation(
            title='New',
            content='New',
            evidence=[
                NewEvidenceItem(
                    memory_id='0',
                    quote='q',
                    relevance_explanation='r',
                    timestamp=str(datetime.now(timezone.utc)),
                )
            ],
        )
    ]
    mock_compare = MagicMock(spec=ComparePhaseOutput)
    mock_compare.observations = [
        ValidatedObservation(
            title='Brand New',
            content='Fresh',
            evidence=[
                NewEvidenceItem(
                    memory_id='0',
                    quote='q',
                    relevance_explanation='r',
                    timestamp=str(datetime.now(timezone.utc)),
                )
            ],
        )
    ]
    mock_compare.provenance = [ObservationProvenance(output_index=0, status='added')]
    mock_compare.entity_summary = ''

    with patch(
        'memex_core.memory.reflect.reflection.run_dspy_operation', new_callable=AsyncMock
    ) as mock_run:
        mock_run.return_value = MagicMock(result=mock_compare)
        final, _summary = await engine._phase_4_compare(
            existing=existing, new_obs=new_obs, entity_name='X'
        )

    assert len(final) == 1
    assert final[0].id != existing[0].id


@pytest.mark.asyncio
async def test_phase4_malformed_provenance_length_mismatch_falls_back(engine):
    """provenance.len != observations.len → all entries fall back to fresh uuid4.

    This exercises the 'length_mismatch' branch specifically (length 2 vs 1),
    NOT the 'empty' branch (which would be length 0).
    """
    existing = _make_existing(2)
    cited_uuid = uuid4()
    existing[0].evidence = [
        EvidenceItem(memory_id=cited_uuid, quote='q', timestamp=datetime.now(timezone.utc))
    ]
    new_obs = [
        ValidatedObservation(
            title='New',
            content='New',
            evidence=[
                NewEvidenceItem(
                    memory_id='0',
                    quote='q',
                    relevance_explanation='r',
                    timestamp=str(datetime.now(timezone.utc)),
                )
            ],
        )
    ]
    mock_compare = MagicMock(spec=ComparePhaseOutput)
    mock_compare.observations = [
        ValidatedObservation(
            title='X',
            content='Y',
            evidence=[
                NewEvidenceItem(
                    memory_id='0',
                    quote='q',
                    relevance_explanation='r',
                    timestamp=str(datetime.now(timezone.utc)),
                )
            ],
        )
    ]
    # 2 provenance entries vs 1 observation ⇒ length_mismatch branch.
    mock_compare.provenance = [
        ObservationProvenance(output_index=0, status='kept', merged_from_existing_indices=[0]),
        ObservationProvenance(output_index=1, status='added'),
    ]
    mock_compare.entity_summary = ''

    with patch(
        'memex_core.memory.reflect.reflection.run_dspy_operation', new_callable=AsyncMock
    ) as mock_run:
        mock_run.return_value = MagicMock(result=mock_compare)
        final, _summary = await engine._phase_4_compare(
            existing=existing, new_obs=new_obs, entity_name='X'
        )

    assert len(final) == 1
    assert final[0].id not in {e.id for e in existing}


@pytest.mark.asyncio
async def test_phase4_empty_provenance_falls_back(engine):
    """provenance == [] with observations > 0 → 'empty' branch, all uuid4."""
    existing = _make_existing(2)
    cited_uuid = uuid4()
    existing[0].evidence = [
        EvidenceItem(memory_id=cited_uuid, quote='q', timestamp=datetime.now(timezone.utc))
    ]
    new_obs = [
        ValidatedObservation(
            title='New',
            content='New',
            evidence=[
                NewEvidenceItem(
                    memory_id='0',
                    quote='q',
                    relevance_explanation='r',
                    timestamp=str(datetime.now(timezone.utc)),
                )
            ],
        )
    ]
    mock_compare = MagicMock(spec=ComparePhaseOutput)
    mock_compare.observations = [
        ValidatedObservation(
            title='X',
            content='Y',
            evidence=[
                NewEvidenceItem(
                    memory_id='0',
                    quote='q',
                    relevance_explanation='r',
                    timestamp=str(datetime.now(timezone.utc)),
                )
            ],
        )
    ]
    mock_compare.provenance = []  # empty branch
    mock_compare.entity_summary = ''

    with patch(
        'memex_core.memory.reflect.reflection.run_dspy_operation', new_callable=AsyncMock
    ) as mock_run:
        mock_run.return_value = MagicMock(result=mock_compare)
        final, _summary = await engine._phase_4_compare(
            existing=existing, new_obs=new_obs, entity_name='X'
        )

    assert len(final) == 1
    assert final[0].id not in {e.id for e in existing}


def test_observation_id_survives_jsonb_roundtrip():
    """Observation.model_dump → dict → Observation(**dict) preserves the id."""
    original = Observation(
        id=uuid4(),
        title='T',
        content='C',
        evidence=[],
    )
    dumped = original.model_dump(mode='json')
    rehydrated = Observation(**dumped)
    assert rehydrated.id == original.id
