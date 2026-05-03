"""F10 shared types — kept in a leaf module to avoid the circular import
between ``memory.lint_llm.checks`` (which produces findings) and
``services.lint_llm`` (which persists them).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Literal

from pydantic import BaseModel, Field, field_validator

from memex_core.memory.sql_models import LintType


class PolarityLabel(str, Enum):
    """Three-way NLI label produced by F10b's polarity classifier."""

    ENTAILMENT = 'entailment'
    NEUTRAL = 'neutral'
    CONTRADICTION = 'contradiction'


PolarityLiteral = Literal['entailment', 'neutral', 'contradiction']


class PolarityResult(BaseModel):
    """Argmax label + per-class probabilities from F10b's NLI classifier.

    Probabilities are stored verbatim (no rounding) so the gate can apply its
    threshold to the contradiction-probability without re-deriving it. The
    ``model_validator`` enforces the three-key shape and that probabilities sum
    to within tolerance of 1.0.
    """

    label: PolarityLabel = Field(description='Argmax of the three-class probabilities.')
    contradiction_prob: float = Field(ge=0.0, le=1.0)
    entailment_prob: float = Field(ge=0.0, le=1.0)
    neutral_prob: float = Field(ge=0.0, le=1.0)

    @field_validator('label', mode='before')
    @classmethod
    def _coerce_label(cls, v: Any) -> Any:
        if isinstance(v, PolarityLabel):
            return v
        if isinstance(v, str):
            return PolarityLabel(v.lower())
        return v


@dataclass
class LLMLintFinding:
    """Output of an F10 LLM check, ready to persist as a MaintenanceProposal.

    ``rule_name`` identifies the DSPy signature that produced the finding
    (e.g. ``llm_semantic_contradiction``, ``llm_schema_drift``).
    ``check_type`` is the corresponding evidence-payload tag per RFC-006.
    """

    rule_name: str
    check_type: str
    target_type: str
    target_id: str
    suggested_action: str
    surprise_score: float
    explanation: str
    related_unit_ids: list[str] = field(default_factory=list)
    extra_evidence: dict[str, Any] = field(default_factory=dict)
    lint_type: LintType = LintType.QUALITY


@dataclass
class CheckContext:
    """Optional context the F10 service threads into a check invocation.

    Currently carries the F10b polarity result computed by the orchestrator's
    OR'd gate so the check does not re-invoke the NLI model. Forwards the
    argmax label to the DSPy signature as ``polarity_hint`` and the
    probabilities into the finding's ``extra_evidence`` payload.
    """

    polarity: 'PolarityResult | None' = None


RunLLMCheck = Callable[
    ...,
    Awaitable['LLMLintFinding | None'],
]
