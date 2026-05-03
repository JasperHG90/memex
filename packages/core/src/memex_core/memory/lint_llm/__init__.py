"""F10 — Surprise-gated LLM-assisted lint.

Submodules:

- ``surprise``: anisotropy-corrected surprise score for a memory unit.
- ``signatures``: DSPy signatures for the LLM lint checks.
- ``checks``: factories that bind a DSPy LM to a runnable ``RunLLMCheck``.
- ``polarity``: F10b NLI classifier wrapper + per-vault rate limiter.
- ``types``: shared dataclasses + the ``PolarityLabel`` enum.
"""

from memex_core.memory.lint_llm.checks import (
    make_schema_drift_check,
    make_semantic_contradiction_check,
)
from memex_core.memory.lint_llm.polarity import (
    DEFAULT_POLARITY_THRESHOLD,
    PolarityClassifier,
    PolarityRateLimiter,
    gate_passes,
)
from memex_core.memory.lint_llm.signatures import (
    CheckSchemaDrift,
    CheckSemanticContradiction,
)
from memex_core.memory.lint_llm.surprise import (
    DEFAULT_K,
    compute_unit_surprise,
    warm_corrector,
)
from memex_core.memory.lint_llm.types import (
    CheckContext,
    PolarityLabel,
    PolarityLiteral,
    PolarityResult,
)

__all__ = [
    'CheckContext',
    'CheckSchemaDrift',
    'CheckSemanticContradiction',
    'DEFAULT_K',
    'DEFAULT_POLARITY_THRESHOLD',
    'PolarityClassifier',
    'PolarityLabel',
    'PolarityLiteral',
    'PolarityRateLimiter',
    'PolarityResult',
    'compute_unit_surprise',
    'gate_passes',
    'make_schema_drift_check',
    'make_semantic_contradiction_check',
    'warm_corrector',
]
