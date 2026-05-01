"""F6 — Prometheus scrape after a lint run (TC-20-5 +1, AC-F6-5).

After ``LintService.run_rules`` emits findings, the registered Prometheus
metrics must reflect that:

  * ``memex_lint_findings_total{rule_name, lint_type, vault_id}`` counter is
    ``>= 1`` for the rule that fired.
  * ``memex_lint_run_duration_seconds{rule_name}`` histogram has at least one
    sample for every rule executed (`_count > 0`).

We read counters/histogram samples from the metric objects directly (the
Prometheus default registry is process-wide), which mirrors what the
``/metrics`` endpoint scrapes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from memex_common.types import FactTypes
from memex_core import metrics as metrics_mod
from memex_core.memory.sql_models import MemoryUnit, Note, Vault


pytestmark = [pytest.mark.integration]


def _counter_value(counter, **labels) -> float:
    """Read a single Counter sample by label values."""
    return counter.labels(**labels)._value.get()


def _histogram_count(hist, **labels) -> int:
    """Read the ``_count`` of a Histogram sample by label values.

    prometheus_client exposes the count via ``_metrics[<labelvalues>]._count``
    on the parent or via ``collect()`` walking samples; we use the
    ``_sum/_count`` attributes on the per-label child after labels(...).
    """
    child = hist.labels(**labels)
    # Histogram child stores count + sum directly.
    sample_count = getattr(child, '_count', None)
    if sample_count is not None:
        return int(sample_count.get())
    # Fallback: walk the metric family for a `_count` sample.
    for sample in hist.collect()[0].samples:
        if sample.name.endswith('_count') and all(
            sample.labels.get(k) == v for k, v in labels.items()
        ):
            return int(sample.value)
    return 0


@pytest.mark.asyncio
async def test_prometheus_scrape_reflects_lint_run(session: AsyncSession, api) -> None:
    """Run lint with one fires-case, then verify the scrape."""
    vault = Vault(name=f'F6-metrics-{uuid4().hex[:8]}')
    session.add(vault)
    await session.commit()
    await session.refresh(vault)

    note = Note(
        id=uuid4(),
        vault_id=vault.id,
        content_hash=f'hash-{uuid4().hex[:8]}',
        original_text='seed',
    )
    session.add(note)
    await session.commit()

    unit = MemoryUnit(
        id=uuid4(),
        vault_id=vault.id,
        note_id=note.id,
        text='sensitive metrics-test unit',
        fact_type=FactTypes.WORLD,
        status='active',
        is_deprioritized=False,
        risk_class='sensitive',
        event_date=datetime.now(timezone.utc),
        embedding=[0.1] * 384,
    )
    session.add(unit)
    await session.commit()

    summary = await api.lint.run_rules(vault.id)
    assert summary.total_findings == 1
    fired = next(r for r in summary.rules if r.findings_emitted)

    # Counter labelled with the firing rule must show >= 1.
    counter_value = _counter_value(
        metrics_mod.LINT_FINDINGS_TOTAL,
        rule_name=fired.rule_name,
        lint_type=fired.lint_type.value,
        vault_id=str(vault.id),
    )
    assert counter_value >= 1, (
        f'Expected LINT_FINDINGS_TOTAL >= 1 for {fired.rule_name}; got {counter_value}'
    )

    # Every rule that ran (fired or not) records at least one duration sample.
    for r in summary.rules:
        hist_count = _histogram_count(metrics_mod.LINT_RUN_DURATION_SECONDS, rule_name=r.rule_name)
        assert hist_count >= 1, (
            f'Expected LINT_RUN_DURATION_SECONDS samples for {r.rule_name}; got {hist_count}'
        )
