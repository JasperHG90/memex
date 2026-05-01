"""AC-F38-4 — `services/consolidation.py` is a thin orchestrator.

Per RFC-010 §"Module-write audit", the consolidation module's only direct
DB write is the ``ConsolidationTick`` summary row. All other side-effects
are delegated to ReflectionService / ContradictionEngine / prune_stale_evidence.

This test inspects the module source for ``session.add(...)`` and ``session.execute(insert(...))``
references and asserts the only target is ``ConsolidationTick``.
"""

from __future__ import annotations

import inspect
import re

from memex_core.services import consolidation


_SOURCE = inspect.getsource(consolidation)


def test_only_tick_summary_is_direct_session_add():
    """Every ``session.add`` call must add a ``ConsolidationTick``-typed row."""
    add_sites = re.findall(r'session\.add\(\s*(\w+)\s*\)', _SOURCE)
    assert add_sites, 'expected at least one session.add — the tick-summary write'
    # Each variable name passed to session.add must be assigned a
    # ``ConsolidationTick(...)`` constructor in the same source file.
    for varname in set(add_sites):
        pattern = re.compile(rf'\b{re.escape(varname)}\s*=\s*ConsolidationTick\s*\(', re.MULTILINE)
        assert pattern.search(_SOURCE), (
            f'AC-F38-4 violation: session.add({varname}) but {varname} is not '
            f'assigned a ConsolidationTick row in services/consolidation.py.'
        )


def test_no_direct_inserts_other_than_tick():
    direct_inserts = re.findall(r'insert\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)', _SOURCE)
    bad = [x for x in direct_inserts if x != 'ConsolidationTick']
    assert not bad, f'AC-F38-4 violation: direct inserts to {bad}'


def test_step_ordering_contradiction_before_reflection_in_source():
    """Source-level guard: contradiction.detect_contradictions occurs before
    reflection.reflect_batch in tick(). Defends against accidental reordering."""
    contradiction_idx = _SOURCE.find('detect_contradictions(')
    reflection_idx = _SOURCE.find('reflect_batch(')
    prune_idx = _SOURCE.find('prune_stale_evidence(')
    assert contradiction_idx > 0 and reflection_idx > 0 and prune_idx > 0
    assert contradiction_idx < reflection_idx, (
        'Step ordering violation: contradiction must run BEFORE reflection in tick().'
    )
    assert reflection_idx < prune_idx, (
        'Step ordering violation: reflection must run BEFORE prune in tick().'
    )


def test_prune_only_on_already_stale_check_present():
    """Source-level guard: tick() filters to ContentStatus.STALE before prune."""
    assert 'ContentStatus.STALE' in _SOURCE, (
        'AC-F38-2 violation: tick() must filter to ContentStatus.STALE before prune.'
    )
