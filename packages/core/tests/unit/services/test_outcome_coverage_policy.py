"""Coverage policy semantics for `record_outcome`.

`coverage_ratio = reported / retrieved` is now recorded raw (no clamp), and
strict mode rejects BOTH over- and under-coverage so the audit log is
faithful — partial classifications are an integrity bug, not a soft signal.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from memex_core.services.outcomes import OutcomeService, UnitOutcome


def _make_units(n: int) -> list[UnitOutcome]:
    return [UnitOutcome(unit_id=uuid4(), verb='helpful', reason='r') for _ in range(n)]


@pytest.mark.asyncio
async def test_outcome_coverage_ratio_over_one_in_permissive_mode_warns():
    """Permissive mode tolerates ratio > 1.0 but emits a structured warning."""
    svc = OutcomeService()
    session = AsyncMock()
    units = _make_units(3)
    with patch('memex_core.services.outcomes.logger') as mock_logger:
        mock_logger.bind.return_value = mock_logger
        try:
            await svc.record_outcome(
                session=session,
                units=units,
                vault_id=str(uuid4()),
                retrieved_set_size=2,
                coverage_check_mode='permissive',
            )
        except Exception:
            # Downstream DB ops are mocked and may raise; the over-coverage
            # warning is emitted BEFORE the SQL fan-out, so we still assert.
            pass

    warning_calls = [
        c for c in mock_logger.warning.call_args_list if c.args and 'over_one' in c.args[0]
    ]
    assert warning_calls, 'expected an outcome.coverage_over_one warning'


@pytest.mark.asyncio
async def test_outcome_coverage_strict_rejects_over_coverage():
    """Strict mode rejects len(units) > retrieved_set_size."""
    svc = OutcomeService()
    session = AsyncMock()
    units = _make_units(3)
    with pytest.raises(ValueError, match='Strict coverage'):
        await svc.record_outcome(
            session=session,
            units=units,
            vault_id=str(uuid4()),
            retrieved_set_size=2,
            coverage_check_mode='strict',
        )


@pytest.mark.asyncio
async def test_outcome_coverage_strict_rejects_under_coverage():
    """Strict mode continues to reject partial classification."""
    svc = OutcomeService()
    session = AsyncMock()
    units = _make_units(1)
    with pytest.raises(ValueError, match='Strict coverage'):
        await svc.record_outcome(
            session=session,
            units=units,
            vault_id=str(uuid4()),
            retrieved_set_size=3,
            coverage_check_mode='strict',
        )


@pytest.mark.asyncio
async def test_outcome_coverage_strict_accepts_exact_match():
    """Strict mode accepts len(units) == retrieved_set_size (no raise on guard)."""
    svc = OutcomeService()
    session = AsyncMock()
    units = _make_units(2)
    try:
        await svc.record_outcome(
            session=session,
            units=units,
            vault_id=str(uuid4()),
            retrieved_set_size=2,
            coverage_check_mode='strict',
        )
    except ValueError as exc:
        if 'Strict coverage' in str(exc):
            pytest.fail(f'Exact match should not raise the coverage guard: {exc}')
    except Exception:
        # Other DB-mock-related failures are expected; this test only pins
        # that the strict-coverage guard does not reject exact matches.
        pass
