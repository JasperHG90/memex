---
name: memex
description: Long-term memory for OpenClaw agents via Memex. Store notes, search memories, explore knowledge graphs, and build persistent context across sessions.
---

# Memex Memory

## Retrieval — Route by Query Type

IF relationships/connections/landscape:
  → Entity exploration: memex_list_entities → memex_get_entity_cooccurrences → memex_get_entity_mentions

IF specific content/document lookup:
  → Search: memex_memory_search (broad) and/or memex_note_search (targeted) — run in parallel
  → Filter: after memory_search, call memex_get_notes_metadata. After note_search, metadata is inline — skip.
  → Read: memex_get_page_index → memex_get_nodes (batch). memex_read_note only when total_tokens < 500.
  → Assets: IF has_assets: true → memex_list_assets → memex_get_resources

IF broad: run entity exploration AND search in parallel.

## Tools

### Memory Search
| Tool | Purpose | When |
|------|---------|------|
| memex_memory_search | Search facts/events/observations | Broad queries, "what do I know about X?" |
| memex_note_search | Search source documents | Targeted lookup, "find the doc about X" |

### Note Reading (Progressive)
| Tool | Purpose | When |
|------|---------|------|
| memex_get_notes_metadata | Cheap relevance check (~50 tokens) | After memory_search, before page_index |
| memex_get_page_index | Section TOC with token estimates | After metadata confirms relevance |
| memex_get_nodes | Read specific sections by node ID | After page_index identifies relevant sections |
| memex_read_note | Full note content | Only when total_tokens < 500 |

### Entity Exploration
| Tool | Purpose | When |
|------|---------|------|
| memex_list_entities | Browse/search entities by name | Starting entity exploration |
| memex_get_entity | Get entity details + recent mentions | After identifying entity of interest |
| memex_get_entity_cooccurrences | Find related entities | Discovering connections |

### Note Management
| Tool | Purpose | When |
|------|---------|------|
| memex_add_note | Create a note with full metadata | Capturing decisions, findings, context |
| memex_store | Quick note capture | Fast saves without metadata control |
| memex_set_note_status | Update lifecycle status | Marking notes superseded/appended |
| memex_rename_note | Rename a note | Correcting titles |
| memex_delete_note | Delete a note | Removing incorrect/duplicate notes |
| memex_update_note_date | Update note date with cascade | Correcting timestamps |

### Other
| Tool | Purpose | When |
|------|---------|------|
| memex_get_lineage | Trace provenance chain | Understanding where a fact came from |
| memex_reflect | Trigger entity reflection | Synthesizing observations into mental models |
| memex_delete_memory | Delete a memory unit | Removing incorrect facts |

## Strategy Hints
- strategies: ["temporal"] → chronological ordering
- strategies: ["graph"] → entity-centric traversal
- strategies: ["mental_model"] → synthesized observations
- Default (all strategies) is best for general queries

## Capture — When to Save

Call memex_add_note (background: true, author: "openclaw") when:
1. Completed a multi-step task
2. Diagnosed a bug root cause
3. Made/discovered an architectural decision
4. Learned a user preference or workflow pattern
5. Resolved a tricky configuration issue

Keep notes concise (max 300 tokens).

## Citations — MANDATORY

Every response using Memex data MUST include:
1. Inline numbered references [1], [2] on every claim
2. Reference list at end: [note] title + note ID | [memory] title + memory ID + source note ID

## Rules
- Only use IDs from tool output. Never fabricate IDs.
- Filter before reading. Never call memex_get_page_index on unconfirmed notes.
- Do not call memex_get_notes_metadata after memex_note_search (metadata already inline).
