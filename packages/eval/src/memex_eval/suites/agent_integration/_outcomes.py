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
