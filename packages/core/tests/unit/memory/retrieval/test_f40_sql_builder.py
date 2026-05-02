"""F40 — pinning tests for the pre-reranker filter SQL builder.

These tests lock down two invariants the round-3 review settled:

1. **Python-level conditional, NOT SQL-side runtime flag.** When the FSFM
   branch is disabled (default — until F11 ships the migration adding
   ``importance`` / ``stability`` / ``last_outcome_at`` columns), the
   generated SQL must NOT reference those column names at all. A SQL-side
   ``(NOT :fsfm_enabled OR ...)`` guard would crash at parse time on the
   missing columns.

2. **NULL-handling — branch-result COALESCE, not column-level.** When the
   FSFM branch is enabled, the inner expression must be wrapped in
   ``COALESCE(..., FALSE)`` so SQL three-valued logic (``NULL OR FALSE
   -> NULL``) does not flip the surrounding ``NOT (...)`` and exclude
   cold-start rows.

The third invariant tested here is the bypass: when ``apply_pre_filter``
is False, the builder returns ``None`` so the whole ``WHERE NOT (...)``
clause drops out — every branch bypassed in one go.
"""

from __future__ import annotations

import pytest

from memex_core.memory.retrieval.engine import (
    STABILITY_SECONDS_PER_DAY,
    _build_pre_filter_clause,
)


class TestF40PreFilterBuilder:
    def test_fsfm_disabled_omits_fsfm_columns(self) -> None:
        """With ``fsfm_branch_enabled=False`` the generated SQL must not
        reference any of the F11-owned columns. SQL-side runtime flags
        would still parse-time crash because the columns do not exist
        until F11's migration runs."""
        clause = _build_pre_filter_clause(
            apply_pre_filter=True,
            fsfm_branch_enabled=False,
        )
        assert clause is not None
        assert 'success_co_count' in clause
        assert 'failure_co_count' in clause
        assert 'importance' not in clause, (
            'F11 column leaked into F40-only SQL — SQL-side runtime flag regression. '
            'The FSFM branch must be omitted via a Python-level conditional, not '
            'guarded by a runtime SQL clause.'
        )
        assert 'stability' not in clause
        assert 'last_outcome_at' not in clause

    def test_fsfm_enabled_emits_coalesce_branch(self) -> None:
        """With ``fsfm_branch_enabled=True`` the FSFM expression must be
        wrapped in ``COALESCE(..., FALSE)`` at the BRANCH level (round-3
        NULL-handling fix). Column-level COALESCE would mask real values
        with synthetic defaults."""
        clause = _build_pre_filter_clause(
            apply_pre_filter=True,
            fsfm_branch_enabled=True,
        )
        assert clause is not None

        assert 'success_co_count' in clause
        assert 'importance' in clause
        assert 'stability' in clause
        assert 'last_outcome_at' in clause

        assert 'COALESCE' in clause, 'FSFM branch must be wrapped in COALESCE(..., FALSE)'

        coalesce_idx = clause.find('COALESCE')
        importance_idx = clause.find('importance')
        assert coalesce_idx < importance_idx, (
            'COALESCE must wrap the FSFM expression (importance * exp(...) < 0.10), '
            'not appear after it'
        )

        assert 'COALESCE(memory_units.stability,' not in clause, (
            'Column-level COALESCE on stability detected. The round-3 review '
            'settled on BRANCH-level COALESCE only — wrapping individual columns '
            'masks real values with synthetic defaults.'
        )
        assert 'COALESCE(memory_units.last_outcome_at,' not in clause
        # The COALESCE wrap is allowed on the FSFM branch result (which starts
        # with ``memory_units.importance * exp(...)``) — that's the round-3
        # NULL-handling fix. What's forbidden is column-level fallback like
        # ``COALESCE(stability, 1.0)`` that masks real values.
        assert ', 1.0)' not in clause
        assert ', 0.5)' not in clause

        # The unit-conversion divisor must come from the named constant.
        assert str(STABILITY_SECONDS_PER_DAY) in clause

        # NULLIF guards zero-stability rows from filtering (degenerate state
        # that observability surfaces).
        assert 'NULLIF(memory_units.stability, 0)' in clause

    def test_apply_pre_filter_false_returns_none(self) -> None:
        """``apply_pre_filter=False`` drops the entire ``WHERE NOT (...)``
        clause — every branch (MW, FSFM, future confidence) bypassed in
        one go. Single flag because the three signals share one cognitive
        model ('things the system normally hides')."""
        for fsfm in (False, True):
            clause = _build_pre_filter_clause(
                apply_pre_filter=False,
                fsfm_branch_enabled=fsfm,
            )
            assert clause is None, (
                f'apply_pre_filter=False must drop entire WHERE NOT (...) clause '
                f'(fsfm_branch_enabled={fsfm}); got {clause!r}'
            )

    def test_branches_or_joined(self) -> None:
        """Branches are OR'd — either signal is sufficient grounds to skip
        the cross-encoder. AND'ing would underprune (the spec is
        emphatic on this)."""
        clause = _build_pre_filter_clause(
            apply_pre_filter=True,
            fsfm_branch_enabled=True,
        )
        assert clause is not None
        assert ' OR ' in clause
        assert ' AND COALESCE' not in clause

    def test_mw_threshold_is_five_outcomes(self) -> None:
        """Cold-start safeguard: the MW branch is gated on
        ``>= 5 outcomes`` so zero-outcome units never get pruned."""
        clause = _build_pre_filter_clause(
            apply_pre_filter=True,
            fsfm_branch_enabled=False,
        )
        assert clause is not None
        assert '>= 5' in clause
        assert '< 0.15' in clause

    def test_mw_uses_beta_bernoulli_closed_form(self) -> None:
        """``mw_score = (succ + 1) / (succ + fail + 2)`` — the
        Beta-Bernoulli α=β=1 posterior mean. Derived inline; there is
        no ``mw_score`` column."""
        clause = _build_pre_filter_clause(
            apply_pre_filter=True,
            fsfm_branch_enabled=False,
        )
        assert clause is not None
        assert '(memory_units.success_co_count + 1.0)' in clause
        assert '(memory_units.success_co_count + memory_units.failure_co_count + 2.0)' in clause


class TestStabilitySecondsPerDayConstant:
    def test_constant_is_named_and_value_matches(self) -> None:
        """Magic-number documentation: STABILITY_SECONDS_PER_DAY = 86400.
        If F11 ever changes ``stability``'s unit convention, this divisor
        must change in lockstep — pulling the literal from a named
        constant makes that a one-edit."""
        assert STABILITY_SECONDS_PER_DAY == 86400.0


@pytest.mark.parametrize(
    ('apply_pre_filter', 'fsfm_branch_enabled', 'expect_none'),
    [
        (False, False, True),
        (False, True, True),
        (True, False, False),
        (True, True, False),
    ],
)
def test_builder_truth_table(
    apply_pre_filter: bool, fsfm_branch_enabled: bool, expect_none: bool
) -> None:
    clause = _build_pre_filter_clause(
        apply_pre_filter=apply_pre_filter,
        fsfm_branch_enabled=fsfm_branch_enabled,
    )
    if expect_none:
        assert clause is None
    else:
        assert clause is not None
        assert 'success_co_count' in clause
