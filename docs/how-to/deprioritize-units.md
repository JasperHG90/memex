# Deprioritize and restore memory units

You ingested a meeting note from January. Six months later, half the facts it produced no longer reflect how the team operates — old commit conventions, a deprecated deploy step, a one-off exception that became a habit and then stopped being one. Deleting the unit would lose the audit trail. Leaving it in place keeps polluting search results.

Deprioritization is the move you want. It hides the unit from default-scope retrieval without deleting it; if you change your mind, one command brings it back.

## Prerequisites

- A running Memex server.
- The CLI or MCP client configured against that server.
- A vault you have write access to.
- The UUID of the unit you want to act on. If you only have a hunch about the content, start with `memex memory search` and copy the ID from the results.

## Procedure

### 1. Find the unit

Search for content that surfaces the unit, then capture its UUID from the JSON output:

```bash
memex memory search "deploy hook on the staging job" --json | jq '.[] | {id, fact_type, text}'
```

Pick the entry that matches and copy its `id`. The minimal output mode is convenient when you already know which result is the right one and you want only the UUID:

```bash
memex memory search "deploy hook on the staging job" --minimal | head -1
```

If nothing in the default results matches, retry with `--include-stale` or with `--no-keyword` / `--no-semantic` to vary the strategy mix; the full flag list is in the CLI reference.

### 2. Deprioritize the unit

Pass the UUID and a short reason:

```bash
memex memory deprioritize 7f4c2a18-5d1e-4f9b-9c30-2a1b7c8d4e5f \
  --reason "deploy hook was removed in the Q1 rewrite"
```

The reason is free text. It is written to the audit log alongside the deprioritization event, so future maintainers (or you, six months later) can see why the unit was hidden. The default reason if you omit the flag is `manual`. <code-ref path="packages/cli/src/memex_cli/memory.py" lines="159-197" />

On success the CLI prints `Memory unit <id> deprioritized.  reason=<reason>` and the unit's `is_deprioritized` flag flips to `true`.

From an MCP-aware agent, the equivalent call is:

```python
memex_memory_deprioritize(
    unit_id="7f4c2a18-5d1e-4f9b-9c30-2a1b7c8d4e5f",
    reason="deploy hook was removed in the Q1 rewrite",
)
```

`vault_id` is optional and defaults to your active write vault. The return shape is `{"unit_id": "<uuid>", "is_deprioritized": true, "reason": "<reason>"}`. <code-ref path="packages/mcp/src/memex_mcp/server.py" lines="3781-3831" />

### 3. Confirm the unit no longer surfaces

Run the original search again — the unit should be absent:

```bash
memex memory search "deploy hook on the staging job"
```

Default search excludes deprioritized units. To verify the unit still exists (and is just hidden), look it up by ID:

```bash
memex memory view 7f4c2a18-5d1e-4f9b-9c30-2a1b7c8d4e5f
```

The `Status:` line still reads `active`. The deprioritized state is a separate boolean that retrieval filters honour by default. From MCP, set `include_deprioritized=true` on `memex_memory_search` to bring hidden units back into one specific result set. <code-ref path="packages/mcp/src/memex_mcp/server.py" lines="1483-1494" />

### 4. Restore the unit

If you change your mind, flip the flag back:

```bash
memex memory restore 7f4c2a18-5d1e-4f9b-9c30-2a1b7c8d4e5f
```

The CLI prints `Memory unit <id> restored.` and the unit re-enters default-scope search. Restore writes its own audit row; the deprioritize and restore events both stay in the log. <code-ref path="packages/cli/src/memex_cli/memory.py" lines="200-227" />

The MCP equivalent is `memex_memory_restore(unit_id, vault_id=None)`, which returns `{"unit_id": "<uuid>", "is_deprioritized": false}`. <code-ref path="packages/mcp/src/memex_mcp/server.py" lines="3834-3878" />

### 5. Archiving a whole note

If every unit extracted from one note is stale — the meeting that produced them is itself obsolete — do not loop the unit-level command. Archive the note instead. From MCP:

```python
memex_set_note_status(note_id="<note-uuid>", status="archived")
```

The note's `archived_at` timestamp is set and a cascade flips every unit derived from the note to `is_deprioritized=true`. The units stay `status=active`; they are simply filtered from default search. To bring the note back, call the same tool with `status="active"` — the cascade reverses, `archived_at` clears, and the units rejoin the default surface. <code-ref path="packages/mcp/src/memex_mcp/server.py" lines="387-444" />

`memex_set_note_status` is currently MCP-only; there is no `memex note set-status` CLI command. If you live on the CLI, archive a note via the MCP tool through your agent, or call the underlying HTTP endpoint directly.

## Verification

You have done the job if:

- `memex memory search` against the original query no longer surfaces the unit.
- `memex memory view <unit_id>` still returns the unit (it is hidden, not deleted).
- The audit log shows a `deprioritize` event with your reason — and, if you restored, a matching `restore` event after it.

## Troubleshooting

**The call returned HTTP 400 with `source_memory_units` in the body.** You passed an observation UUID, not a memory-unit UUID. Observations are read-only projections synthesized by reflection; deprioritizing them directly is not supported. The 400 body lists the underlying memory units that produced the observation — re-issue the deprioritize call against one of those IDs and the observation will refresh asynchronously on the surviving evidence. <code-ref path="packages/core/src/memex_core/services/units.py" lines="216-238" />

**Auto-band keeps re-flipping the unit.** A background scorer runs the FSFM-inspired curate pass on a timer and flips low-Memory-Worth units to `is_deprioritized=true` on its own. To stop a restored unit from being immediately re-deprioritized, the scorer enforces a cooldown window after every restore audit row — auto-banding is skipped if a `memory_restore` event exists for the unit within `cooldown_days`. If you keep racing the scheduler, lengthen the cooldown in your config, or record an outcome (`memex_record_outcome` with `verb=helpful`) so the unit's Memory Worth climbs out of the auto-band zone. <code-ref path="packages/core/src/memex_core/services/deprioritize_score.py" lines="1-50" />

**Restore worked but the unit is still missing from search.** Default-scope retrieval excludes deprioritized units, but it also applies the standard Memory Worth and decay filters at hydration. A long-dormant unit may pass the `is_deprioritized` gate and still be filtered downstream. To confirm the unit is back, query with `apply_pre_filter=false` on `memex_memory_search` — that bypasses the Memory Worth and decay screens so you see every unit that matches the query regardless of state. If the unit appears with the pre-filter off but not with it on, the fix is to feed the unit a positive outcome rather than to keep restoring it.

**The unit was already deprioritized when I called the tool.** Deprioritize is idempotent — calling it on an already-hidden unit succeeds and writes a fresh audit row with your new reason. Use that to refine the reason text without restoring first. The same applies to restore.

**I want to delete the unit, not hide it.** Use `memex memory delete <unit_id>` instead. Deletion is permanent and cascades to entity links and memory links derived from the unit; there is no undo. Deprioritization is the right move whenever you might want the data back, or whenever you want the audit trail of why a fact stopped being relevant.

## See also

- [Tutorial: Getting started](../tutorials/getting-started.md)
- [How-to: Resolve contradictions](linting.md)
- [Reference: CLI commands](../reference/cli-commands.md)
- [Explanation: Memory worth and FSFM scoring](../explanation/memory-worth.md)
