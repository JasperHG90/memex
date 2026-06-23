"""Regression gate for the maintenance-proposal ON CONFLICT arbiter predicate.

The partial unique index ``uq_maintenance_proposals_pending`` is defined with a
literal predicate (``WHERE status = 'pending'``; see ``sql_models.py``). Postgres
will only use a partial index as an ``ON CONFLICT`` arbiter when it can prove the
statement's ``index_where`` implies the index predicate.

Building ``index_where`` as ``col(mp.status) == 'pending'`` renders the predicate
as a *bound parameter* (``status = %(status_1)s`` / ``status = $N::VARCHAR``).
Postgres substitutes the real value under a *custom* plan, so the arbiter resolves
and naive single-execution tests pass — but once the cached prepared statement
flips to a *generic* plan (after ~5 executions) the parameter is opaque, the
predicate is no longer provable, and the insert raises "no unique or exclusion
constraint matching the ON CONFLICT specification". That is the regression that
shipped in v1.0.0rc1 (background lint maintenance-proposal upserts).

The fix renders ``index_where`` as the literal ``text("status = 'pending'")`` so
it matches the index predicate byte-for-byte and is provable under *any* plan.
This test asserts on the *compiled SQL* (no database, plan-independent), so it
gates the bound-parameter form deterministically. The plan-dependent runtime
behaviour is covered by the ``force_generic_plan`` integration test in
``test_int_lint_external_proposals.py``.
"""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy.dialects import postgresql

from memex_core.services.lint import _build_insert_finding_stmt
from memex_core.services.lint_external import (
    ExternalProposalRequest,
    _build_external_proposal_stmt,
)

_LITERAL_PREDICATE = (
    "ON CONFLICT (rule_name, target_type, target_id, vault_id) WHERE status = 'pending'"
)


def _compile(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


def test_internal_finding_arbiter_predicate_is_literal() -> None:
    """lint.py: the ON CONFLICT arbiter predicate is the literal index predicate."""
    sql = _compile(
        _build_insert_finding_stmt(
            vault_id='00000000-0000-0000-0000-000000000001',
            lint_type='routing',
            target_type='note',
            target_id='00000000-0000-0000-0000-000000000002',
            rule_name='demo-rule',
            evidence='{"k": "v"}',
            suggested_action='do the thing',
        )
    )
    assert _LITERAL_PREDICATE in sql
    # Guard against regression to the bound-parameter form (status = %(status_N)s).
    assert 'WHERE status = %(status_' not in sql


def test_external_proposal_arbiter_predicate_is_literal() -> None:
    """lint_external.py: same arbiter predicate must also be a literal."""
    req = ExternalProposalRequest(
        vault_id=str(uuid4()),
        rule_name='skill-misroute',
        lint_type='routing',
        target_type='note',
        target_id=str(uuid4()),
        description='classifier was confident but wrong',
        suggested_action='route the note to the agentic vault',
        evidence={'confidence': 0.93},
    )
    sql = _compile(
        _build_external_proposal_stmt(
            vault_id=uuid4(), req=req, evidence={'confidence': 0.93}, cooldown_days=30
        )
    )
    assert _LITERAL_PREDICATE in sql
    assert 'WHERE status = %(status_' not in sql
