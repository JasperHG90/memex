"""Write-time intent + risk safety filter (F25b).

F25 originally introduced a *separate* DSPy classifier (``ClassifyMemoryUnit``)
that ran one LLM call per extracted fact. F25b folded those output fields into
the fact-extraction signature itself (see ``ExtractSemanticFacts`` in
``extraction/core.py``), so intent + risk now arrive **with** each fact in the
single extraction call. This module is what's left after the fold:

* ``_coerce_intent`` / ``_coerce_risk`` — default-on-fail coercion of the
  LLM-produced strings into the canonical enum values. If the LLM omits a
  field or returns gibberish, the fact keeps the schema defaults
  (intent=durable, risk=none). Extraction must never be blocked by a
  classification mishap.
* ``filter_safety_blocked`` — post-extraction filter that drops facts the
  LLM flagged as ``risk_class='safety'`` *before* persistence. Counter-side
  effect tracks the drop rate per vault for dashboards.

Pipeline placement (post-F25b):
    extract_facts → coerce intent/risk on each fact → dedup → embedding →
    filter_safety_blocked → persist.

The two-verb risk policy (unchanged from F25):
    none      — public-safe content (default).
    sensitive — flagged for linter; still retrievable in default scope.
    private   — excluded from default retrieval (see strategies.apply_generic_filters).
    safety    — refused at ingestion; ``filter_safety_blocked`` drops these facts.

Permanent / Durable / Ephemeral mirror KinthAI's three-way split (intent class
captures lifecycle, separate from risk).
"""

from __future__ import annotations

import logging

from memex_common.schemas import (
    IntentClass,
    RiskClass,
)
from memex_core.memory.extraction.models import ProcessedFact
from memex_core.metrics import CLASSIFIER_BLOCKED_TOTAL

logger = logging.getLogger('memex.core.memory.extraction.classifier')

INTENT_VALUES: tuple[str, ...] = tuple(c.value for c in IntentClass)
RISK_VALUES: tuple[str, ...] = tuple(c.value for c in RiskClass)

DEFAULT_INTENT = IntentClass.DURABLE.value
DEFAULT_RISK = RiskClass.NONE.value


def _coerce_intent(value: object) -> str:
    if isinstance(value, str) and value in INTENT_VALUES:
        return value
    return DEFAULT_INTENT


def _coerce_risk(value: object) -> str:
    if isinstance(value, str) and value in RISK_VALUES:
        return value
    return DEFAULT_RISK


def filter_safety_blocked(facts: list[ProcessedFact]) -> list[ProcessedFact]:
    """Drop facts the classifier flagged as ``risk_class='safety'``.

    Returns a new list. Counter-side effect tracks how many were dropped per
    vault (so dashboards can alert if the classifier suddenly starts blocking
    a lot).
    """
    kept: list[ProcessedFact] = []
    blocked_by_vault: dict[str, int] = {}
    for f in facts:
        if f.risk_class == 'safety':
            blocked_by_vault[str(f.vault_id)] = blocked_by_vault.get(str(f.vault_id), 0) + 1
            continue
        kept.append(f)
    for vault_id, n in blocked_by_vault.items():
        CLASSIFIER_BLOCKED_TOTAL.labels(vault_id=vault_id).inc(n)
    if blocked_by_vault:
        logger.info(
            'Write-time classifier blocked %d facts (risk_class=safety) across %d vaults',
            sum(blocked_by_vault.values()),
            len(blocked_by_vault),
        )
    return kept
