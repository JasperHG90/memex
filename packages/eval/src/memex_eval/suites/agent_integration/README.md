# Agent Integration Suite

Tests that LLM agents — Claude Code via MCP or Hermes via the
memex-hermes-plugin — can answer questions about a Memex vault using
the agent-facing tool surface.

## Why this matters

Internal suites test memex's API directly. This suite tests the
**integration surface** — how an agent actually experiences memex.
A regression in the MCP tool descriptions, the plugin provider, or
the prompt templates surfaces here.

## Backends

- `--answer-mode api` (default for CI smoke): direct API; 1 of 3
  scenarios will fail (`agent_calls_memex_search` — there are no tool
  calls in API mode). This is expected.
- `--answer-mode claude-code`: spawns the `claude` CLI as a subagent
  with `.mcp.json` pointing at the eval vault. Captures answer + tool
  trace + cost.
- `--answer-mode hermes`: spawns `hermes` CLI with the
  `memex-hermes-plugin` providing memory. Run `memex hermes install`
  once before invoking.

## Components under test

- `packages/mcp/` — MCP tool registration + descriptions
- `packages/hermes-plugin/src/memex_hermes_plugin/memex/provider.py` — Hermes memory provider
- `packages/core/src/memex_core/server/` — request handlers the agent hits

## Primary metrics

- `suite.pass_rate` — pass count
- `metric.graded_score.mean` — judge's grade of the agent's final answer
- `cost.total_usd` — agent inference cost
- `tokens.total_in` / `tokens.total_out` — agent token usage

## Custom backends

Register your own via:

```python
from memex_eval.suite import register_backend, AnswerBackend, AgentAnswer

@register_backend('my-agent')
class MyAgentBackend(AnswerBackend):
    async def answer(self, scenario, *, api, vault_id, server_url, judge=None):
        # call your agent, return AgentAnswer
        ...
```

Then run `memex-eval suite run agent_integration --answer-mode my-agent`.
