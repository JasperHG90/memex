"""Suite-private outcomes for ``agent_integration``.

Currently exposes ``deprio_recovers_from_400``: asserts the agent
ends up deprioritizing the underlying memory units that an observation
cites, exercising the V21 contract where passing an observation UUID
to ``memex_memory_deprioritize`` returns HTTP 400 with the
``source_memory_units`` payload that the agent's MCP tool description
tells it to retry against.
"""

from __future__ import annotations

from typing import Any, Literal

from memex_eval.suite.agents import AgentAnswer
from memex_eval.suite.base import ExpectedOutcomeBase, register_outcome


@register_outcome('deprio_recovers_from_400')
class DeprioRecoversFrom400(ExpectedOutcomeBase):
    """Pass iff the agent deprioritizes at least one of the observation's
    source memory units.

    The seeded scenario carries:
    - ``context['seed_mental_model_observation.observation_id']`` — the
      observation UUID the agent may try first; the server 400s on it.
    - ``context['seed_mental_model_observation.source_mu_ids']`` — the
      list of MU UUIDs cited as evidence; the server's 400 payload
      surfaces these and the agent's MCP tool description tells it
      to retry against one of them.

    Pass criteria (lenient — see note below):
    - At least one ``memex_memory_deprioritize`` call passes a
      ``unit_id`` that's in ``source_mu_ids``.

    Stricter variant we could enforce in a future tightening:
    - The agent attempts the observation UUID FIRST, THEN retries
      against one of the MUs (proves the 400 contract drove the
      recovery).
    We don't enforce that today because some agents legitimately
    resolve the observation -> source_mu mapping client-side from
    their search results' shape and skip the observation UUID
    entirely. What V21 ships is "the agent ends up doing the right
    write"; observability into whether the 400 actually happened is
    a server-side metric, not an agent-behavior assertion.

    Reported metrics:
    - ``pass`` (1.0 / 0.0)
    - ``observation_call_count`` (informational — how many times the
      agent passed the observation UUID; useful for the future
      stricter variant).
    - ``mu_call_count`` (informational — how many MU-targeted
      deprio calls the agent issued).
    """

    type: Literal['deprio_recovers_from_400']
    # Optional context-key overrides for callers that want to point at
    # a different seed action's namespace. Defaults match the auto-prefix
    # the runner applies to ``seed_mental_model_observation``.
    observation_id_context_key: str = 'seed_mental_model_observation.observation_id'
    source_mu_ids_context_key: str = 'seed_mental_model_observation.source_mu_ids'

    def score(
        self,
        answer: AgentAnswer,
        scenario,
        *,
        context: dict[str, Any] | None = None,
        **_kw,
    ) -> dict[str, float]:
        ctx = context or {}
        observation_id = str(ctx.get(self.observation_id_context_key) or '').strip().lower()
        source_mu_ids_raw = ctx.get(self.source_mu_ids_context_key) or []
        source_mu_ids: set[str] = {
            str(m).strip().lower() for m in source_mu_ids_raw if m is not None
        }

        observation_call_count = 0
        mu_call_count = 0
        for call in answer.tool_calls:
            if call.get('tool') != 'memex_memory_deprioritize':
                continue
            args = call.get('input') or {}
            raw_unit_id = args.get('unit_id')
            if raw_unit_id is None:
                continue
            unit_id = str(raw_unit_id).strip().lower()
            if observation_id and unit_id == observation_id:
                observation_call_count += 1
            if unit_id in source_mu_ids:
                mu_call_count += 1

        passed = mu_call_count >= 1
        return {
            'pass': 1.0 if passed else 0.0,
            'observation_call_count': float(observation_call_count),
            'mu_call_count': float(mu_call_count),
        }

    def metric_keys(self, top_k: int | None = None) -> list[str]:
        return ['pass', 'observation_call_count', 'mu_call_count']


@register_outcome('tool_call_order')
class ToolCallOrder(ExpectedOutcomeBase):
    """Pass iff ``before`` is first called strictly before ``after``.

    The load-bearing ordering gate for longer-horizon procedural flows:
    the agent must SEARCH/PROBE the plane *before* it WRITES (so it
    reuses or updates rather than blindly re-creating). We compare the
    index of the first ``before`` call against the first ``after`` call
    in ``answer.tool_calls`` (which the hermes/claude-code backends emit
    in invocation order).

    Pass requires BOTH tools to have been called AND the first ``before``
    to precede the first ``after``. If either is absent, the scenario
    fails (the multi-step flow didn't happen). Reports ``pass`` plus
    informational first-index metrics.
    """

    type: Literal['tool_call_order']
    before: str
    after: str

    def score(self, answer: AgentAnswer, scenario: Any = None, **_kw: Any) -> dict[str, float]:
        before_idx: int | None = None
        after_idx: int | None = None
        for i, call in enumerate(answer.tool_calls):
            tool = call.get('tool')
            if tool == self.before and before_idx is None:
                before_idx = i
            if tool == self.after and after_idx is None:
                after_idx = i
        ordered = before_idx is not None and after_idx is not None and before_idx < after_idx
        return {
            'pass': 1.0 if ordered else 0.0,
            'before_first_index': float(before_idx if before_idx is not None else -1),
            'after_first_index': float(after_idx if after_idx is not None else -1),
        }

    def metric_keys(self, top_k: int | None = None) -> list[str]:
        return ['pass', 'before_first_index', 'after_first_index']
