"""Write-time intent + risk safety filter.

Post-extraction filter that drops facts flagged as ``risk_class='safety'``
before persistence. Tracks the drop rate per vault for dashboards.

Intent/risk coercion lives on ``RawFact`` pydantic validators in
``extraction/models.py`` — this module does not coerce.

Pipeline: extract_facts → dedup → embedding → filter_safety_blocked → persist.

Risk classes: none (default), sensitive (flagged for linter), private (excluded
from default retrieval), safety (blocked at ingestion).

Intent classes: permanent, durable, ephemeral (mirrors lifecycle split).
"""

from __future__ import annotations

import logging

from memex_core.memory.extraction.models import ProcessedFact
from memex_core.metrics import CLASSIFIER_BLOCKED_TOTAL

logger = logging.getLogger('memex.core.memory.extraction.classifier')

# Intent/risk enum values and defaults moved to ``memex_common.schemas``
# (IntentClass, RiskClass). Pydantic validators on RawFact/ExtractedFact
# enforce them at parse time.


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
