"""Write-time intent + risk safety filter (F25b).

F25 originally introduced a *separate* DSPy classifier (``ClassifyMemoryUnit``)
that ran one LLM call per extracted fact. F25b folded those output fields into
the fact-extraction signature itself (see ``ExtractSemanticFacts`` in
``extraction/core.py``), so intent + risk now arrive **with** each fact in the
single extraction call. This module is what's left after the fold:

* ``filter_safety_blocked`` — post-extraction filter that drops facts the
  LLM flagged as ``risk_class='safety'`` *before* persistence. Counter-side
  effect tracks the drop rate per vault for dashboards.

Default-on-fail coercion of the LLM-produced ``intent_class`` / ``risk_class``
strings happens at parse time on ``RawFact`` itself (pydantic
``@field_validator``s in ``extraction/models.py``). The previous module-level
``_coerce_intent`` / ``_coerce_risk`` helpers were retired in the F25b
follow-up — they had become dead production code that duplicated the
validators, with the only consumers being their own unit tests. Single
source of truth for the coercion lives on ``RawFact`` (and, post-Hermes
round-2 MED, on ``ExtractedFact`` too).

Pipeline placement (post-F25b):
    extract_facts (LLM emits intent/risk; RawFact validators coerce on the
    way in) → dedup → embedding → filter_safety_blocked → persist.

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

from memex_core.memory.extraction.models import ProcessedFact
from memex_core.metrics import CLASSIFIER_BLOCKED_TOTAL

logger = logging.getLogger('memex.core.memory.extraction.classifier')

# Hermes round-5 MED: ``INTENT_VALUES`` / ``RISK_VALUES`` / ``DEFAULT_INTENT``
# / ``DEFAULT_RISK`` were retired from this module. The canonical valid
# values live on the ``IntentClass`` / ``RiskClass`` enums in
# ``memex_common.schemas``; the canonical defaults are
# ``IntentClass.DURABLE.value`` / ``RiskClass.NONE.value`` (and are enforced
# by the pydantic validators on ``RawFact`` / ``ExtractedFact``). Tests that
# previously imported the constants from here should derive them directly
# from the enums.


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
