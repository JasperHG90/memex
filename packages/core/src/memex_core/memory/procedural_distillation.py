"""Distillation pass: cases → procedure, procedures → strategy (design §9).

Procedures and strategies are **projections over their source cluster**
(§9): a procedure is the distillation of the *tight* case cluster assigned
to its ``(scope, verb, context)`` anchor; a strategy is the distillation
of the *broad* cluster of sibling procedures sharing ``(scope, verb)``.
This module is the LLM half — the typed DSPy passes — mirroring the
assignment judge (``procedural_assignment.py``). The worker that claims
queue rows, gathers the cluster, calls these passes, and writes the
derived entry lives in ``services/procedural_derivation_service.py``.

The distillation **discipline** is the §9 rule set, hardened by spike #5
(§19.5): the model erases quantitative anchors (canary "10%" → "a small
percentage") unless rule 6 forbids it — validated 0/4 → 4/4 anchors
preserved on the production stack with the anchor rule. The rule text
below is the spike-validated prompt verbatim; do NOT soften it.

§19.3 note (a): optional string outputs on the production model come back
as literal ``'None'`` / ``'null'`` / quote-wrapped — :func:`_normalise`
(shared shape with the assignment judge) scrubs them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import dspy
from pydantic import BaseModel, Field

from memex_core.llm import run_dspy_operation

logger = logging.getLogger('memex.core.memory.procedural_distillation')


# §9: "Distill from a cluster (N≥3), never one case." Below this, the
# anchor stays a draft stub — there isn't enough shared signal to
# generalize a happy path without inventing it.
MIN_CASES_FOR_DISTILLATION = 3


# The §9 discipline, verbatim from spike #5's validated prompt (§19.5).
# Rules 1–5 are the §9 groundedness/generalization discipline; rule 6 is
# the spike-validated quantitative-anchor fix (0/4 → 4/4). Do not paraphrase.
DISTILLATION_DISCIPLINE = """\
1. GROUND EVERY STEP. Every step MUST list source_cases — the case ids of
   the cases whose text actually supports that step. A step you cannot
   support with specific case text MUST NOT appear. Do not add
   best-practice steps from general knowledge (no "notify the team",
   "update docs", "write tests" unless a case did it).
2. GENERALIZE ONLY WHAT IS SHARED. Steps appearing in >=2 cases form the
   happy path, phrased service-agnostically (no service names in step text).
3. FAILURE BRANCHES BECOME CONDITIONS. A step learned from a failure or a
   conditional branch carries a condition describing when it applies; cite
   the case where that branch fired.
4. SINGLE-CASE STEPS are allowed ONLY as conditioned guards, never as
   unconditional happy-path steps.
5. when_to_use must generalize the case triggers — the task class, not any
   one episode.
6. PRESERVE QUANTITATIVE ANCHORS VERBATIM. Percentages, durations, retry
   counts, and named windows from the cases (e.g. "10%", "15 minutes",
   "three times in a row", "the batch window") must appear verbatim in the
   step text or condition that uses them; never paraphrase a number into
   vague prose.
