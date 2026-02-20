import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from uuid import uuid4, UUID
from datetime import datetime, timezone

from memex_core.memory.reflect.reflection import ReflectionEngine
from memex_core.memory.reflect.prompts import ValidatedObservation, NewEvidenceItem
from memex_core.memory.sql_models import Observation, EvidenceItem, Trend
from memex_core.config import MemexConfig


@pytest.fixture
def mock_session():
    from sqlmodel.ext.asyncio.session import AsyncSession

    return AsyncMock(spec=AsyncSession)


@pytest.fixture
def engine(mock_session):
    mock_config = MagicMock(spec=MemexConfig)
    return ReflectionEngine(session=mock_session, config=mock_config, embedder=MagicMock())


@pytest.mark.asyncio
async def test_phase_4_compare_logic(engine):
    # Setup Data
    uuid1 = uuid4()
    uuid2 = uuid4()
    uuid3 = uuid4()  # Only in new

    # Existing Observation with Evidence 1 & 2
    obs_existing = Observation(
        title='Old Obs',
        content='Old Content',
        evidence=[
            EvidenceItem(memory_id=uuid1, quote='Quote 1', timestamp=datetime.now(timezone.utc)),
            EvidenceItem(memory_id=uuid2, quote='Quote 2', timestamp=datetime.now(timezone.utc)),
        ],
    )

    # New Validated Observation with Evidence 2 & 3
    obs_new = ValidatedObservation(
        title='New Obs',
        content='New Content',
        evidence=[
            NewEvidenceItem(
                memory_id=str(uuid2),
                quote='Quote 2 Updated',
                relevance_explanation='Relevance 2',
                timestamp=str(datetime.now(timezone.utc)),
            ),
            NewEvidenceItem(
                memory_id=str(uuid3),
                quote='Quote 3',
                relevance_explanation='Relevance 3',
                timestamp=str(datetime.now(timezone.utc)),
            ),
        ],
    )

    # Mock LLM Response
    with patch(
        'memex_core.memory.reflect.reflection.run_dspy_operation', new_callable=AsyncMock
    ) as mock_run_dspy:
        mock_result = MagicMock()
        # Assume LLM selects index 0 (which maps to the first sorted UUID)
        mock_result.result.observations = [
            ValidatedObservation(
                title='Merged Obs',
                content='Merged Content',
                evidence=[
                    NewEvidenceItem(
                        memory_id='0',
                        quote='Quote 1',
                        relevance_explanation='Exp 1',
                        timestamp=str(datetime.now(timezone.utc)),
                    )
                ],
            )
        ]
        mock_run_dspy.return_value = (mock_result, None)

        engine.lm = MagicMock()  # Needs to be set

        # Execute
        final_obs = await engine._phase_4_compare(existing=[obs_existing], new_obs=[obs_new])

        # Verify Input to LLM
        call_args = mock_run_dspy.call_args
        input_kwargs = call_args.kwargs['input_kwargs']

        evidence_context = input_kwargs['evidence_context']
        existing_context = input_kwargs['existing_context']
        input_kwargs['new_context']

        # Check evidence context size
        assert len(evidence_context) == 3  # uuid1, uuid2, uuid3

        # Determine sorted order to verify indices
        all_uuids = sorted([str(uuid1), str(uuid2), str(uuid3)])
        uuid1_idx = all_uuids.index(str(uuid1))
        uuid2_idx = all_uuids.index(str(uuid2))
        all_uuids.index(str(uuid3))

        # Check existing context string contains correct indices
        # Indices in list: [uuid1_idx, uuid2_idx] (order depends on original evidence order)
        assert uuid1_idx in existing_context[0].evidence_indices
        assert uuid2_idx in existing_context[0].evidence_indices

        # Check output reconstruction
        assert len(final_obs) == 1
        assert final_obs[0].title == 'Merged Obs'
        assert len(final_obs[0].evidence) == 1

        # The returned evidence index was "0", so it should match the first UUID in sorted list
        expected_uuid = UUID(all_uuids[0])
        assert final_obs[0].evidence[0].memory_id == expected_uuid


@pytest.mark.asyncio
async def test_phase_4_updates_trend_state(engine):
    """
    Verify that _phase_4_compare correctly calculates and updates the 'trend'
    field of the resulting Observation objects.
    """
    from datetime import timedelta

    # Setup Data
    uuid1 = uuid4()

    # Create a timestamp that is definitely "stale" (older than 90 days)
    old_date = datetime.now(timezone.utc) - timedelta(days=100)
    old_date_str = old_date.isoformat()

    # Existing Observation with "Old" Evidence
    obs_existing = Observation(
        title='Old Obs',
        content='Old Content',
        trend=Trend.NEW,  # Currently defaults to NEW
        evidence=[
            EvidenceItem(memory_id=uuid1, quote='Quote 1', timestamp=old_date),
        ],
    )

    # Mock LLM Response to return the SAME observation (preserved)
    # This simulates the LLM deciding to keep the observation.
    with patch(
        'memex_core.memory.reflect.reflection.run_dspy_operation', new_callable=AsyncMock
    ) as mock_run_dspy:
        mock_result = MagicMock()

        # The LLM returns an observation that points to the old evidence
        # We assume index 0 maps to uuid1
        mock_result.result.observations = [
            ValidatedObservation(
                title='Old Obs',
                content='Old Content',
                evidence=[
                    NewEvidenceItem(
                        memory_id='0',
                        quote='Quote 1',
                        relevance_explanation='Still relevant',
                        timestamp=old_date_str,
                    )
                ],
            )
        ]
        mock_run_dspy.return_value = (mock_result, None)

        engine.lm = MagicMock()

        # Execute Phase 4
        # We pass a DUMMY new observation to ensure logic proceeds past the "if not new_obs: return" check.
        # The LLM mock ignores this input anyway and returns the "mock_result" defined above.
        dummy_new = ValidatedObservation(title='Dummy', content='Dummy', evidence=[])
        final_obs = await engine._phase_4_compare(existing=[obs_existing], new_obs=[dummy_new])

        # Verify
        assert len(final_obs) == 1
        obs = final_obs[0]

        # The evidence is purely old (> 90 days), so the trend should be STALE.
        # If the code assumes default (NEW) and doesn't call compute_trend, this will fail.
        assert obs.trend == Trend.STALE, f'Expected Trend.STALE, but got {obs.trend}'
