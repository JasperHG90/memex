"""Write-time intent + risk classifier.

Records but does not block facts flagged as ``risk_class='safety'``.
Safety-classified facts pass through for observability; blocking will be
handled by a future pre-flight risk assessment. Tracks the safety rate
per vault for dashboards.

Intent/risk coercion lives on ``RawFact`` pydantic validators in
``extraction/models.py`` — this module does not coerce.

Pipeline: extract_facts → dedup → embedding → filter_safety_blocked → persist.

Risk classes: none (default), sensitive (flagged for linter), private (excluded
from default retrieval), safety (recorded but passed through).

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
    """Record but do not drop facts flagged as ``risk_class='safety'``.

    Safety-classified facts are passed through — they are logged and
    tracked via metrics but not blocked at ingestion. A pre-flight risk
    assessment (not yet implemented) will handle actual blocking upstream.
    """
    safety_by_vault: dict[str, int] = {}
    for f in facts:
        if f.risk_class == 'safety':
            safety_by_vault[str(f.vault_id)] = safety_by_vault.get(str(f.vault_id), 0) + 1
    for vault_id, n in safety_by_vault.items():
        CLASSIFIER_BLOCKED_TOTAL.labels(vault_id=vault_id).inc(n)
    if safety_by_vault:
        logger.info(
            'Write-time classifier recorded %d safety-classified facts across %d vaults',
            sum(safety_by_vault.values()),
            len(safety_by_vault),
        )
    return facts
