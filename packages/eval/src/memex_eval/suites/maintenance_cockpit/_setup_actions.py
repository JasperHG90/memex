"""Suite-private setup actions for the maintenance_cockpit eval suite.

These handlers are registered via the decorator for forward-compatibility
but all seven scenarios use the ``@suite.scenario`` decorator API with
async evaluator functions that drive the API directly.

Custom setup actions live here — not in the framework — per the
eval-suites.md constraint.
"""

from __future__ import annotations

# No custom setup action classes needed. All scenarios use the
# @suite.scenario decorator API with async evaluator functions
# that drive the lint lifecycle via ctx.api directly.

__all__: list[str] = []
