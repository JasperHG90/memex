"""Unit tests for contradiction dedup — emission-time UUID normalization.

When the ``make_semantic_contradiction_check`` factory's inner ``_check``
coroutine detects a contradiction between units A and B, it normalizes the
finding to prevent duplicate A↔B / B↔A pairs.  The rule:

- Keep only cited_unit_ids whose string UUID is lexicographically GREATER
  than the target ``unit_id``.
- If all cited units are smaller → suppress the finding (return None).

This means exactly one direction of the pair survives: the check whose
target UUID is the smaller of the two emits the finding; the check with
the larger target UUID is suppressed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

# We test the dedup logic by directly calling the inner coroutine
# produced by ``make_semantic_contradiction_check``.  That requires
# mocking: dspy.LM, the DB helpers, and run_dspy_operation.

import dspy


def _uuid(hex_suffix: str) -> UUID:
    """Build a UUID from a hex suffix, zero-padded."""
    return UUID(hex_suffix.rjust(32, '0'))


# Predictable UUIDs whose string representations have a known ordering.
UUID_SMALL = _uuid('00000001')  # str < UUID_LARGE
UUID_LARGE = _uuid('ffffffff')  # str > UUID_SMALL
UUID_MID = _uuid('88888888')


class _MockPrediction:
    """Fake DSPy prediction returned by ``run_dspy_operation``."""

    def __init__(
        self,
        has_contradiction: bool,
        indices: list[int] | None = None,
        explanation: str = 'test',
    ) -> None:
        self.has_contradiction = has_contradiction
        self.contradiction_with_unit_indices = indices or []
        self.explanation = explanation


class TestSemanticContradictionDedup:
    """Tests for the UUID normalization inside make_semantic_contradiction_check."""

    @pytest.mark.asyncio
    async def test_finding_emitted_when_unit_id_less_than_cited(self) -> None:
        """When unit_id < cited_unit_id, the finding IS emitted."""
        from memex_core.memory.lint_llm.checks import make_semantic_contradiction_check

        lm = MagicMock(spec=dspy.LM)
        check = make_semantic_contradiction_check(lm, k=3)

        # related_ids: [UUID_LARGE] at index 0.
        related = [(UUID_LARGE, 'text large')]

        with (
            patch(
                'memex_core.memory.lint_llm.checks._load_unit_text',
                new_callable=AsyncMock,
                return_value='unit text',
            ),
            patch(
                'memex_core.memory.lint_llm.checks._load_top_k_related',
                new_callable=AsyncMock,
                return_value=related + [(UUID_MID, 'filler')],  # need >=2 related
            ),
            patch(
                'memex_core.memory.lint_llm.checks.compute_unit_surprise',
                new_callable=AsyncMock,
                return_value=0.9,
            ),
            patch(
                'memex_core.memory.lint_llm.checks._llm.run_dspy_operation',
                new_callable=AsyncMock,
                return_value=_MockPrediction(
                    has_contradiction=True,
                    indices=[0],  # points to UUID_LARGE
                ),
            ),
        ):
            session = AsyncMock()
            vault_id = UUID('00000000-0000-0000-0000-000000000001')
            # UUID_SMALL < UUID_LARGE → finding should be emitted.
            result = await check(UUID_SMALL, vault_id, session)

        assert result is not None
        assert str(UUID_LARGE) in result.related_unit_ids

    @pytest.mark.asyncio
    async def test_finding_suppressed_when_unit_id_greater_than_cited(self) -> None:
        """When unit_id > cited_unit_id, the finding is suppressed (returns None)."""
        from memex_core.memory.lint_llm.checks import make_semantic_contradiction_check

        lm = MagicMock(spec=dspy.LM)
        check = make_semantic_contradiction_check(lm, k=3)

        # related_ids: [UUID_SMALL] at index 0.
        related = [(UUID_SMALL, 'text small'), (UUID_MID, 'filler')]

        with (
            patch(
                'memex_core.memory.lint_llm.checks._load_unit_text',
                new_callable=AsyncMock,
                return_value='unit text',
            ),
            patch(
                'memex_core.memory.lint_llm.checks._load_top_k_related',
                new_callable=AsyncMock,
                return_value=related,
            ),
            patch(
                'memex_core.memory.lint_llm.checks.compute_unit_surprise',
                new_callable=AsyncMock,
                return_value=0.9,
            ),
            patch(
                'memex_core.memory.lint_llm.checks._llm.run_dspy_operation',
                new_callable=AsyncMock,
                return_value=_MockPrediction(
                    has_contradiction=True,
                    indices=[0],  # points to UUID_SMALL
                ),
            ),
        ):
            session = AsyncMock()
            vault_id = UUID('00000000-0000-0000-0000-000000000001')
            # UUID_LARGE > UUID_SMALL → finding should be suppressed.
            result = await check(UUID_LARGE, vault_id, session)

        assert result is None

    @pytest.mark.asyncio
    async def test_multiple_cited_keeps_only_greater_uuids(self) -> None:
        """When multiple cited units, only those with UUID > unit_id are kept."""
        from memex_core.memory.lint_llm.checks import make_semantic_contradiction_check

        lm = MagicMock(spec=dspy.LM)
        check = make_semantic_contradiction_check(lm, k=5)

        # related list: [UUID_SMALL, UUID_LARGE, UUID_MID]
        related = [
            (UUID_SMALL, 'small'),
            (UUID_LARGE, 'large'),
            (UUID_MID, 'mid'),
        ]

        with (
            patch(
                'memex_core.memory.lint_llm.checks._load_unit_text',
                new_callable=AsyncMock,
                return_value='unit text',
            ),
            patch(
                'memex_core.memory.lint_llm.checks._load_top_k_related',
                new_callable=AsyncMock,
                return_value=related,
            ),
            patch(
                'memex_core.memory.lint_llm.checks.compute_unit_surprise',
                new_callable=AsyncMock,
                return_value=0.9,
            ),
            patch(
                'memex_core.memory.lint_llm.checks._llm.run_dspy_operation',
                new_callable=AsyncMock,
                return_value=_MockPrediction(
                    has_contradiction=True,
                    indices=[0, 1, 2],  # all three cited
                ),
            ),
        ):
            session = AsyncMock()
            vault_id = UUID('00000000-0000-0000-0000-000000000001')
            # Target is UUID_MID. UUID_SMALL < UUID_MID → dropped.
            # UUID_LARGE > UUID_MID → kept. UUID_MID == UUID_MID → not kept (< not <=).
            result = await check(UUID_MID, vault_id, session)

        assert result is not None
        # Only UUID_LARGE should survive.
        assert result.related_unit_ids == [str(UUID_LARGE)]

    @pytest.mark.asyncio
    async def test_all_cited_smaller_returns_none(self) -> None:
        """When all cited units have UUID < unit_id, returns None."""
        from memex_core.memory.lint_llm.checks import make_semantic_contradiction_check

        lm = MagicMock(spec=dspy.LM)
        check = make_semantic_contradiction_check(lm, k=3)

        # related: all smaller than UUID_LARGE.
        related = [(UUID_SMALL, 'small'), (UUID_MID, 'mid')]

        with (
            patch(
                'memex_core.memory.lint_llm.checks._load_unit_text',
                new_callable=AsyncMock,
                return_value='unit text',
            ),
            patch(
                'memex_core.memory.lint_llm.checks._load_top_k_related',
                new_callable=AsyncMock,
                return_value=related,
            ),
            patch(
                'memex_core.memory.lint_llm.checks.compute_unit_surprise',
                new_callable=AsyncMock,
                return_value=0.9,
            ),
            patch(
                'memex_core.memory.lint_llm.checks._llm.run_dspy_operation',
                new_callable=AsyncMock,
                return_value=_MockPrediction(
                    has_contradiction=True,
                    indices=[0, 1],
                ),
            ),
        ):
            session = AsyncMock()
            vault_id = UUID('00000000-0000-0000-0000-000000000001')
            result = await check(UUID_LARGE, vault_id, session)

        assert result is None

    @pytest.mark.asyncio
    async def test_no_contradiction_returns_none(self) -> None:
        """When the LLM says no contradiction, returns None regardless of UUIDs."""
        from memex_core.memory.lint_llm.checks import make_semantic_contradiction_check

        lm = MagicMock(spec=dspy.LM)
        check = make_semantic_contradiction_check(lm, k=3)

        related = [(UUID_LARGE, 'large'), (UUID_MID, 'mid')]

        with (
            patch(
                'memex_core.memory.lint_llm.checks._load_unit_text',
                new_callable=AsyncMock,
                return_value='unit text',
            ),
            patch(
                'memex_core.memory.lint_llm.checks._load_top_k_related',
                new_callable=AsyncMock,
                return_value=related,
            ),
            patch(
                'memex_core.memory.lint_llm.checks.compute_unit_surprise',
                new_callable=AsyncMock,
                return_value=0.5,
            ),
            patch(
                'memex_core.memory.lint_llm.checks._llm.run_dspy_operation',
                new_callable=AsyncMock,
                return_value=_MockPrediction(has_contradiction=False),
            ),
        ):
            session = AsyncMock()
            vault_id = UUID('00000000-0000-0000-0000-000000000001')
            result = await check(UUID_SMALL, vault_id, session)

        assert result is None
