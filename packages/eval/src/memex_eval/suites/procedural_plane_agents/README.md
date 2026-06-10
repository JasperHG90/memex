# procedural_plane_agents — V7 agent-mode regression gate

Pins the V7 procedural-plane *agent-facing* contract: does the LLM agent
(Hermes or Claude Code) actually reach for the `memex_procedural_*` tools
when it should, and does the routing land on the V7 plane (NOT on the
legacy KV-namespace procedure path)?

Sister suite to `procedural_plane` (which drives `api.procedural_*`
directly via `DirectApiBackend`, no LLM in the loop). This suite catches
routing regressions that the API-mode suite cannot — a working API
endpoint is still useless if the agent sends "rotate creds" to
`memex_kv_put` instead of `memex_procedural_create`.

## Scenarios

| Scenario | Group | Tool under test | Discriminator |
|----------|-------|------------------|---------------|
| `stores_a_case_via_procedural_create` | store | `memex_procedural_create` | `kind="case"` + non-empty `trigger` |
| `retrieves_procedure_via_search` | retrieve | `memex_procedural_search` | `query` mentions `rotate|cred|api|key` |
| `probes_identity_before_writing` | store | `memex_procedural_get_by_identity` | `kind="procedure"`, `verb="rotate"`, `context="creds"` (read-before-write contract) |
| `loads_briefing_cards_at_session_start` | retrieve | `memex_procedural_briefing_cards` | non-empty `context_keys` list |

`mutating_scenario=True` is set on the two store-group scenarios
(`stores_a_case_*`, `probes_identity_before_writing`) — they call
`create`/`update` and the runner needs to know not to reuse a vault
without explicit opt-in.

`replicates_override=2` on the same two scenarios — the read-before-write
contract is the load-bearing one; a single replay can pass by luck.

## Why this suite exists

A case is briefing-eligible. If the agent routes a trigger signal
("when CI returns 500 after step 3...") to `memex_kv_put`, the signal
is invisible to `memex_procedural_briefing_cards` and vault briefings
silently miss the failure pattern. The `kind="case"` regex on
`memex_procedural_create` is the discriminator that catches that
routing bug.

A procedure with `verb="rotate"`, `context="creds"` is *also* a
candidate for the legacy `<scope>:procedure:rotate:creds` KV key.
If the agent prefers the KV path, the entry is invisible to
`memex_procedural_search` and `memex_procedural_briefing_cards`. The
`ToolCallContains(memex_procedural_search)` assertion catches that.

The `probes_identity_before_writing` scenario is the operational
contract: when the agent is told to remember a procedure that already
exists, it MUST call `memex_procedural_get_by_identity` first.
Without that probe, the agent's `create` call 409s and the retry loop
hammers the server.

## Out of scope

- The actual correctness of the LLM's final prose answer (covered by
  `LLMJudge` in the composite). This suite gates tool routing only.
- Ranking precision of `memex_procedural_search` (covered by the
  `procedural_plane` sister suite, which drives the API directly).
- Briefing card content quality (covered by the sister suite's
  `briefing_cards` API-mode scenario).

## Side-effect imports

`__init__.py` imports `procedural_plane._setup_actions` for its
decorator side-effect (registers the `procedural_upsert` setup
action). Without this import, the three retrieve/read scenarios
that pre-seed a procedure on the (procedure, global, rotate, creds)
anchor cannot resolve `SetupAction(kind='procedural_upsert', ...)` at
register-time.
