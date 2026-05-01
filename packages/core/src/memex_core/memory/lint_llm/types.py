"""F10 shared types — kept in a leaf module to avoid the circular import
between ``memory.lint_llm.checks`` (which produces findings) and
``services.lint_llm`` (which persists them).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from memex_core.memory.sql_models import LintType


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


RunLLMCheck = Callable[[UUID, UUID, AsyncSession], Awaitable['LLMLintFinding | None']]
