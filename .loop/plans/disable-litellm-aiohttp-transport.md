# disable-litellm-aiohttp-transport: stop litellm leaking aiohttp ClientSessions

## Size / Effort

**S.** One module-level assignment plus one deterministic unit test.
Effort is dominated by getting the assignment's *location* right (it
must run before any LLM call, once, globally), not by volume of code.

## Triggered by

Background batch extraction logs repeatedly emit aiohttp
`Unclosed client session` warnings (e.g. session
`bg-batch-349a975b5b77`, three per batch, clustered right after the
fact-linking / LLM extraction stage). These are noise that masks real
signal in the logs, and each leaked session drops a connection pool
(loses HTTP keep-alive across calls).

## Context

The warnings are aiohttp's `ClientSession.__del__` firing on sessions
that were never `.close()`-ed. No repo code constructs a `ClientSession`
directly; it comes from **litellm's aiohttp transport**, which is on by
default:

- `litellm.use_aiohttp_transport = True` / `litellm.disable_aiohttp_transport = False`
  are the installed defaults (verified at runtime).
- litellm's own default is documented at
  `.venv/.../litellm/__init__.py:387`:
  `disable_aiohttp_transport: bool = False  # Set this to true to use httpx instead`

The async LLM calls in this repo that hit that transport:

- `packages/core/src/memex_core/llm.py:104` and `:111` —
  `await predictor.acall(**input_kwargs)` (dspy async path → `litellm.acompletion`).
- `packages/core/src/memex_core/memory/models/backends/litellm_nli.py:74` —
  `await litellm.acompletion(...)` directly.

litellm binds its aiohttp `ClientSession` per event loop and never
closes it; when those sessions are garbage-collected the warning
fires. Timing in the logs (warnings land after fact-linking, before the
`S3AsyncFileStore` asset commit) confirms the LLM path, not s3fs, is the
source.

**Setting `litellm.disable_aiohttp_transport = True` does not desync any
call.** Verified against the installed litellm source: the async client
is always `httpx.AsyncClient`
(`.venv/.../litellm/llms/custom_httpx/http_handler.py:343`); the flag
only swaps the mounted `transport=` object. With the flag set,
`_should_use_aiohttp_transport()` returns `False`
(`http_handler.py:736-740`) and litellm builds the client on httpx's own
async transport (`_create_httpx_transport()`, `http_handler.py:837-847`).
`acall` / `acompletion` stay fully async and concurrent.

The LLM is configured once at `packages/core/src/memex_core/api.py:444-456`
(`dspy.settings.configure(lm=...)`), but the aiohttp behavior is a global
litellm module flag, so the fix belongs at a single import-time locus,
not inside per-instance API construction.

## Non-goals / out of scope

- Do NOT add a config field, env plumbing, or a settings knob for this
  (see Open Questions — litellm already reads `DISABLE_AIOHTTP_TRANSPORT`
  from the env as an OR condition if an escape hatch is ever needed).
- Do NOT touch the sync litellm paths (`litellm.embedding` in
  `litellm_embedder.py`, reranker, embedding_processor) — they use
  httpx-sync and do not leak aiohttp sessions.
- Do NOT change the dspy/LLM configuration at `api.py:444-456` beyond
  what is needed to host the flag, and do NOT alter model/timeout/retry
  behavior.
- Do NOT attempt the alternative "keep aiohttp, close the session on
  shutdown" approach; it is out of scope for this ticket.

## Requirements & restrictions

1. After the memex LLM module is imported,
   `litellm.disable_aiohttp_transport` MUST be `True`, so no code path
   (`predictor.acall`, `litellm.acompletion` in the NLI backend) opens an
   unmanaged aiohttp session.
2. The change must be **surgical** — a single assignment traceable to
   this ticket, matching the surrounding style, no refactor of adjacent
   LLM setup (`.claude/rules/` Surgical Changes; AGENTS.md §3).
3. **Simplicity first** (AGENTS.md §2): the minimum is one module-level
   line. No speculative configurability.
4. The assignment MUST run exactly once and before the first LLM call.
   It lives at module scope in `llm.py`. The correctness guarantee is the
   startup ordering: every real process constructs `MemexAPI`, whose
   `__init__` imports `llm.py` (`api.py:444-456`, `dspy` config) before
   any extraction or NLI work runs. Note the one path this does NOT cover
   by import alone: `litellm_nli.py:74` calls `litellm.acompletion`
   directly and does not import `memex_core.llm` (its model is built via
   `get_nli_model`, `memory/models/__init__.py:25` ← `scheduler.py:438`).
   That path is safe only because `MemexAPI` is already constructed in
   the running server/CLI before the scheduler drives NLI — an ordering
   fact, not an import-graph guarantee. See Resolved decision 3.
5. Every code change ships with a test that exercises it
   (`.claude/rules/python-testing.md`, `all-code-needs-tests`); the test
   must be deterministic and offline (no network, no marker needed).

## Code surface

