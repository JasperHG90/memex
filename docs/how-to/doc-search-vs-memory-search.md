# How to Choose Between Document Search and Memory Search

This guide shows you how to pick the right search interface in Memex and use each one effectively.

## Prerequisites

* A running Memex server with ingested content
* Familiarity with the `memex` CLI

## When to Use Each Search

**Use Note Search** when you need:
- A specific passage, quote, or paragraph from a source document
- The original formatting and context of a note
- Results scoped to a specific vault or set of documents
- Raw information that has not been consolidated by reflection

**Use Memory Search** when you need:
- A synthesized answer spanning your entire knowledge base
- Facts connected across multiple source documents
- Entity relationships and temporal context
- Insights from the reflection engine (mental models)

## Instructions

### 1. Search Source Documents with Note Search

```bash
memex note search "deployment architecture"
```

**Example output:**
```
1. Infrastructure Planning Doc (Score: 0.847)
   Snippets:
   - [Deployment] The production cluster runs on 3 nodes with...
   - [Architecture] Services communicate via gRPC with...

2. Meeting Notes 2025-01-15 (Score: 0.723)
   Snippets:
   - [Action Items] Migrate to Kubernetes by Q2...
```

**Use `--reason` to see why sections matched:**

```bash
memex note search "deployment architecture" --reason
```

This activates Skeleton-Tree Identification — Memex scans the logical structure (table of contents) of each matching note and explains which sections are relevant and why.

**Use `--summarize` to get a synthesized answer:**

```bash
memex note search "deployment architecture" --summarize
```

Memex finds the relevant sections, then generates a direct answer grounded in those sections.

**Scope to a specific vault:**

```bash
memex note search "deployment architecture" --vault project-x
```

### 2. Search the Knowledge Graph with Memory Search

```bash
memex memory search "What is our deployment strategy?"
```

**Example output:**
```
1. [Type: fact] (Score: 0.91) (Date: 2025-01-15)
   The production environment uses a blue-green deployment strategy
   with automated rollback triggers.

2. [Type: event] (Score: 0.78)
   The last deployment to staging took 45 minutes due to database
   migration overhead.
```

**Disable specific strategies to narrow results:**

```bash
# Skip temporal scoring — useful when time is irrelevant
memex memory search "What is our deployment strategy?" --no-temporal

# Skip mental models — get raw facts only
memex memory search "What is our deployment strategy?" --no-mental-model
```

### 3. Trace a Fact Back to Its Source

When Memory Search returns a useful fact, use lineage to find the original document:

```bash
memex memory lineage memory_unit <unit-uuid>
```

This shows the full provenance chain: Memory Unit -> Chunk -> Note -> Original File.

### 4. Discover Related Notes

When Note Search returns results, each result includes **related notes** — other documents that share specific entities with the result. This helps you discover connections between documents without running additional searches.

**Via MCP:** The `memex_note_search` tool automatically includes `related_notes` in each result. Each related note shows the shared entities and a strength score (0.0-1.0), where higher values mean more specific overlap.

**Via REST API:**

```bash
# Get related notes for specific note IDs
curl -X POST http://localhost:8000/api/v1/notes/related \
  -H "Content-Type: application/json" \
  -d '{"note_ids": ["<note-uuid>"]}'
```

**Via Python:**

```python
related = await api.get_related_notes([note_id])
for note_id, related_list in related.items():
    for r in related_list:
        print(f"  {r.title} (strength: {r.strength}, via: {r.shared_entities})")
```

Note relations complement search — search finds documents matching a query, while relations find documents connected to a known document through their shared entities.

## Quick Reference

| Feature | Note Search | Memory Search |
| :--- | :--- | :--- |
| **Granularity** | Chunks / Snippets / Nodes | Atomic facts / events |
| **Architecture** | Hybrid RRF (semantic + BM25 + graph + temporal) | TEMPR (Temporal, Entity, Mental Model, Keyword, Semantic) |
| **Output** | Document snippets with reasoning | Memory units with scores |
| **Scope** | Per-vault or per-document | Cross-vault knowledge graph |
| **Best for** | "Find the PDF where X is mentioned" | "What do we know about X globally?" |
| **Relations** | Includes related notes (shared entities) | Includes memory links (causal, temporal, semantic) |
| **CLI command** | `memex note search` | `memex memory search` |
| **MCP tool** | `memex_note_search` | `memex_memory_search` |

## Verification

To verify searches return results, run both against a known topic:

```bash
memex note search "your known topic" --vault your-vault
memex memory search "your known topic"
```

Both should return results if the topic has been ingested and processed.

## See Also

* [About the Hindsight Framework](../explanation/hindsight-framework.md) — how extraction, retrieval, and reflection work
* [About Retrieval Strategies](../explanation/retrieval-strategies.md) — TEMPR architecture in depth
* [MCP Tools Reference](../reference/mcp-tools.md) — `memex_memory_search` and `memex_note_search` parameters