7. SKILL HINTS ARE CAPABILITY DESCRIPTIONS, NOT SPECIFIC SKILLS. When a step
   would obviously be executed by a recognizable KIND of automation, set
   skill_hint to a short platform-agnostic capability description ("a skill
   that can bump, tag, and push a release") — never a concrete skill id or
   tool name. Leave skill_hint null when no skill obviously fits; the prose
   action is always authoritative."""


# Strategy distillation discipline: a strategy is the *heuristic above* its
# procedures (§9 "emerges above multiple procedures"), not a re-run of the
# procedure steps. It generalizes the shared situation + decision rule.
STRATEGY_DISCIPLINE = """\
1. A STRATEGY IS A HEURISTIC, NOT A CHECKLIST. Synthesize the shared
   situation these procedures address and the decision rule for choosing
   among them — not a merged step list.
2. GROUND IN THE PROCEDURES. Every claim must trace to one or more of the
   given procedures; do not invent guidance no procedure supports.
3. when_to_apply generalizes the procedures' triggers into the broad task
   class (the situation), not any single procedure's specific trigger.
4. PRESERVE QUANTITATIVE ANCHORS VERBATIM (percentages, durations, retry
   counts, named windows) when they are decision-relevant; never paraphrase
   a number into vague prose."""


class DistilledStep(BaseModel):
    """One atomic, cited step of a distilled procedure (spike #5 grammar)."""

    order: int = Field(description='1-based position in the happy path.')
    action: str = Field(description='Service-agnostic imperative action.')
    condition: str | None = Field(
        default=None,
        description='When this step applies (failure/branch guards only); null on happy path.',
    )
    source_cases: list[str] = Field(
        default_factory=list,
        description='Case ids whose text supports this step (rule 1).',
    )
    skill_hint: str | None = Field(
        default=None,
        description=(
            '§18.8 capability description: the KIND of agent skill that would '
            'execute this step (e.g. "a skill that can bump, tag, and push a '
            'release"), platform-agnostic. NOT a specific skill id — the '
            'retrieving agent matches it against its own registry; the prose '
            'action stays authoritative. Null when no skill obviously fits.'
        ),
    )


class DistillProcedure(dspy.Signature):
    """You are the distillation pass of a procedural-memory system. The given
    CASES (worked episodes, templated as Trigger / Situation / Actions /
    Outcome+Lesson) form one tight cluster sharing an anchor. Derive the
    single PROCEDURE this cluster supports, following the distillation
    discipline EXACTLY."""

    cases_markdown: str = dspy.InputField(
        desc='The full text of all cases in the cluster, delimited; each labelled with its case id.'
    )
    anchor: str = dspy.InputField(
        desc='The (scope, verb, context) anchor this procedure is keyed on.'
    )
    discipline: str = dspy.InputField(desc='Non-negotiable distillation rules.')

    title: str = dspy.OutputField(desc='Short imperative title for the procedure.')
    summary: str = dspy.OutputField(desc='One-sentence explanation of what the procedure does.')
    when_to_use: str = dspy.OutputField(desc='Generalized trigger for the task class (rule 5).')
    steps: list[DistilledStep] = dspy.OutputField(desc='Ordered, cited, atomic steps.')
    notes: str = dspy.OutputField(desc='<=40 words on anything deliberately excluded and why.')


class DistillStrategy(dspy.Signature):
    """You are the strategy-distillation pass of a procedural-memory system.
    The given PROCEDURES all share a ``(scope, verb)`` anchor — they are
    specific ways of handling the same broad task class. Derive the single
    STRATEGY (the heuristic above them: the shared situation + the rule for
    choosing among them), following the discipline EXACTLY."""

    procedures_markdown: str = dspy.InputField(
        desc='The sibling procedures (title, when_to_use, summary) sharing the (scope, verb) anchor.'
    )
    anchor: str = dspy.InputField(desc='The (scope, verb) anchor this strategy is keyed on.')
    discipline: str = dspy.InputField(desc='Non-negotiable strategy-distillation rules.')

    title: str = dspy.OutputField(desc='Short title for the strategy/heuristic.')
    summary: str = dspy.OutputField(desc='One-sentence statement of the heuristic.')
    when_to_apply: str = dspy.OutputField(desc='The broad situation the strategy covers.')
    body: str = dspy.OutputField(
        desc='The heuristic: shared situation + decision rule, in markdown.'
    )
    notes: str = dspy.OutputField(desc='<=40 words on scope/exclusions.')


@dataclass(frozen=True)
class DistilledProcedure:
    """Normalised procedure distillation, ready to write as an entry."""

    title: str
    summary: str
    trigger: str  # when_to_use
    body: str  # rendered steps + notes (markdown)
    steps: list[DistilledStep]
    notes: str


@dataclass(frozen=True)
class DistilledStrategy:
    """Normalised strategy distillation, ready to write as an entry."""

    title: str
    summary: str
    trigger: str  # when_to_apply
    body: str
    notes: str


def _normalise(value: object) -> str | None:
    """Scrub §19.3 string-output artefacts: literal 'None'/'null',
    quote-wrapped values, surrounding whitespace. Shared shape with the
    assignment judge's ``_normalise``."""
    if value is None:
        return None
    text = str(value).strip().strip('"').strip("'").strip()
    if not text or text.lower() in ('none', 'null', 'n/a'):
        return None
    return text


def _render_steps_markdown(steps: list[DistilledStep], notes: str | None) -> str:
    """Render the cited, conditioned steps into a stable markdown body.

    Conditions ride inline (``— when …``) so a guard never reads as an
    unconditional happy-path step (rule 3/4). Cited case ids are kept as a
    trailing parenthetical so the body stays auditable against §9 rule 1.
    """
    lines: list[str] = ['## Steps', '']
    for step in sorted(steps, key=lambda s: s.order):
        action = (step.action or '').strip()
        if not action:
            continue
        line = f'{step.order}. {action}'
        cond = (step.condition or '').strip()
        if cond and cond.lower() not in ('none', 'null'):
            line += f' — when {cond}'
        cites = [c for c in (step.source_cases or []) if c and str(c).strip()]
        if cites:
            line += f' _(cases: {", ".join(str(c) for c in cites)})_'
        hint = (step.skill_hint or '').strip()
        if hint and hint.lower() not in ('none', 'null'):
            # §18.8: advisory capability description; the agent may map it
            # to one of its own skills, else follow the prose action.
            line += f' _[skill: {hint}]_'
        lines.append(line)
    body = '\n'.join(lines)
    note_text = (notes or '').strip()
    if note_text and note_text.lower() not in ('none', 'null'):
        body += f'\n\n## Notes\n\n{note_text}'
    return body


async def distill_procedure(
    lm: dspy.LM,
    *,
    cases_markdown: str,
    anchor: str,
    timeout: int = 120,
) -> DistilledProcedure:
    """Run one procedure distillation over a case cluster (§9 / spike #5).

    Raises on executor failure — the derivation worker maps any raise to
    ``mark_derivation_failed`` (retry then fail), so a transient LLM error
    never corrupts the entry.
    """
    predictor = dspy.Predict(DistillProcedure)
    result = await run_dspy_operation(
        lm,
        predictor,
        {
            'cases_markdown': cases_markdown,
            'anchor': anchor,
            'discipline': DISTILLATION_DISCIPLINE,
        },
        operation_name='procedural.distill_procedure',
        timeout=timeout,
    )

    steps = list(result.steps or [])
    title = _normalise(result.title) or 'Untitled procedure'
    summary = _normalise(result.summary) or ''
    trigger = _normalise(result.when_to_use) or ''
    notes = _normalise(result.notes) or ''
    return DistilledProcedure(
        title=title,
        summary=summary,
        trigger=trigger,
        body=_render_steps_markdown(steps, notes),
        steps=steps,
        notes=notes,
    )


async def distill_strategy(
    lm: dspy.LM,
    *,
    procedures_markdown: str,
    anchor: str,
    timeout: int = 120,
) -> DistilledStrategy:
    """Run one strategy distillation over a cluster of sibling procedures (§9)."""
    predictor = dspy.Predict(DistillStrategy)
    result = await run_dspy_operation(
        lm,
        predictor,
        {
            'procedures_markdown': procedures_markdown,
            'anchor': anchor,
            'discipline': STRATEGY_DISCIPLINE,
        },
        operation_name='procedural.distill_strategy',
        timeout=timeout,
    )

    title = _normalise(result.title) or 'Untitled strategy'
    summary = _normalise(result.summary) or ''
    trigger = _normalise(result.when_to_apply) or ''
    body = _normalise(result.body) or ''
    notes = _normalise(result.notes) or ''
    if notes and notes.lower() not in ('none', 'null'):
        body = f'{body}\n\n## Notes\n\n{notes}' if body else notes
    return DistilledStrategy(
        title=title,
        summary=summary,
        trigger=trigger,
        body=body,
        notes=notes,
    )


def render_cases_markdown(cases: list[tuple[str, str]]) -> str:
    """Delimit ``[(case_id, case_text), …]`` into the labelled block the
    distiller expects. Each case is fenced with its id so the model can
    cite it in ``source_cases`` (rule 1)."""
    blocks = []
    for case_id, text in cases:
        blocks.append(f'### case: {case_id}\n\n{(text or "").strip()}')
    return '\n\n---\n\n'.join(blocks)


def render_procedures_markdown(procedures: list[dict[str, str]]) -> str:
    """Delimit sibling procedures for the strategy distiller. Each carries
    title / when_to_use / summary so the heuristic can generalize over
    them without re-reading raw cases."""
    blocks = []
    for proc in procedures:
        blocks.append(
            f'### procedure: {proc.get("title", "")}\n'
            f'when_to_use: {proc.get("trigger", "")}\n'
            f'summary: {proc.get("summary", "")}'
        )
    return '\n\n---\n\n'.join(blocks)


__all__ = [
    'MIN_CASES_FOR_DISTILLATION',
    'DISTILLATION_DISCIPLINE',
    'STRATEGY_DISCIPLINE',
    'DistilledStep',
    'DistillProcedure',
    'DistillStrategy',
    'DistilledProcedure',
    'DistilledStrategy',
    'distill_procedure',
    'distill_strategy',
    'render_cases_markdown',
    'render_procedures_markdown',
]
