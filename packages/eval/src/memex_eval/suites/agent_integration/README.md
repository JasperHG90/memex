# Agent Integration Suite

Tests that LLM agents — Claude Code via MCP or Hermes via the
memex-hermes-plugin — can answer questions about a Memex vault using
the agent-facing tool surface.

## Why this matters

Internal suites test memex's API directly. This suite tests the
**integration surface** — how an agent actually experiences memex.
A regression in the MCP tool descriptions, the plugin provider, or
the prompt templates surfaces here.

## Setup

The hermes backend needs `hermes-agent` (the Python library) and
`memex-hermes-plugin` (workspace package). Install both with:

```bash
uv sync --extra hermes --group hermes-integration
```

You also need an LLM API key for the agent itself. The backend routes
by model prefix:

- `gemini/*` (default `gemini/gemini-2.5-flash`) → `GOOGLE_API_KEY` or `GEMINI_API_KEY`
- `anthropic/*` → `ANTHROPIC_API_KEY`
- `openai/*` → `OPENAI_API_KEY`
- `openrouter/*` → `OPENROUTER_API_KEY`
- Anything else → `HERMES_API_KEY`

Override the model with `HERMES_MODEL`.

## Backends

- `--answer-mode hermes` (**default**): runs the Hermes Agent in-process
  via its Python library. The plugin is auto-symlinked into a temp
  `HERMES_HOME` — no `memex hermes install` step needed. This is the
  integration this suite exists to test.
- `--answer-mode claude-code`: spawns the `claude` CLI as a subagent
  with `.mcp.json` pointing at the eval vault. Captures answer + tool
  trace + cost.
- `--answer-mode api`: direct REST against the eval vault. There is no
  agent in this mode, so `agent_calls_memex_search` cannot pass. Use
  only for sanity-checking that ingest + scoring plumbing works; do
  not rely on this for integration signal.

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
