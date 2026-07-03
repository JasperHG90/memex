"""Unit tests for F10 surprise score (memex_core.memory.lint_llm.surprise).

DB queries are mocked at the AsyncSession boundary; the F2 corrector is the
real :class:`AnisotropyCorrector` so we test the wrapper behaviour, not the
corrector internals (those have their own unit tests under
``tests/unit/memory/models/test_anisotropy.py``).

Substantive integration coverage (corpus-level surprise separation, polarity
limit pin) lives in ``tests/integration/services/test_int_f10_lint_llm.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from memex_core.memory.lint_llm.surprise import (
    DEFAULT_K,
    compute_unit_surprise,
    warm_corrector,
)
from memex_core.memory.models.anisotropy import AnisotropyCorrector


def _mock_session_with_sims(sims: list[float]) -> AsyncMock:
    """Build an AsyncSession mock whose connection.execute returns sims as rows."""
    rows = [MagicMock(sim=s) for s in sims]
    result = MagicMock()
    result.__iter__ = lambda self: iter(rows)
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=result)
    session = AsyncMock()
    session.connection = AsyncMock(return_value=conn)
    return session


class TestComputeUnitSurpriseSparseNeighborhood:
    """A vault with fewer than k peers returns 1.0 (maximally surprising)."""

    @pytest.mark.asyncio
    async def test_zero_peers_returns_one(self):
        session = _mock_session_with_sims([])
        score = await compute_unit_surprise(uuid4(), uuid4(), session, k=8)
        assert score == 1.0

    @pytest.mark.asyncio
    async def test_below_k_returns_one(self):
        session = _mock_session_with_sims([0.9, 0.8, 0.85])
        score = await compute_unit_surprise(uuid4(), uuid4(), session, k=8)
        assert score == 1.0

    @pytest.mark.asyncio
    async def test_exactly_k_does_not_short_circuit(self):
        sims = [0.85] * DEFAULT_K
        corrector = AnisotropyCorrector(min_samples=2, epsilon=1e-8)
        for v in [0.85] * 5:
            corrector.normalize(v)
        session = _mock_session_with_sims(sims)
        score = await compute_unit_surprise(
            uuid4(), uuid4(), session, k=DEFAULT_K, corrector=corrector
        )
        assert score != 1.0


class TestComputeUnitSurpriseRange:
    """Returned surprise score lies in [0, 1] for any valid input."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        'sims',
        [
            [0.99] * 8,
            [0.5] * 8,
            [0.01] * 8,
            [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8],
        ],
    )
    async def test_score_in_range(self, sims):
        corrector = AnisotropyCorrector(min_samples=2, epsilon=1e-8)
        for v in [0.5, 0.6, 0.7, 0.8, 0.9]:
            corrector.normalize(v)
        session = _mock_session_with_sims(sims)
        score = await compute_unit_surprise(uuid4(), uuid4(), session, k=8, corrector=corrector)
        assert 0.0 <= score <= 1.0


class TestComputeUnitSurpriseCorrectorSemantics:
    """Surprise = 1 - mean(corrected_sims); higher sims → lower surprise."""

    @pytest.mark.asyncio
    async def test_high_similarity_yields_low_surprise(self):
        """Unit that fits its vault (high sims) → low surprise."""
        corrector = AnisotropyCorrector(min_samples=2, epsilon=1e-8)
        for v in [0.5, 0.55, 0.6]:  # tight low cluster
            corrector.normalize(v)
        sims = [0.95] * 8  # genuinely high → corrected output > 0.5 → surprise < 0.5
        session = _mock_session_with_sims(sims)
        score = await compute_unit_surprise(uuid4(), uuid4(), session, k=8, corrector=corrector)
        assert score < 0.5

    @pytest.mark.asyncio
    async def test_low_similarity_yields_high_surprise(self):
        """Unit anomalous to its vault (low sims) → high surprise."""
        corrector = AnisotropyCorrector(min_samples=2, epsilon=1e-8)
        for v in [0.85, 0.88, 0.90]:  # tight high cluster
            corrector.normalize(v)
        sims = [0.3] * 8  # genuinely low → corrected < 0.5 → surprise > 0.5
        session = _mock_session_with_sims(sims)
        score = await compute_unit_surprise(uuid4(), uuid4(), session, k=8, corrector=corrector)
        assert score > 0.5

    @pytest.mark.asyncio
    async def test_passthrough_corrector_inverts_mean_sim(self):
        """Cold-start corrector (passthrough) → surprise = 1 - mean(sims)."""
        corrector = AnisotropyCorrector(window_size=0)  # disabled, passthrough
        sims = [0.8] * 8
        session = _mock_session_with_sims(sims)
        score = await compute_unit_surprise(uuid4(), uuid4(), session, k=8, corrector=corrector)
        assert abs(score - 0.2) < 1e-9


class TestComputeUnitSurpriseQueryShape:
    """Verify the SQL is called with the right parameters."""

    @pytest.mark.asyncio
    async def test_query_called_with_unit_vault_k(self):
        session = _mock_session_with_sims([0.8] * 8)
        unit_id = uuid4()
        vault_id = uuid4()
        await compute_unit_surprise(unit_id, vault_id, session, k=12)
        conn = await session.connection()
        conn.execute.assert_awaited_once()
        _stmt, params = conn.execute.await_args.args
        assert params == {
            'unit_id': str(unit_id),
            'vault_id': str(vault_id),
            'k': 12,
        }


class TestWarmCorrector:
    """warm_corrector feeds N similarities into the shared corrector."""

    @pytest.mark.asyncio
    async def test_returns_count_fed(self):
        sims = [0.85, 0.86, 0.84, 0.87, 0.83]
        session = _mock_session_with_sims(sims)
        corrector = AnisotropyCorrector(min_samples=2)
        fed = await warm_corrector(session, uuid4(), target_observations=5, corrector=corrector)
        assert fed == 5
        assert corrector.count == 5

    @pytest.mark.asyncio
    async def test_query_uses_target_observations_as_limit(self):
        session = _mock_session_with_sims([0.85] * 256)
        await warm_corrector(session, uuid4(), target_observations=256)
        conn = await session.connection()
        conn.execute.assert_awaited_once()
        _stmt, params = conn.execute.await_args.args
        assert params['limit'] == 256

    @pytest.mark.asyncio
    async def test_zero_rows_returns_zero(self):
        session = _mock_session_with_sims([])
        fed = await warm_corrector(session, uuid4(), target_observations=10)
        assert fed == 0