- `packages/core/src/memex_core/llm.py` — add, at module scope near the
  existing `import litellm.exceptions` (currently line 7), an explicit
  `import litellm` and a single line
  `litellm.disable_aiohttp_transport = True` with a one-line comment
  citing that this avoids leaked aiohttp `ClientSession`s while keeping
  `acompletion`/`acall` async on httpx's transport. This is the only
  production change.
- `packages/core/tests/unit/test_llm_transport.py` — NEW test file (home
  for the test named in §8). Sits alongside the existing
  `test_run_dspy_operation_timeout.py` which already unit-tests
  `llm.py`.

## Tests & validation gates

Gates discovered from `.loop/config.json` and `justfile`:

- `just test` → `uv run pytest tests` (default suite, offline).
- `just prek` → `uv run prek run -a` (lint/type/format hooks;
  ruff + mypy per `.claude/rules/prek-code-quality.md`).

Both must pass. Do not silence any hook (`# type: ignore`, `--no-verify`).

Acceptance eval: `.loop/evals/disable-litellm-aiohttp-transport.md` (five
deterministic scenarios, all at a 100% bar).

Test to add (in `packages/core/tests/unit/test_llm_transport.py`):

- `test_llm_module_disables_aiohttp_transport` — import
  `memex_core.llm`, then assert `litellm.disable_aiohttp_transport is True`.
  Since this asserts a global module flag, guard test isolation: the test
  must not leave the flag in a state that misleads other tests (it is the
  intended production value, so leaving it `True` is correct; if the test
  mutates it, restore via `monkeypatch`/`try-finally`). A stronger,
  behavior-level assertion is also acceptable and preferred if cheap:
  assert `AsyncHTTPHandler._should_use_aiohttp_transport()` returns
  `False` after import (imported from
  `litellm.llms.custom_httpx.http_handler`), since that is the function
  litellm actually consults — this survives an internal rename of the
  bare flag.

## Risk assessment

- **Blast radius:** global — flips the transport under *every* async
  litellm call in the process. Mitigated: litellm's own docs designate
  httpx as the supported fallback; the async API surface is unchanged.
- **Reversibility:** trivial — delete one line to revert.
- **Likeliest failure modes:**
  1. Throughput/latency regression — litellm's docstring
     (`http_handler.py:700`) claims aiohttp has "much higher throughput
     and lower latency than httpx." For a background extraction workload
     this is expected to be negligible, but it is the one real trade. If
     extraction throughput is latency-bound on HTTP and regresses
     measurably, reconsider the keep-aiohttp-and-close route (a separate
     ticket).
  2. Flag set too late — if some import path issues an `acompletion`
     before `llm.py` is imported, that first call still leaks. Verify the
     assignment is at module top-level (import-time), not inside a
     function.
  3. Test isolation — a test that leaves the global flag flipped could
     mask a real regression elsewhere; keep the assertion read-only or
     restore with `monkeypatch`.

## Subtickets

1. Add `import litellm` + `litellm.disable_aiohttp_transport = True`
   (with citing comment) at module scope in `llm.py`.
   → verify: line present at import-time (module top-level), `just prek`
   clean.
2. Add `test_llm_transport.py` asserting the flag is `True` (and,
   preferably, that `_should_use_aiohttp_transport()` is `False`) after
   importing `memex_core.llm`.
   → verify: `uv run pytest tests/unit/test_llm_transport.py` passes.
3. Run full gates.
   → verify: `just test` and `just prek` both green.

## Resolved decisions

Both forks are settled by the operator; the implementer must follow
these, not re-open them.

1. **Escape hatch: hardcode, no env/config override.** Set
   `litellm.disable_aiohttp_transport = True` unconditionally at module
   scope. Do NOT read `os.getenv`, add a config field, or wire a settings
   knob (this is also a Non-goal above). Rationale: Simplicity First; the
   aiohttp throughput edge is irrelevant for this background extraction
   workload. Re-enabling aiohttp later is a deliberate one-line revert or
   a follow-up ticket, not speculative config now.
2. **Assertion strength: assert both the flag and the consulted
   function.** The test asserts `litellm.disable_aiohttp_transport is True`
   (intent) AND that `AsyncHTTPHandler._should_use_aiohttp_transport()`
   returns `False` (behavior survives a rename of the bare flag). Both are
   required rows in the eval, not optional — if the private helper import
   proves genuinely unavailable in the pinned litellm version, that is an
   `out-of-scope-fix-needed` / surface-it moment, not a silent drop to
   flag-only.
3. **Flag locus: `llm.py` module scope, single assignment.** Not
   `memex_core/__init__.py` (rejected: would add an eager `import litellm`
   to every memex_core import, penalizing non-LLM CLI/util paths) and not
   duplicated across `llm.py` + `litellm_nli.py` (rejected: two
   assignments, and a future third `acompletion` site would be missed).
   Accepted trade: correctness rests on the startup ordering documented in
   Requirement 4, which holds for every production entry point. If a
   future path calls `litellm.acompletion` before `MemexAPI` is
   constructed, that is a new ticket, not a silent regression here.
