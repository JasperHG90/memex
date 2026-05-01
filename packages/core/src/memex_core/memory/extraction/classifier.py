"""Write-time intent + risk classifier (F25).

Single LLM call per fact produces both intent and risk classifications.
Subsumes F26 (risk-class-only) — risk is just one dimension of the same
constrained-JSON output.

Pipeline placement: AFTER fact extraction + dedup, BEFORE persistence
(see ``extraction/engine.py: extract_and_persist``). The classifier mutates
``ProcessedFact.intent_class`` and ``ProcessedFact.risk_class`` in place,
then the caller filters out ``risk_class='safety'`` units before storage.

Default-on-fail policy: if the LLM call errors, raises, or returns an
unparseable response, the fact keeps the schema defaults (intent='durable',
risk='none'). Errors are logged + counted, never raised — extraction must
not be blocked by a classifier outage.

The two-verb risk policy:
    none      — public-safe content (default).
    sensitive — flagged for linter; still retrievable in default scope.
    private   — excluded from default retrieval (see strategies.apply_generic_filters).
    safety    — refused at ingestion; the caller must drop these facts.

Permanent / Durable / Ephemeral mirror KinthAI's three-way split (intent class
captures lifecycle, separate from risk).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

import dspy

from memex_core.llm import run_dspy_operation
from memex_core.memory.extraction.models import ProcessedFact
from memex_core.metrics import (
    CLASSIFIER_CALLS_TOTAL,
    CLASSIFIER_INTENT_DISTRIBUTION,
    CLASSIFIER_RISK_DISTRIBUTION,
)

logger = logging.getLogger('memex.core.memory.extraction.classifier')

INTENT_VALUES = ('permanent', 'durable', 'ephemeral')
RISK_VALUES = ('none', 'sensitive', 'private', 'safety')

DEFAULT_INTENT = 'durable'
DEFAULT_RISK = 'none'

IntentLiteral = Literal['permanent', 'durable', 'ephemeral']
RiskLiteral = Literal['none', 'sensitive', 'private', 'safety']


class ClassifyMemoryUnit(dspy.Signature):
    """Classify a single memory unit by intent (lifecycle) and risk (sensitivity).

    Intent describes how durable the fact is, NOT how important it feels:
      - permanent: identity, preferences, key facts that should never decay
        (e.g. "user has a peanut allergy", "user prefers ruff over black").
      - durable: project decisions, relationship state, multi-week relevance.
        DEFAULT for unclear cases.
      - ephemeral: task context, session details, days-to-weeks relevance only
        (e.g. "tomorrow's standup is at 10am", "Bob is on vacation this week").

    Set intent based on the content's actual durability, not perceived importance.
    A specific date for next week is "ephemeral" even if the event is important.
    The user's home address is "permanent" even though it's not exciting.

    Risk classifies sensitivity:
      - none: default, public-safe content.
      - sensitive: flagged for linter review; still retrievable in default scope.
      - private: excluded from default retrieval; surfaced only on explicit query
        (passwords, financial details, medical specifics).
      - safety: blocked entirely (Memex refuses to ingest). Use ONLY for content
        that would cause real-world harm if surfaced (e.g. self-harm planning,
        instructions for violence). Be conservative — when in doubt, prefer
        'sensitive' or 'private' over 'safety'.
    """

    fact_text: str = dspy.InputField(description='The memory unit text to classify.')
    fact_context: str = dspy.InputField(description='Optional surrounding context. May be empty.')
    intent_class: IntentLiteral = dspy.OutputField(
        description='Lifecycle: permanent | durable | ephemeral.'
    )
    risk_class: RiskLiteral = dspy.OutputField(
        description='Sensitivity: none | sensitive | private | safety.'
    )


def _coerce_intent(value: object) -> str:
    if isinstance(value, str) and value in INTENT_VALUES:
        return value
    return DEFAULT_INTENT


def _coerce_risk(value: object) -> str:
    if isinstance(value, str) and value in RISK_VALUES:
        return value
    return DEFAULT_RISK


async def _classify_one(
    fact: ProcessedFact,
    lm: dspy.LM,
    predictor: dspy.Module,
    semaphore: asyncio.Semaphore | None,
) -> None:
    """Mutate ``fact.intent_class`` + ``fact.risk_class`` from one LLM call.

    Default-on-fail: any error keeps the schema defaults. The classifier is
    intentionally non-blocking — extraction proceeds even if classification
    fails for a single fact.
    """
    try:
        result = await run_dspy_operation(
            lm=lm,
            predictor=predictor,
            input_kwargs={
                'fact_text': fact.fact_text,
                'fact_context': fact.context or '',
            },
            semaphore=semaphore,
            operation_name='classifier',
        )
        fact.intent_class = _coerce_intent(getattr(result, 'intent_class', None))
        fact.risk_class = _coerce_risk(getattr(result, 'risk_class', None))
        CLASSIFIER_CALLS_TOTAL.labels(status='success').inc()
    except Exception as e:  # noqa: BLE001 — classifier must never block extraction
        logger.warning(
            'Write-time classifier failed for fact (defaulting to %s/%s): %s',
            DEFAULT_INTENT,
            DEFAULT_RISK,
            e,
        )
        CLASSIFIER_CALLS_TOTAL.labels(status='error').inc()
        # fact retains schema defaults
    finally:
        # Use ``.value`` so Prometheus labels stay as 'durable' / 'safety' /
        # etc., not 'IntentClass.DURABLE' (str-Enum __str__ uses class.member).
        intent_label = (
            fact.intent_class.value if hasattr(fact.intent_class, 'value') else fact.intent_class
        )
        risk_label = fact.risk_class.value if hasattr(fact.risk_class, 'value') else fact.risk_class
        CLASSIFIER_INTENT_DISTRIBUTION.labels(intent_class=intent_label).inc()
        CLASSIFIER_RISK_DISTRIBUTION.labels(risk_class=risk_label).inc()


async def classify_facts(
    facts: list[ProcessedFact],
    lm: dspy.LM,
    semaphore: asyncio.Semaphore | None = None,
    predictor: dspy.Module | None = None,
) -> list[ProcessedFact]:
    """Classify each fact's intent + risk in parallel under the shared semaphore.

    Mutates and returns the same list (callers may rely on mutation in place,
    but the return value lets composition stay readable).

    Caller is responsible for filtering out ``risk_class='safety'`` units
    before persistence — see ``extraction/engine.py``.
    """
    if not facts:
        return facts
    pred = predictor if predictor is not None else dspy.Predict(ClassifyMemoryUnit)
    await asyncio.gather(*(_classify_one(f, lm, pred, semaphore) for f in facts))
    return facts


def filter_safety_blocked(facts: list[ProcessedFact]) -> list[ProcessedFact]:
    """Drop facts the classifier flagged as ``risk_class='safety'``.

    Returns a new list. Counter-side effect tracks how many were dropped per
    vault (so dashboards can alert if the classifier suddenly starts blocking
    a lot).
    """
    from memex_core.metrics import CLASSIFIER_BLOCKED_TOTAL

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
