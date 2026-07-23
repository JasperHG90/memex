eval: disable-litellm-aiohttp-transport

**Definition of Done:** importing the memex LLM module globally disables
litellm's aiohttp transport (killing the leaked `Unclosed client session`
warnings) while every async LLM call stays async on httpx, and no new
config/env plumbing is introduced.

| Behavior | Input | Expected | Scorer | Threshold |
|----------|-------|----------|--------|-----------|
| Importing the LLM module turns off litellm's aiohttp transport | `import memex_core.llm` in a fresh interpreter | `litellm.disable_aiohttp_transport is True` | Deterministic (unit test) | 100% |
| litellm actually consults the flag and chooses httpx | After `import memex_core.llm`, call `AsyncHTTPHandler._should_use_aiohttp_transport()` (from `litellm.llms.custom_httpx.http_handler`) | Returns `False` — survives a rename of the bare flag because it asserts the consulted behavior | Deterministic (unit test) | 100% |
| The transport is set at import time, before any MemexAPI is built | Import `memex_core.llm` alone, construct NO `MemexAPI` and issue no LLM call | Flag is already `True` at module scope — defends against the "set too late, first call still leaks" failure mode | Deterministic (unit test) | 100% |
| Async LLM calls are preserved, not desynced (the operator's core worry) | After import, resolve litellm's async transport selection for `AsyncHTTPHandler` | The mounted transport is an httpx async transport (`httpx.AsyncHTTPTransport` or `None`, i.e. httpx's default async transport), never an `AiohttpTransport`; the client remains `httpx.AsyncClient` | Deterministic (unit test) | 100% |
| Scope guardrail: fix stays surgical, no new configurability | The production diff for this ticket (`llm.py`) | Change is a single `import litellm` + `litellm.disable_aiohttp_transport = True`; no new config field, settings knob, or `os.getenv` read introduced anywhere in the diff | Deterministic (diff inspection) | 100% |
