# Proposal actions reference

The maintenance-proposal action registry exposes canned, reversible
mutations the cockpit can run on a finding. Each action declares its
target type, its reversibility, and the shape of `prior_state` it
captures so a later `reverse()` can roll the side effect back.

The registry lives at
`packages/core/src/memex_core/services/proposal_actions/`. Actions are
registered as a side effect of importing the package; the cockpit, the
HTTP layer, and the MCP layer look actions up by `action.id`.

The catalogue is **closed**: actions cannot be registered at runtime and
only grow with core releases. External tools participate by submitting
*proposals* (`POST /api/v1/lint/proposals`) that pair caller-owned rule
metadata with a suggestion from this catalogue — see the
[linting how-to](../how-to/linting.md) for the submission flow.

## Action contract

Every action satisfies the `ProposalAction` Protocol:

| Attribute / method | Purpose |
|--------------------|---------|
| `id: ClassVar[str]` | Stable identifier used in the HTTP body and the cockpit's option menu. |
| `name: ClassVar[str]` | Human label shown in menus. |
| `description: ClassVar[str]` | One-line description rendered alongside the menu entry. |
| `applicable_target_types: ClassVar[tuple[str, ...]]` | Filter the cockpit applies before listing the action. Actions that do not apply to a proposal's `target_type` are hidden, not greyed out. |
| `reversible: ClassVar[bool]` | When `False`, the server's `/reverse` endpoint short-circuits to `409 {reason: 'forward_only'}` without writing an audit row. |
| `params_schema: ClassVar[dict \| None]` | JSON schema for `params` (from a Pydantic params model's `model_json_schema()`), or `None` for parameterless actions. Discoverability only — `validate()` stays the execution-time gate. Served verbatim by `GET /api/v1/lint/actions` so external submitters and review UIs can render parameter forms. |
| `validate(params, *, target_type, target_id)` | Synchronous; raises `ActionValidationError` (→ HTTP 400) for impossible params. Runs before any side effect — including at proposal-submission time when a proposal carries a `proposed_action`. |
| `async execute(api, params, *, target_id, vault_id, actor)` | Run the mutation. Returns `ExecuteResult(applied_state, prior_state)`. The server stamps both under `evidence.resolution.followup`. |
| `async reverse(api, params, applied_state, prior_state, *, target_id, vault_id, actor)` | Undo the side effects captured under `prior_state`. Forward-only actions should also raise `ProposalActionError` here for defence-in-depth. |
| `async preview(api, params, *, target_id, vault_id)` | Read-only — returns a one-line description of the blast radius, served by `POST /api/v1/lint/findings/{id}/preview`. The review surfaces render it before confirming any irreversible action. |

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

### `route_note_to_vault`

| | |
|---|---|
| Target types | `note` |
| Reversible | yes — reverse migrates the note back to its source vault |
| Params | `{target_vault_id: uuid, other_vault_ids?: [uuid]}` |
| Effect | Calls `MemexAPI.migrate_note` (note + chunks + nodes + units + links move vaults atomically; filestore paths rewrite). |

### Note lifecycle and metadata

| Action | Params | Reversible | Effect |
|---|---|---|---|
| `set_note_status` | `{status: active\|superseded\|archived, linked_note_id?}` | yes — prior lifecycle fields (status, superseded_by, archived_at) are snapshotted and re-applied; a prior `appended_to` pointer is NOT restorable, so reverse refuses in that case | Delegates to `MemexAPI.set_note_status` with its full cascades (superseded → units stale; archived → units deprioritized; active → reactivate). |
| `update_note_title` | `{new_title}` | yes — prior title restored | Replaces the title; embedded title facts re-extract. Reverse refuses when the prior title was empty. |
| `update_note_date` | `{new_date: ISO-8601}` | yes — prior publish date restored | Replaces `publish_date`; child memory-unit timestamps cascade. Reverse refuses when the prior date was unset. |

### Entity merges (forward-only)

| Action | Params | Effect |
|---|---|---|
| `merge_entities` | `{winner_id, member_ids}` — winner must be ∈ member_ids, ≥2 distinct members | Folds the listed entities onto the winner via `EntityService.collapse_cluster` (links, aliases, counters, per-vault mental models merge; losers hard-deleted). |
| `collapse_into_new_entity` | `{new_canonical_name, member_ids}` — ≥2 distinct members | Creates a bare survivor entity, then folds ALL listed members onto it via the same audited collapse. Rejects a `new_canonical_name` that already exists (merge into that winner instead). |

Because the action protocol receives only `params` (never the finding's
evidence), `member_ids` is the explicit, authoritative merge list — the
review surfaces fill it from `evidence.cluster_members`; external
callers supply it directly. Both actions are `reversible = False`: the
merged entities are hard-deleted.

### Other forward-only actions

| Action | Target types | Params | Effect |
|---|---|---|---|
| `kv_delete` | `kv` | `{key?}` (defaults to the finding's `target_id`) | Hard-deletes the KV entry. History, TTL, and embedding state are not restorable; the deleted value is deliberately NOT captured into the resolution payload (KV rows can hold sensitive preferences). |
| `record_outcome` | `memory_unit` | `{verb: helpful\|not_helpful\|not_used, reason?}` — reason REQUIRED for the credit-bearing verbs | Appends an outcome to the unit's Memory-Worth ledger. Append-only by design: decrementing to "undo" would corrupt the posterior's evidence count. |

### Hard deletes (forward-only, fenced)

| Action | Target types | Effect |
|---|---|---|
| `delete_note` | `note` | Hard-deletes the note + memory units + chunks + nodes + links + filestore assets. |
| `delete_entity` | `entity` | Hard-deletes the entity + mental models + aliases + links + unit-entity rows. |
| `delete_mental_model` | `entity`, `mental_model` | Hard-deletes this vault's mental model row; the parent entity is untouched. Reflection rebuilds a fresh model from surviving units over time. |

Containment is layered: the resolve endpoint's attended-mode gate covers
every canned action, the human picks the action in the review surface,
and each delete ships a live blast-radius `preview()` (computed from the
database) that renders before confirmation. Prefer the lifecycle-state
alternatives (`set_note_status`, `archive_mental_model`,
`deprioritize_unit`) unless the content must actually go away.

## Adding a new action

1. Create a new file under
   `packages/core/src/memex_core/services/proposal_actions/`. The
   module declares one class that satisfies `ProposalAction` and
   calls `register_action(...)` at module scope. Parameterized actions
   declare a private Pydantic params model and set
   `params_schema = _Params.model_json_schema()`.
2. Re-export the module name from `proposal_actions/__init__.py` so
   the import side effect fires when the package loads.
3. Optionally add the action to a rule's option list in
   `packages/cli/src/memex_cli/cockpit/controller.py`
   (`_DEFAULT_OPTIONS_BY_RULE`) and to its static fallback catalogue —
   the cockpit swaps in the live registry from `GET /lint/actions` on
   first fetch, so new actions appear in the menus without a CLI
   release; the static rows only matter offline.
4. Write a unit test under
   `packages/core/tests/unit/services/test_proposal_actions.py` —
   the existing tests are the template (execute/reverse round-trip
   against a fake api, target-type filtering, forward-only fences).
5. Add the action row to this reference page.

The action registry intentionally has no auto-discovery: every action
is registered explicitly so the catalogue is grep-able and the closed
set stays auditable.

## Endpoints that read the registry

| HTTP path | What it calls |
|-----------|---------------|
| `GET /api/v1/lint/actions` | The full catalogue in wire shape (`id`, `name`, `description`, `applicable_target_types`, `reversible`, `params_schema`). Read-only discoverability for external submitters and review UIs. |
| `POST /api/v1/lint/proposals` | Validates a proposal's optional `proposed_action` against the registry at submission time (unknown action / target mismatch / bad params → the item is rejected). |
| `POST /api/v1/lint/findings/{id}/preview` with `{action, params}` | `action.validate` → `action.preview` — read-only blast radius, no mutation. |
| `POST /api/v1/lint/findings/{id}/resolve` with `{action, params, note}` | `action.validate` → `action.execute` → write `evidence.resolution` → flip status to `resolved`. |
| `POST /api/v1/lint/findings/{id}/reverse` | Read `evidence.resolution.followup.action`; if `action.reversible` is `False`, return 409 forward_only; else call `action.reverse` with the captured `prior_state` and write `evidence.resolution.reversal`. |

The cockpit drives these via the `lint_resolve` / `lint_reverse` /
`list_lint_actions` / `lint_preview_action` client methods. Agents reach
the registry through the `memex_list_lint_actions` and
`memex_submit_lint_proposal` MCP tools.
