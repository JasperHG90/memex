"""Standing-item unit tests for the F6 v1 rule SQL.

These are guards against contributor regressions that a future rule author
might introduce — they assert structural properties of the SQL strings that
must hold for **every** rule, regardless of what the rule actually checks.

Currently:

  * Every rule whose target is vault-scoped MUST include
    ``vault_id = :vault_id`` in its WHERE clause (otherwise a rule-run
    against vault A would also count vault B's rows in vault A's
    findings).
  * The :class:`RuleSpec` ``select_sql`` must yield a SELECT (not an
    INSERT/UPDATE/DELETE) — guards the AC-F6-2 read-only invariant at the
    spec level.
  * Every rule's ``select_sql`` must project both ``target_id`` and
    ``evidence`` columns; otherwise the insert into ``maintenance_proposals``
    will fail at runtime.
"""

from __future__ import annotations

import re

import pytest

from memex_core.services.lint import V1_RULES


def _normalize(sql: str) -> str:
    return re.sub(r'\s+', ' ', sql).strip().lower()


@pytest.mark.parametrize('spec', V1_RULES, ids=lambda s: s.name)
def test_each_rule_filters_on_vault_id(spec) -> None:
    """Every v1 rule must include ``vault_id = :vault_id`` in its WHERE clause.

    Reason: rules are vault-scoped (`run_rules(vault_id)`), so any rule that
    forgets the predicate would fan-out across vaults.
    """
    norm = _normalize(spec.select_sql)
    assert 'vault_id = :vault_id' in norm, (
        f'Rule {spec.name} is missing `vault_id = :vault_id` in its WHERE clause'
    )


@pytest.mark.parametrize('spec', V1_RULES, ids=lambda s: s.name)
def test_each_rule_select_starts_with_select(spec) -> None:
    """Spec-level read-only guard: ``select_sql`` must start with SELECT."""
    norm = _normalize(spec.select_sql)
    assert norm.startswith('select '), (
        f'Rule {spec.name} select_sql does not start with SELECT: {norm[:60]!r}'
    )
    forbidden = ('insert ', 'update ', 'delete ', 'truncate ', 'merge ', 'copy ')
    assert not any(tok in norm for tok in forbidden), (
        f'Rule {spec.name} select_sql contains a write keyword'
    )


@pytest.mark.parametrize('spec', V1_RULES, ids=lambda s: s.name)
def test_each_rule_projects_target_id_and_evidence(spec) -> None:
    """The SELECT must project both ``target_id`` and ``evidence``."""
    norm = _normalize(spec.select_sql)
    assert 'as target_id' in norm, f'Rule {spec.name} does not alias a column to `target_id`'
    assert 'as evidence' in norm, f'Rule {spec.name} does not alias a column to `evidence`'
