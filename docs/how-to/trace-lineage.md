# Trace lineage and unit history

You have a memory unit in front of you — a fact extracted from a note — and you want to know two things: where it came from, and how your view on it has changed. This guide walks you through both. Use upstream lineage to follow a fact back to the note it was extracted from; use downstream lineage to see which observations and mental models the fact later fed into; use unit history to walk the supersession chain when a newer note contradicted an older one.

Memex stores facts append-only. When a new note contradicts an older fact, the older unit is not deleted — it is superseded, and a `contradicts` or `weakens` link points from the newer authoritative unit back to the older one. Tracing that chain is how you audit a belief that has shifted over time.

## Prerequisites

- A running Memex server (local or remote) and a working CLI install. If you haven't, see *Configure Memex*.
- At least one ingested note that produced memory units. Fresh vaults will return empty trees.
- A memory unit UUID to trace. You almost always start by searching for it — the next section shows how.

## Procedure

### 1. Find the unit id

You rarely have the UUID memorised. Start with a content search:

```bash
memex memory search "JWT rotation cadence" --compact
```

`--compact` prints one line per result with the unit type and a truncated snippet. Copy the UUID of the unit you want to trace. (For richer output drop `--compact`; for JSON downstream piping, add `--json`.)

If you already know the source note's title, you can also list units linked to it:

```bash
memex memory links --note-key jwt-rotation-decision
```

### 2. Trace upstream — where did this fact come from?

Upstream lineage walks the provenance chain in the direction `mental_model -> observation -> memory_unit -> note`. For a memory unit, that means one or two hops back to its source note.

```bash
memex memory lineage memory_unit <unit_id> --direction upstream
```

The first positional argument is the entity type (`mental_model`, `observation`, `memory_unit`, or `note`). The second is the UUID. Direction defaults to `upstream`, so you can drop the flag if you prefer.

Defaults: `--depth 3` and `--limit 5` (max children per node). Increase `--depth` when the chain runs through multiple observation layers; increase `--limit` when a single observation is built from many units.

Output is a `rich` tree printed to the terminal. Add `--json` if you want to pipe the response into another tool.

### 3. Trace downstream — what did this fact spawn?

Downstream lineage walks the other direction: `note -> memory_unit -> observation -> mental_model`. Use it to answer "which mental models cite this fact?" or "which observations were built on top of this note?"

```bash
memex memory lineage note <note_id> --direction downstream --depth 5
```

You can pass `--direction both` to print upstream and downstream from the same starting entity in one call.

### 4. Walk the unit's version history (MCP)

Lineage answers *what was this derived from*. Unit history answers *what older fact did this replace*. They are different graphs. Lineage walks across entity types; history walks the contradiction graph within memory units alone.

There is no CLI command for unit history at the time of writing. Reach for the MCP tool from your agent harness (Claude Code, Hermes, or any MCP client):

```python
memex_get_unit_history(
    unit_id="<uuid>",
    vault_id="<vault-uuid-or-name>",  # required
    max_depth=10,                      # optional, default 10
)
```

The tool walks backward in time, following `contradicts` and `weakens` edges from the named unit. It returns a `UnitHistoryNodeDTO` tree rooted at the unit (depth 0), with each predecessor nested under `predecessors` and sorted oldest-first by `event_date`. Each non-root node carries a `link_type` of `contradicts` or `weakens`, naming the supersession edge from that node up to its parent. <code-ref path="packages/core/src/memex_core/services/units.py" lines="605-680" />

The walk follows the negative-evidence path only. `reinforces` edges point forward in time and are excluded in v1.

### 5. MCP signature for `memex_get_lineage` (for agents)

Agents querying lineage directly use the same shape:

```python
memex_get_lineage(
    entity_type="memory_unit",  # or mental_model, observation, note
    entity_id="<uuid>",
    direction="upstream",        # or downstream, both
    depth=3,
    limit=5,
)
```

Returns an `McpLineageNode` tree with `entity_type`, `entity`, and `derived_from` children. <code-ref path="packages/mcp/src/memex_mcp/server.py" lines="2905-2957" />

## Verification

You traced the chain successfully when:

- **Upstream from a memory unit** ends at a `note` node whose `id` matches the source note you expected. The note's title appears in the `entity` payload.
- **Downstream from a note** lists the memory units extracted from it. If the note was reflected on, observations and mental models appear at deeper levels.
- **Unit history** returns a root node (depth 0) for the unit you queried. If predecessors exist, they appear nested. If none exist, `predecessors` is an empty list — meaning nothing has contradicted or weakened this unit yet.

A useful sanity check after step 2: pick the source note's UUID from the upstream tree and run the downstream lineage on it. The original memory unit should appear in the result.

If you scripted the trace, the JSON output makes verification cheap. Pipe `--json` into `jq` to assert the expected note id appears at the leaf:

```bash
memex memory lineage memory_unit <unit_id> -d upstream --json \
  | jq '.derived_from[].entity.id'
```

The leaf entity id should match the source note you ingested.

## Troubleshooting

**`Memory unit <uuid> not found.`** The unit id is wrong, or the unit lives in a different vault. The CLI uses the unit's own `vault_id` by default; the MCP tool requires `vault_id` explicitly. Re-search with `memex memory search "<phrase>" --vault "*"` to find which vault holds the unit, then retry.

**Invalid entity type.** The lineage CLI accepts only `mental_model`, `observation`, `memory_unit`, or `note`. A typo (singular vs plural, dashes vs underscores) returns a 4xx. Use the exact strings.

**Lineage tree truncates earlier than you expected.** The default `--depth 3` is shallow. Long supersession chains or deep observation pyramids need `--depth 5` or higher. The `--limit` flag caps the number of children expanded per node — increase it when an observation was built from many units and you need to see them all.

**Unit history shows `truncated: true` on a leaf.** The walk hit `max_depth` before reaching a terminal node, or it revisited a node already seen on another branch. Re-run with a larger `max_depth`, or accept that the cap is a deliberate safety net against cycles. <code-ref path="packages/common/src/memex_common/schemas.py" lines="1126-1175" />

**Supersession chain looks one-sided.** Unit history only walks `contradicts` and `weakens` — the negative-evidence path. `reinforces` links point forward in time and are excluded in v1. If you want to see what reinforced a unit, query downstream lineage on its source note instead.

**Observation appears in the lineage tree but you cannot deprioritize it.** Observations are read-only projections of memory units (their `unit_metadata.virtual` flag is `true`). Calling `memex_memory_deprioritize` on an observation UUID returns HTTP 400 with a `source_memory_units` list — re-issue the call against one of the listed underlying memory unit IDs.

**Empty downstream tree from a note you just ingested.** Extraction is asynchronous; the reflection loop has not run yet. Wait a few seconds, or check the scheduler with `memex system status`. Once units are extracted, downstream lineage from the note will populate.

## See also

- [How-to: Deprioritize units](deprioritize-units.md)
- [How-to: Resolve contradictions](linting.md)
- [Explanation: Mental model observations](../explanation/mental-model-observations.md)
