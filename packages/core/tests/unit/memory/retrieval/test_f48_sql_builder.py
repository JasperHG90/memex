"""F48 — pinning tests for the confidence pre-filter branch.

F48 extends F40's ``WHERE NOT (...)`` predicate with a third OR'd branch:
``memory_units.confidence < 0.2``. The branch is always active (not gated
by a config flag) because the column ships with a ``NOT NULL DEFAULT 1.0``
constraint — cold-start units never match. The ``apply_pre_filter`` flag
inherited from F40 is the single bypass for all three branches.

Round-3 review settled the strict ``<`` semantics: α-stepping in the
contradiction engine IS the evidence accumulation, so a count-gate would
double-count. Reaching ``confidence < 0.2`` already requires multiple
contradiction events.

These tests pin:

1. The substring ``confidence < 0.2`` appears in the rendered SQL when
   ``apply_pre_filter=True`` (regardless of FSFM flag — the branch is
   always-on).
2. The substring is absent when ``apply_pre_filter=False`` (entire clause
   drops out via the F40 single-bypass flag).
3. No COALESCE wrap is emitted around the confidence branch — column is
   NOT NULL so SQL three-valued logic does not arise.
4. Pre-merge sequencing: F40 must be merged before F48 — assert
   ``apply_pre_filter`` exists on ``RetrievalRequest``.
"""

from __future__ import annotations

from memex_core.memory.retrieval.engine import _build_pre_filter_clause
from memex_core.memory.retrieval.models import RetrievalRequest


# Pre-merge sequencing gate (module-level — fails import if F40 not merged).
assert (
    hasattr(RetrievalRequest, 'model_fields')
    and 'apply_pre_filter' in RetrievalRequest.model_fields
), (
    'F48 requires F40 to be merged on the base branch — '
    "RetrievalRequest must expose the 'apply_pre_filter' field."
)


class TestF48ConfidenceBranch:
    def test_confidence_branch_in_sql_when_apply_pre_filter_true_fsfm_off(self) -> None:
        """The confidence branch is always-on — no config flag gates it."""
        clause = _build_pre_filter_clause(
            apply_pre_filter=True,
            fsfm_branch_enabled=False,
        )
        assert clause is not None
        sql = str(clause)
        assert 'memory_units.confidence < 0.2' in sql, (
            f'F48 regression: confidence branch missing from rendered SQL when '
            f'apply_pre_filter=True. Got: {sql!r}'
        )

    def test_confidence_branch_in_sql_when_fsfm_on(self) -> None:
        """F48 composes alongside the FSFM branch; both must be present."""
        clause = _build_pre_filter_clause(
            apply_pre_filter=True,
            fsfm_branch_enabled=True,
        )
        assert clause is not None
        sql = str(clause)
        assert 'memory_units.confidence < 0.2' in sql
        assert 'importance' in sql
        assert 'stability' in sql

    def test_confidence_branch_absent_when_apply_pre_filter_false(self) -> None:
        """F40's single-bypass flag drops every branch (MW + FSFM + F48)
        in one go — the entire ``WHERE NOT (...)`` clause is omitted."""
        for fsfm in (False, True):
            clause = _build_pre_filter_clause(
                apply_pre_filter=False,
                fsfm_branch_enabled=fsfm,
            )
            assert clause is None, (
                f'apply_pre_filter=False must drop the entire predicate '
                f'(fsfm_branch_enabled={fsfm}); got {clause!r}'
            )

    def test_confidence_branch_or_joined(self) -> None:
        """The third branch joins via ``OR`` — either signal is sufficient
        grounds to skip the cross-encoder."""
        clause = _build_pre_filter_clause(
            apply_pre_filter=True,
            fsfm_branch_enabled=False,
        )
        assert clause is not None
        sql = str(clause)
        assert ' OR memory_units.confidence < 0.2' in sql, (
            f'F48 branch must be OR-joined to the predicate; got {sql!r}'
        )

    def test_confidence_branch_uses_strict_less_than(self) -> None:
        """Round-2 review settled strict ``<`` (not ``<=``) so the 0.2
        boundary stays on the kept side. A unit with confidence==0.2
        survives the filter."""
        clause = _build_pre_filter_clause(
            apply_pre_filter=True,
            fsfm_branch_enabled=False,
        )
        assert clause is not None
        sql = str(clause)
        assert 'confidence < 0.2' in sql
        assert 'confidence <= 0.2' not in sql, (
            'F48 must use strict < (not <=); the 0.2 boundary is kept.'
        )

    def test_confidence_branch_has_no_coalesce_wrap(self) -> None:
        """``confidence`` is NOT NULL DEFAULT 1.0 — three-valued logic does
        not arise. A COALESCE wrap would be defensive noise; the spec is
        explicit that no wrap is needed."""
        clause = _build_pre_filter_clause(
            apply_pre_filter=True,
            fsfm_branch_enabled=False,
        )
        assert clause is not None
        sql = str(clause)
        # Locate the confidence branch substring; nothing immediately
        # surrounding it should be a COALESCE call.
        assert 'COALESCE(memory_units.confidence' not in sql, (
            'F48 confidence branch must NOT be COALESCE-wrapped — column is '
            'NOT NULL DEFAULT 1.0 so SQL three-valued logic does not arise.'
        )

    def test_cold_start_schema_invariant(self) -> None:
        """Pinning the cold-start invariant at the schema level: the column
        is ``NOT NULL DEFAULT 1.0``. If either invariant ever weakens, the
        F48 branch could either NULL-poison the surrounding ``NOT (...)``
        (TVL) or filter cold-start rows — both regressions."""
        from memex_core.memory.sql_models import MemoryUnit

        confidence_col = MemoryUnit.__table__.columns['confidence']
        assert confidence_col.nullable is False, (
            'F48 cold-start invariant: memory_units.confidence must be NOT NULL '
            'so the F48 branch never sees NULL inputs.'
        )
        server_default = confidence_col.server_default
        assert server_default is not None, (
            'F48 cold-start invariant: memory_units.confidence must declare a '
            'server_default so DB-side inserts (raw SQL paths) get the safe value.'
        )
        default_text = (
            str(server_default.arg) if hasattr(server_default, 'arg') else str(server_default)
        )
        assert '1.0' in default_text or '1' == default_text.strip(), (
            f'F48 cold-start invariant: memory_units.confidence server_default '
            f'must be 1.0 (strictly > 0.2 threshold). Got: {default_text!r}'
        )
