"""F10 — Surprise-gated LLM-assisted lint.

Submodules:

- ``surprise``: anisotropy-corrected surprise score for a memory unit.
"""

from memex_core.memory.lint_llm.surprise import (
    DEFAULT_K,
    compute_unit_surprise,
    warm_corrector,
)

__all__ = [
    'DEFAULT_K',
    'compute_unit_surprise',
    'warm_corrector',
]
