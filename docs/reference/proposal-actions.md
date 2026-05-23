# Proposal actions reference

The maintenance-proposal action registry exposes canned, reversible
mutations the cockpit can run on a finding. Each action declares its
target type, its reversibility, and the shape of `prior_state` it
captures so a later `reverse()` can roll the side effect back.

The registry lives at
`packages/core/src/memex_core/services/proposal_actions/`. Actions are
registered as a side effect of importing the package; the cockpit, the
HTTP layer, and (in a follow-on) the MCP layer look actions up by
`action.id`.

## Action contract

Every action satisfies the `ProposalAction` Protocol:

| Attribute / method | Purpose |
|--------------------|---------|
| `id: ClassVar[str]` | Stable identifier used in the HTTP body and the cockpit's option menu. |
| `name: ClassVar[str]` | Human label shown in menus. |
| `description: ClassVar[str]` | One-line description rendered alongside the menu entry. |
| `applicable_target_types: ClassVar[tuple[str, ...]]` | Filter the cockpit applies before listing the action. Actions that do not apply to a proposal's `target_type` are hidden, not greyed out. |
| `reversible: ClassVar[bool]` | When `False`, the server's `/reverse` endpoint short-circuits to `409 {reason: 'forward_only'}` without writing an audit row. |
| `validate(params, *, target_type, target_id)` | Synchronous; raises `ActionValidationError` (→ HTTP 400) for impossible params. Runs before any side effect. |
| `async execute(api, params, *, target_id, vault_id, actor)` | Run the mutation. Returns `ExecuteResult(applied_state, prior_state)`. The server stamps both under `evidence.resolution.followup`. |
| `async reverse(api, params, applied_state, prior_state, *, target_id, vault_id, actor)` | Undo the side effects captured under `prior_state`. Forward-only actions should also raise `ProposalActionError` here for defence-in-depth. |
| `async preview(api, params, *, target_id, vault_id)` | Read-only — returns a one-line description of the blast radius. Defined on the protocol so future cockpit / MCP code can render a dry-run before commit; **not invoked by the TUI in this release** (the cockpit uses the static `effect` field on `CockpitOption` from `controller.py`). |

## Built-in actions

### `no_op`

| | |
|---|---|
| Target types | `memory_unit`, `mental_model`, `note`, `unit_entity` |
| Reversible | yes (trivially) |
| Effect | Records the verdict and optional note; no state mutation. |
| Used by | `llm_schema_drift` (recommended), `cold_low_mw_unit`, `composite_deprioritize_candidate`, `claim_too_aggressive`, `sensitive_unreviewed_unit`, every "Other" mapping that wants pure-audit semantics. |

### `deprioritize_unit`

| | |
|---|---|
| Target types | `memory_unit` |
| Reversible | yes — paired with `restore_unit` |
| Effect | Calls `MemexAPI.deprioritize_memory_unit`. Flips `MemoryUnit.is_deprioritized=true`; scans `mental_models.observations` for citing observations and enqueues one `refresh_observation` task per match. |
| `prior_state` | `{unit_id, is_deprioritized: False}` |
| Used by | `cold_low_mw_unit` (recommended), `composite_deprioritize_candidate` (recommended), `llm_semantic_contradiction` (recommended on the lower-confidence side), `claim_too_aggressive`, `llm_schema_drift`, `sensitive_unreviewed_unit`. |
| Reverse semantics | Calls `restore_memory_unit`. Refresh tasks queued during the apply are not cancelled — they observe the live state when they run, so a restored unit will re-surface in observations naturally. |

### `restore_unit`

| | |
|---|---|
| Target types | `memory_unit` |
| Reversible | yes — paired with `deprioritize_unit` |
| Effect | Calls `MemexAPI.restore_memory_unit`. Clears `is_deprioritized`. |
| `prior_state` | `{unit_id, is_deprioritized: True}` |
| Used by | Manual reversal flow; rarely selected directly. |

### `archive_mental_model`

| | |
|---|---|
| Target types | `mental_model` |
| Reversible | yes |
| Effect | `UPDATE mental_models SET archived_at = now() WHERE id = … AND vault_id = … AND archived_at IS NULL` (CAS). Retrieval (`TEMPR.MentalModelStrategy`), session briefing, and the reflection engine's mental-model lookups all filter `WHERE archived_at IS NULL`, so the archive freezes the row's content for audit. Survey reads MentalModel through retrieval, so it inherits the filter. The row's `observations`, `entity_metadata`, and `embedding` are untouched. |
| `prior_state` | `{mental_model_id, archived_at: None}` |
| Used by | `orphan_mental_model` (recommended). |
| Reverse semantics | `UPDATE … SET archived_at = NULL …`. The model immediately re-surfaces in retrieval and briefing. Reflection invariants are preserved because content was never touched. |

## Forward-only actions (deferred — not yet wired)

The plan calls out `regenerate_mental_model` as a forward-only action:
it enqueues a priority reflect for the model's entity and lets the
reflection engine rebuild observations from current evidence. There is
no way to "undo" a regenerate — the next regenerate is a forward
operation, not a reverse — so the action declares `reversible = False`
and the server returns `409 {reason: 'forward_only'}` on `/reverse`.

This action is not registered in this release; it ships in the
follow-on PR that wires the orphan-mental-model rule to a multi-option
menu (archive + regenerate). For now `orphan_mental_model` shows
`archive_mental_model` (recommended), `no_op`, and `dismiss`.

## Adding a new action

1. Create a new file under
   `packages/core/src/memex_core/services/proposal_actions/`. The
   module declares one class that satisfies `ProposalAction` and
   calls `register_action(...)` at module scope.
2. Re-export the module name from `proposal_actions/__init__.py` so
   the import side effect fires when the package loads.
3. Add the action to the relevant rule's option list in
   `packages/cli/src/memex_cli/cockpit/controller.py`
   (`_DEFAULT_OPTIONS_BY_RULE`). Mark exactly one option per rule as
   `recommended=True`.
4. Write a unit test under
   `packages/core/tests/unit/services/test_proposal_actions.py` —
   the existing tests are the template (execute/reverse round-trip
   against a fake api, target-type filtering).
5. Add the action row to this reference page.

The action registry intentionally has no auto-discovery: every action
is registered explicitly so the catalogue is grep-able, and the cockpit
catalogue is small enough that hand-curation pays off.

## Endpoints that read the registry

| HTTP path | What it calls |
|-----------|---------------|
| `POST /api/v1/lint/findings/{id}/resolve` with `{action, params, note}` | `action.validate` → `action.execute` → write `evidence.resolution` → flip status to `resolved`. |
| `POST /api/v1/lint/findings/{id}/reverse` | Read `evidence.resolution.followup.action`; if `action.reversible` is `False`, return 409 forward_only; else call `action.reverse` with the captured `prior_state` and write `evidence.resolution.reversal`. |

The cockpit drives both via the `lint_resolve` / `lint_reverse` client
methods on `MemexClient`. Agent-facing (MCP) wiring for the registry
ships in a follow-on PR.
