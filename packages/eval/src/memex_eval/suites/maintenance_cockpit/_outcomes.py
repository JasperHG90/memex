"""Suite-private outcomes for the maintenance_cockpit eval suite.

These outcomes are registered via the decorator for forward-compatibility
but all seven scenarios use the ``@suite.scenario`` decorator API with
async evaluator functions. The outcomes here exist purely so
``memex-eval suite list`` can report metric_keys.

Custom outcome types live here — not in the framework — per the
eval-suites.md constraint.
"""

from __future__ import annotations

# No custom outcome classes needed. All scenarios use the
# @suite.scenario decorator API with async evaluator functions
# that populate ctx.metrics directly.

__all__: list[str] = []
