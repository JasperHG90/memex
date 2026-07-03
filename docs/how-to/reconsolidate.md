# Re-run reflection for one entity

You ingested three notes about Project Atlas in the last hour — a kickoff, a follow-up with a revised deadline, and a Slack thread that contradicts the original budget. The scheduler's reflection drain will pick the entity up on its next tick, but you want the mental model rebuilt now so the next search reflects all three notes together.

`memex memory reconsolidate` does exactly that for one entity. It runs contradiction detection across every memory unit linked to the entity, then triggers the Hindsight reflection cycle. The vault-wide cousin, `memex memory consolidate`, is for periodic cleanup — not for in-session conflict resolution.

## Prerequisites

- A running Memex server.
- The CLI or an MCP client configured against that server.
- A vault you have write access to.
- The UUID of the entity you want to reconsolidate. If you only have the name, the first step below shows how to find the ID.

## Procedure

### 1. Pick the entity

If you already noted the UUID from a prior `memex memory search` or `memex_get_entity_mentions` call, skip ahead. Otherwise, list entities and filter by name:

```bash
memex entity list --query "Project Atlas" --limit 5
```

The output table includes `ID` as its last column — copy the UUID for the entity you mean. Use `--type Person`, `--type Organization`, `--type Concept`, and so on to narrow by entity type when the name alone is ambiguous. <code-ref path="packages/cli/src/memex_cli/entities.py" lines="161-228" />

### 2. Reconsolidate the entity

Pass the UUID as a positional argument:

```bash
memex memory reconsolidate 9b4d1a7c-3f2e-4a18-8c5d-1e6f7a8b9c01
```

The default vault is your active write vault; pass `--vault <name-or-uuid>` (or the short `-v`) to target a different one. The command holds a Postgres advisory lock scoped to the entity for up to 30 seconds, runs contradiction detection over every memory unit that mentions the entity, then runs one reflection pass for the entity's mental model. It prints a JSON summary on success:

```json
{
  "entity_id": "9b4d1a7c-3f2e-4a18-8c5d-1e6f7a8b9c01",
  "vault_id": "01970000-0000-0000-0000-000000000001",
  "units_examined": 14,
  "contradictions_run": 14,
  "mental_model_id": "5e2a8f30-7b91-4c0d-9f6e-2a1b3c4d5e6f",
  "observations_added": 3,
  "abandoned": false
}
```

`observations_added` tells you how many new mental-model observations the reflection pass wrote on top of the prior version. <code-ref path="packages/cli/src/memex_cli/memory.py" lines="218-263" /> <code-ref path="packages/core/src/memex_core/services/locks.py" lines="277-381" />

This is LLM-intensive. Contradiction detection plus the full reflection cycle typically costs several LLM calls per linked unit — for a noisy entity with hundreds of units, that adds up. Reconsolidate one entity at a time, with a reason. Batch maintenance is what the scheduler is for.

From an MCP-aware agent, the equivalent call is:

```python
memex_memory_reconsolidate(
    entity_id="9b4d1a7c-3f2e-4a18-8c5d-1e6f7a8b9c01",
    vault_id="01970000-0000-0000-0000-000000000001",
)
```

Both `entity_id` and `vault_id` are required. The return dict matches the CLI shape above. <code-ref path="packages/mcp/src/memex_mcp/server.py" lines="4391-4430" />

### 3. Or: reconsolidate the whole vault

Use `memex memory consolidate` only for periodic vault-wide cleanup. It scans every active unit in the vault, computes the FSFM composite deprioritization score, and flips units below the auto-band threshold to `is_deprioritized=true`. No LLM calls — it is a SQL pass over outcome counters, importance, and decay.

```bash
memex memory consolidate --vault personal --dry-run
```

The `--dry-run` flag previews which units would flip without writing. Drop it to apply. <code-ref path="packages/cli/src/memex_cli/memory.py" lines="266-313" />

Per-entity cleanup belongs on `memex memory reconsolidate`. The vault command does no contradiction detection and runs no reflection — it is the wrong tool when the goal is "rebuild this entity's mental model".

## Verification

You have done the job if:

- The JSON return has `abandoned: false` and a non-null `mental_model_id`. The CAS UPDATE on `mental_models.version` succeeded, which means this call's reflection output is the persisted state. <code-ref path="packages/core/src/memex_core/memory/reflect/reflection.py" lines="638-724" />
- `observations_added` is greater than zero when you expected new mental-model observations. Zero is also a valid result — it means the existing model already covered every unit, nothing fresh to add.
- `memex entity view <entity_id>` shows an up-to-date mention count and reflects the just-ingested evidence. <code-ref path="packages/cli/src/memex_cli/entities.py" lines="231-280" />

## Troubleshooting

**You got a 503 with `Retry-After`.** Another reconsolidate (or a scheduler reflection pass on the same entity) is already holding the per-entity advisory lock. The server waited up to `timeout_seconds` (default 30) and gave up. Wait the duration printed in the `Retry-After` header and retry. From an MCP client, the same condition surfaces as `{"error": "lock_contention", ...}` — same fix, retry in a moment. <code-ref path="packages/core/src/memex_core/server/memories.py" lines="402-440" />

**The return is `abandoned: true`.** A concurrent worker — usually the scheduled reflection drain — refreshed the mental model between your read and your write. Your reflection pass ran, but its CAS UPDATE on `mental_models.version` lost the race. The current persisted state IS fresh; do not retry. Read the entity back via `memex entity view` or `memex_memory_search` to see the latest model. <code-ref path="packages/core/src/memex_core/services/locks.py" lines="336-365" />

**`units_examined` is 0.** No memory units in this vault are linked to that entity. Either you targeted the wrong vault (check `--vault`), the entity ID is for a vault other than the one you queried, or the entity has not been mentioned by any active note. Re-run `memex entity list --query <name>` in the right vault and double-check the UUID.

**`observations_added` is 0 and you expected new ones.** Reflection emits new observations only when fresh evidence shifts the mental model. If you have not ingested new notes since the previous reflection — or the new notes did not mention this entity in a way that produced new memory units — there is nothing for the reflection cycle to find. Check that the source note ingested cleanly with `memex note view <note-key>` and that extraction produced units linked to the entity (`memex memory search <topic>` should surface them).

**`memex memory reconsolidate: command not found`.** You are on an older release. The verb landed in 0.32. Upgrade the CLI or pin the matching server version. Until then, the MCP tool `memex_memory_reconsolidate` is the only way to drive the operation.

## See also

- [Tutorial: Getting started](../tutorials/getting-started.md)
- [How-to: Deprioritize and restore memory units](deprioritize-units.md)
- [Reference: CLI commands](../reference/cli-commands.md)
- [Explanation: Hindsight reflection](../explanation/hindsight-reflection.md)
