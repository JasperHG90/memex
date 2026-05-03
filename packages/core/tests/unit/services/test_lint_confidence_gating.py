"""F22 — confidence/variance gate for F6 + F10 lint findings.

Tests the pure-Python predicate ``_gate_blocks_finding`` and the gate
config defaults from ``LintConfidenceGate``.
"""

from __future__ import annotations

import math
from unittest.mock import AsyncMock, MagicMock

import pytest

from memex_common.config import LintConfidenceGate
from memex_core.memory.confidence import MAX_VARIANCE


class TestGateConfigDefaults:
    """Pre-F22 behaviour preserved by default: confidence_min=0, variance_max=MAX_VARIANCE."""

    def test_default_confidence_min_is_zero(self) -> None:
        gate = LintConfidenceGate()
        assert gate.confidence_min == 0.0

    def test_default_variance_max_is_max_variance(self) -> None:
        gate = LintConfidenceGate()
        assert math.isclose(gate.variance_max, MAX_VARIANCE, rel_tol=1e-9)

    def test_variance_max_capped_at_one_twelfth(self) -> None:
        # Field has le=1/12 — instantiation with a higher value rejects.
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            LintConfidenceGate(variance_max=0.2)

    def test_confidence_min_capped_at_one(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            LintConfidenceGate(confidence_min=1.5)


class TestGatePredicate:
    """``_gate_blocks_finding`` returns True iff the unit fails the gate."""

    @pytest.mark.asyncio
    async def test_cold_start_blocked_when_variance_max_strict(self) -> None:
        """Cold-start unit (count=0, variance=1/12) is blocked when variance_max < 1/12."""
        from memex_core.services.lint import _gate_blocks_finding

        session = MagicMock()
        result = MagicMock()
        result.first.return_value = (1.0, 0)
        session.execute = AsyncMock(return_value=result)

        # variance_max = 0.05 < MAX_VARIANCE (1/12 ≈ 0.0833) → cold-start blocked.
        blocked = await _gate_blocks_finding(
            session, 'unit-id', confidence_min=0.0, variance_max=0.05
        )
        assert blocked is True

    @pytest.mark.asyncio
    async def test_well_evidenced_low_confidence_passes_default_gate(self) -> None:
        """Well-evidenced low-confidence unit surfaces (default gate is permissive)."""
        from memex_core.services.lint import _gate_blocks_finding

        session = MagicMock()
        result = MagicMock()
        result.first.return_value = (0.3, 20)
        session.execute = AsyncMock(return_value=result)

        blocked = await _gate_blocks_finding(
            session, 'unit-id', confidence_min=0.0, variance_max=MAX_VARIANCE
        )
        assert blocked is False

    @pytest.mark.asyncio
    async def test_low_confidence_blocked_by_min(self) -> None:
        from memex_core.services.lint import _gate_blocks_finding

        session = MagicMock()
        result = MagicMock()
        result.first.return_value = (0.2, 20)
        session.execute = AsyncMock(return_value=result)

        blocked = await _gate_blocks_finding(
            session, 'unit-id', confidence_min=0.3, variance_max=MAX_VARIANCE
        )
        assert blocked is True

    @pytest.mark.asyncio
    async def test_missing_unit_row_does_not_block(self) -> None:
        """If the unit row is missing, do not block — fall through (no row to gate on)."""
        from memex_core.services.lint import _gate_blocks_finding

        session = MagicMock()
        result = MagicMock()
        result.first.return_value = None
        session.execute = AsyncMock(return_value=result)

        blocked = await _gate_blocks_finding(
            session, 'unit-id', confidence_min=0.5, variance_max=0.001
        )
        assert blocked is False

    @pytest.mark.asyncio
    async def test_well_evidenced_high_confidence_passes_strict_gate(self) -> None:
        """High-confidence + low-variance unit surfaces even under strict gates."""
        from memex_core.services.lint import _gate_blocks_finding

        session = MagicMock()
        result = MagicMock()
        result.first.return_value = (1.0, 20)
        session.execute = AsyncMock(return_value=result)

        blocked = await _gate_blocks_finding(
            session, 'unit-id', confidence_min=0.5, variance_max=0.005
        )
        assert blocked is False


class TestBulkConfidenceLoad:
    """``_bulk_load_confidence_map`` + ``_confidence_map_blocks`` (Hermes round-1 HIGH).

    Replaces the per-row SELECT in ``_run_one`` so a rule with N candidates
    issues exactly one extra query — not N.
    """

    @pytest.mark.asyncio
    async def test_bulk_load_empty_input_returns_empty_map(self) -> None:
        from memex_core.services.lint import _bulk_load_confidence_map

        session = MagicMock()
        session.execute = AsyncMock()

        out = await _bulk_load_confidence_map(session, [])
        assert out == {}
        # Critical: zero queries when nothing to fetch.
        session.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_bulk_load_single_query_for_many_ids(self) -> None:
        from memex_core.services.lint import _bulk_load_confidence_map

        session = MagicMock()
        rows = [
            {'unit_id': 'a', 'confidence': 0.9, 'confidence_evidence_count': 10},
            {'unit_id': 'b', 'confidence': 0.2, 'confidence_evidence_count': 0},
            {'unit_id': 'c', 'confidence': None, 'confidence_evidence_count': None},
        ]
        result = MagicMock()
        result.mappings.return_value.all.return_value = rows
        session.execute = AsyncMock(return_value=result)

        out = await _bulk_load_confidence_map(session, ['a', 'b', 'c'])

        # Exactly one query regardless of input size.
        assert session.execute.call_count == 1
        assert out['a'] == (0.9, 10)
        assert out['b'] == (0.2, 0)
        # NULL fallback parity with the per-row path: confidence None → 1.0,
        # evidence_count None → 0.
        assert out['c'] == (1.0, 0)

    def test_confidence_map_blocks_missing_id_does_not_block(self) -> None:
        from memex_core.services.lint import _confidence_map_blocks

        # Parity with _gate_blocks_finding: missing row means "do not block".
        assert _confidence_map_blocks({}, 'nope', confidence_min=0.5, variance_max=0.001) is False

    def test_confidence_map_blocks_low_confidence(self) -> None:
        from memex_core.services.lint import _confidence_map_blocks

        cmap = {'u1': (0.2, 20)}
        assert (
            _confidence_map_blocks(cmap, 'u1', confidence_min=0.3, variance_max=MAX_VARIANCE)
            is True
        )

    def test_confidence_map_blocks_high_variance(self) -> None:
        from memex_core.services.lint import _confidence_map_blocks

        # Cold-start (count=0) → variance=1/12. Strict variance_max → blocked.
        cmap = {'u1': (1.0, 0)}
        assert _confidence_map_blocks(cmap, 'u1', confidence_min=0.0, variance_max=0.05) is True

    def test_confidence_map_blocks_well_evidenced_passes(self) -> None:
        from memex_core.services.lint import _confidence_map_blocks

        cmap = {'u1': (1.0, 20)}
        assert _confidence_map_blocks(cmap, 'u1', confidence_min=0.5, variance_max=0.005) is False
