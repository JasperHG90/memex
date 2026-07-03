"""Case → procedure assignment judge (the design §18.1 / §19.3 / spike #3c).

One LLM judgment per submitted case: does this episode *instance* an
existing procedure, or seed a new one? The judge is FORCED to commit
(no abstain branch — spike §19.3 showed models volunteer ``ambiguous``
on 0/5 designed toss-ups) and instead self-assesses through a REQUIRED
``separation`` field (spike #3c: 4/5 toss-up recall, 2/16 noise,
decisions unharmed). The *system* escalates on ``separation !=
'clean'`` — ``ambiguous`` is a system-derived state, never a
model-volunteered one.

Candidates come from the trigger-embedding search (stage 1 of the §18.1
two-stage flow; candidate recall was 10/10 in the spike). The judgment
runs through ``run_dspy_operation`` — the production executor with
circuit breaker + metrics — on the configured default model.

§19.3 implementation note (a): optional string outputs on the
production model return literal ``'None'`` / ``'null'`` and
quote-wrapped values — :func:`_normalise` scrubs them.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Literal

import dspy
from pydantic import BaseModel, Field

from memex_core.llm import run_dspy_operation

logger = logging.getLogger('memex.core.memory.procedural_assignment')


class AssignmentCandidate(BaseModel):
    """One stage-1 candidate procedure shown to the judge."""

    entry_id: str = Field(description='UUID of the candidate procedural entry.')
    verb: str = Field(description='Anchor verb (e.g. "deploy").')
    context: str = Field(description='Anchor context (e.g. "nomad").')
    scope: str = Field(description='Anchor scope (global | project:<id> | app:<id>).')
    trigger: str = Field(description='when_to_use phrase of the candidate.')
    title: str = Field(description='Candidate title.')


class AssignCaseToProcedure(dspy.Signature):
    """Decide whether a worked case is an instance of an existing procedure
    or evidence for a new one.

    You MUST commit to a decision — there is no abstain option. Assess how
    contested the decision was through the `separation` field instead:
    'clean' when no candidate came close to the winner; 'close_call' when a
    runner-up was plausible; 'overlapping' when two or more candidates (or
    candidate-vs-new) are genuinely hard to tell apart. Prefer reusing an
    existing verb from `existing_verbs` when proposing a new procedure;
    only coin a new verb when none fits, and justify it in `reasoning`.
    """

    case_trigger: str = dspy.InputField(desc='What kicked the episode off.')
    case_summary: str = dspy.InputField(desc='Title + outcome + condensed actions of the case.')
    candidates_json: str = dspy.InputField(
        desc='JSON array of candidate procedures (entry_id, verb, context, scope, trigger, title).'
    )
    existing_verbs: str = dspy.InputField(
        desc='Comma-separated verb vocabulary already in use for this scope.'
    )
    requested_scope: str = dspy.InputField(
        desc='The scope the submitter chose (global | project:<id> | app:<id>).'
    )
    scope_reasoning: str = dspy.InputField(
        desc="Submitter's one-sentence justification for the chosen scope."
    )

    decision: Literal['instance_of', 'new_procedure'] = dspy.OutputField(
        desc='instance_of: the case enacted one of the candidates. '
        'new_procedure: no candidate matches; propose an anchor.'
    )
    target_entry_id: str = dspy.OutputField(
        desc='entry_id of the matched candidate when decision=instance_of; empty otherwise.'
    )
    proposed_verb: str = dspy.OutputField(
        desc='Anchor verb for the new procedure when decision=new_procedure; empty otherwise. '
        'Lowercase slug ([a-z][a-z0-9_-]*).'
    )
    proposed_context: str = dspy.OutputField(
        desc='Anchor context for the new procedure when decision=new_procedure; empty otherwise. '
        'Lowercase slug.'
    )
    separation: Literal['clean', 'close_call', 'overlapping'] = dspy.OutputField(
        desc='REQUIRED self-assessment of how contested the decision was.'
    )
    runner_up: str = dspy.OutputField(
        desc='entry_id (or proposed anchor) of the second-best option; empty when none.'
    )
    reasoning: str = dspy.OutputField(desc='One short paragraph supporting the decision.')
    proposed_scope: str = dspy.OutputField(
        desc='The scope this procedure/strategy should live under. '
        'Usually matches requested_scope; override only when the episode '
        'clearly belongs elsewhere. Lowercase scope grammar.'
    )
    scope_separation: Literal['clean', 'close_call', 'overlapping'] = dspy.OutputField(
        desc='REQUIRED self-assessment of how clear the scope choice is '
        'given the trigger and actions.'
    )


@dataclass(frozen=True)
class AssignmentJudgment:
    """Normalised judge output consumed by the case service."""

    decision: Literal['instance_of', 'new_procedure']
    target_entry_id: str | None
    proposed_verb: str | None
    proposed_context: str | None
    separation: Literal['clean', 'close_call', 'overlapping']
    runner_up: str | None
    reasoning: str
    proposed_scope: str | None
    scope_separation: Literal['clean', 'close_call', 'overlapping'] | None

    @property
    def is_clean(self) -> bool:
        return self.separation == 'clean'

    @property
    def is_scope_clean(self) -> bool:
        return self.scope_separation == 'clean'

    def as_dict(self) -> dict[str, str | None]:
        return {
            'decision': self.decision,
            'target_entry_id': self.target_entry_id,
            'proposed_verb': self.proposed_verb,
            'proposed_context': self.proposed_context,
            'separation': self.separation,
            'runner_up': self.runner_up,
            'reasoning': self.reasoning,
            'proposed_scope': self.proposed_scope,
            'scope_separation': self.scope_separation,
        }


def _normalise(value: object) -> str | None:
    """Scrub the §19.3 string-output artefacts: literal 'None'/'null',
    quote-wrapped values, surrounding whitespace."""
    if value is None:
        return None
    text = str(value).strip().strip('"').strip("'").strip()
    if not text or text.lower() in ('none', 'null', 'n/a'):
        return None
    return text


async def judge_assignment(
    lm: dspy.LM,
    *,
    case_trigger: str,
    case_summary: str,
    candidates: list[AssignmentCandidate],
    existing_verbs: list[str],
    requested_scope: str,
    scope_reasoning: str,
    timeout: int = 60,
) -> AssignmentJudgment:
    """Run one assignment judgment. Raises on executor failure — the
    case service maps any raise to the escalation path (fail-safe:
    a missed judgment is a lint-queue review, never a lost case)."""
    predictor = dspy.Predict(AssignCaseToProcedure)
    result = await run_dspy_operation(
        lm,
        predictor,
        {
            'case_trigger': case_trigger,
            'case_summary': case_summary,
            'candidates_json': json.dumps([c.model_dump() for c in candidates]),
            'existing_verbs': ', '.join(sorted(set(existing_verbs))) or '(none yet)',
            'requested_scope': requested_scope,
            'scope_reasoning': scope_reasoning,
        },
        operation_name='procedural.assign_case',
        timeout=timeout,
    )

    decision = _normalise(result.decision) or 'new_procedure'
    if decision not in ('instance_of', 'new_procedure'):
        decision = 'new_procedure'
    separation = _normalise(result.separation) or 'overlapping'
    if separation not in ('clean', 'close_call', 'overlapping'):
        # Unknown self-assessment reads as contested — escalation is the
        # fail-safe branch (§19.3c failure asymmetry).
        separation = 'overlapping'

    target = _normalise(result.target_entry_id)
    if decision == 'instance_of' and target is not None:
        # The judge must point at a real candidate; a hallucinated id is
        # a contested outcome, not an assignment.
        known = {c.entry_id for c in candidates}
        if target not in known:
            logger.warning('assignment judge pointed at unknown candidate %r; escalating', target)
            separation = 'overlapping'
            target = None

    proposed_scope = _normalise(result.proposed_scope) or requested_scope
    scope_separation = _normalise(result.scope_separation) or 'overlapping'
    if scope_separation not in ('clean', 'close_call', 'overlapping'):
        scope_separation = 'overlapping'

    return AssignmentJudgment(
        decision=decision,  # type: ignore[arg-type]
        target_entry_id=target,
        proposed_verb=_normalise(result.proposed_verb),
        proposed_context=_normalise(result.proposed_context),
        separation=separation,  # type: ignore[arg-type]
        runner_up=_normalise(result.runner_up),
        reasoning=_normalise(result.reasoning) or '',
        proposed_scope=proposed_scope,
        scope_separation=scope_separation,  # type: ignore[arg-type]
    )


__all__ = [
    'AssignCaseToProcedure',
    'AssignmentCandidate',
    'AssignmentJudgment',
    'judge_assignment',
]
