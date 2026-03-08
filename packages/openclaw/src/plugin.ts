/**
 * Memex Memory Plugin for OpenClaw
 *
 * Provides long-term memory via the Memex REST API with:
 *   - Auto-recall: search relevant memories before each agent turn
 *   - Auto-capture: store conversation turns as Markdown notes
 *   - Circuit breaker: 3 failures -> 60s cooldown to avoid blocking the agent
 *   - 25 agent tools for full Memex access
 *   - Slash commands: /recall and /remember
 *   - CLI: memex status, memex search
 */

import { Type } from '@sinclair/typebox';
import type { OpenClawPluginApi } from 'openclaw/plugin-sdk';

import { CircuitBreaker } from './circuit-breaker';
import { parseConfig } from './config';
import { extractTextContent, formatMemoryContext, formatMemoryUnit } from './formatting';
import {
  MemexClient,
  encodeBase64,
  formatConversationNote,
  formatSessionNote,
  hashTurnKey,
} from './memex-client';
import type { EntityDTO, MemoryStrategy, NoteStatus, NoteStrategy, SessionTurn } from './types';

// ---------------------------------------------------------------------------
// Note templates (embedded client-side — no API call needed)
// ---------------------------------------------------------------------------

const NOTE_TEMPLATES: Record<string, string> = {
  technical_brief: `# Technical Brief: [Title]

## Problem Statement
[Describe the problem being addressed]

## Proposed Solution
[High-level approach]

## Implementation Details
[Key technical details]

## Trade-offs
[Pros and cons of the approach]

## References
[Links and citations]`,

  general_note: `# [Title]

## Summary
[Brief overview]

## Details
[Main content]

## References
[Links and citations]`,

  architectural_decision_record: `# ADR: [Title]

## Status
[Proposed | Accepted | Deprecated | Superseded]

## Context
[What is the issue that we're seeing that is motivating this decision?]

## Decision
[What is the change that we're proposing and/or doing?]

## Consequences
[What becomes easier or more difficult to do because of this change?]`,

  request_for_comments: `# RFC: [Title]

## Summary
[One paragraph explanation of the proposal]

## Motivation
[Why are we doing this? What problem does it solve?]

## Detailed Design
[Explain the design in enough detail for review]

## Drawbacks
[Why should we NOT do this?]

## Alternatives
[What other approaches were considered?]

## Unresolved Questions
[What needs to be resolved before merging?]`,

  quick_note: `# [Title]

[Content]`,
};

const memexPlugin = {
  id: 'memory-memex',
  name: 'Memory (Memex)',
  description: 'Memex-backed long-term memory with auto-recall/capture via Memex REST API',
  kind: 'memory' as const,

  register(api: OpenClawPluginApi) {
    const cfg = parseConfig(api.pluginConfig);
    const client = new MemexClient(cfg, api.logger);
    const breaker = new CircuitBreaker({ failureThreshold: 3, resetTimeoutMs: 60_000 });
    let turnCounter = 0;
    const sessionId = crypto.randomUUID();
    const sessionBuffer: SessionTurn[] = [];

    api.logger.info(
      `memory-memex: registered (server: ${cfg.serverUrl}, recall: ${cfg.autoRecall}, capture: ${cfg.autoCapture})`,
    );

    // ========================================================================
    // Agent Tools
    // ========================================================================

    // ------ memex_memory_search ------

    api.registerTool(
      {
        name: 'memex_memory_search',
        label: 'Memex Search',
        description:
          'Search Memex memories (facts, events, observations). Best for broad/exploratory queries ("What do I know about X?"). For targeted document lookup, use memex_note_search. When unsure, run both in parallel.\n\nStrategy hints:\n- strategies: ["temporal"] → chronological ordering\n- strategies: ["graph"] → entity-centric traversal\n- strategies: ["mental_model"] → synthesized observations\n- Default (all strategies) is best for general queries\n\nCITE SOURCES: Use numbered citations [1], [2] inline. Add reference list with title + note ID. For memory units, include both memory ID and source note ID.',
        parameters: Type.Object({
          query: Type.String({ description: 'Search query' }),
          limit: Type.Optional(Type.Number({ description: 'Max results (default: 8)' })),
          token_budget: Type.Optional(
            Type.Number({ description: 'Token budget for results (default: server config)' }),
          ),
          strategies: Type.Optional(
            Type.Array(Type.String(), {
              description: 'Retrieval strategies: semantic, keyword, graph, temporal, mental_model',
            }),
          ),
          include_superseded: Type.Optional(
            Type.Boolean({ description: 'Include contradicted/low-confidence units (default: false)' }),
          ),
          after: Type.Optional(Type.String({ description: 'Temporal lower bound (ISO date)' })),
          before: Type.Optional(Type.String({ description: 'Temporal upper bound (ISO date)' })),
          tags: Type.Optional(
            Type.Array(Type.String(), { description: 'Filter by note tags' }),
          ),
        }),
        async execute(_toolCallId, params) {
          const { query, limit, token_budget, strategies, include_superseded, after, before, tags } =
            params as {
              query: string;
              limit?: number;
              token_budget?: number;
              strategies?: MemoryStrategy[];
              include_superseded?: boolean;
              after?: string;
              before?: string;
              tags?: string[];
            };

          const effectiveLimit = limit ?? cfg.searchLimit;
          try {
            const memories = (
              await client.searchMemories(query, undefined, {
                limit: effectiveLimit,
                token_budget,
                strategies,
                include_superseded,
                after,
                before,
                tags,
              })
            ).slice(0, effectiveLimit);

            if (memories.length === 0) {
              return {
                content: [{ type: 'text', text: 'No relevant memories found.' }],
                details: { count: 0 },
              };
            }

            const text = memories.map((m, i) => formatMemoryUnit(m, i + 1)).join('\n\n');

            return {
              content: [{ type: 'text', text: `Found ${memories.length} memories:\n\n${text}` }],
              details: { count: memories.length },
            };
          } catch (err) {
            return {
              content: [{ type: 'text', text: `Memex search failed: ${String(err)}` }],
              details: { error: String(err) },
            };
          }
        },
      },
      { name: 'memex_memory_search' },
    );

    // ------ memex_store ------

    api.registerTool(
      {
        name: 'memex_store',
        label: 'Memex Store',
        description:
          'Quick convenience wrapper to store a note in Memex. Use for capturing facts, decisions, or context. For full control over note creation (author, description, vault), use memex_add_note instead.',
        parameters: Type.Object({
          text: Type.String({ description: 'Content to store' }),
          name: Type.Optional(Type.String({ description: 'Note title' })),
          tags: Type.Optional(Type.Array(Type.String(), { description: 'Tags for the note' })),
        }),
        async execute(_toolCallId, params) {
          const { text, name, tags } = params as {
            text: string;
            name?: string;
            tags?: string[];
          };

          const now = new Date();
          const content = encodeBase64(text);
          const noteKey = hashTurnKey(text, now);

          try {
            client.ingestNote({
              name: name ?? `Note — ${now.toISOString()}`,
              note_key: noteKey,
              description: `Stored via OpenClaw on ${now.toISOString()}`,
              content,
              tags: tags ?? cfg.defaultTags,
              author: 'openclaw',
            });

            return {
              content: [{ type: 'text', text: `Stored: "${text.slice(0, 100)}..."` }],
              details: { action: 'created', noteKey },
            };
          } catch (err) {
            return {
              content: [{ type: 'text', text: `Memex store failed: ${String(err)}` }],
              details: { error: String(err) },
            };
          }
        },
      },
      { name: 'memex_store' },
    );

    // ------ memex_add_note ------

    api.registerTool(
      {
        name: 'memex_add_note',
        label: 'Memex Add Note',
        description:
          'Create a new note with full control over metadata. Returns overlap warnings when similar notes exist (similarity % + note IDs). Use memex_store for quick captures.',
        parameters: Type.Object({
          title: Type.String({ description: 'Note title' }),
          markdown_content: Type.String({ description: 'Note content in Markdown (5-15 lines recommended)' }),
          description: Type.String({ description: 'Summary of the note (max 250 words)' }),
          author: Type.String({ description: 'Author identifier (e.g. "openclaw")' }),
          tags: Type.Array(Type.String(), { description: 'Tags for the note' }),
          vault_id: Type.Optional(Type.String({ description: 'Target vault UUID or name' })),
          note_key: Type.Optional(Type.String({ description: 'Stable key for incremental updates' })),
          background: Type.Optional(Type.Boolean({ description: 'Queue for background ingestion (default: false)' })),
        }),
        async execute(_toolCallId, params) {
          const { title, markdown_content, description, author, tags, vault_id, note_key, background } =
            params as {
              title: string;
              markdown_content: string;
              description: string;
              author: string;
              tags: string[];
              vault_id?: string;
              note_key?: string;
              background?: boolean;
            };

          try {
            const result = await client.addNote(
              {
                name: title,
                description,
                content: encodeBase64(markdown_content),
                tags,
                author,
                vault_id: vault_id ?? null,
                note_key: note_key ?? null,
              },
              background ?? false,
            );

            const parts = [`Note created: ${result.document_id ?? 'queued'}`];
            if (result.overlapping_notes && result.overlapping_notes.length > 0) {
              parts.push('\n⚠ Similar notes found:');
              for (const o of result.overlapping_notes) {
                parts.push(`  - ${o.title ?? o.note_id} (${Math.round(o.similarity * 100)}% similar)`);
              }
            }

            return {
              content: [{ type: 'text', text: parts.join('\n') }],
              details: { document_id: result.document_id, overlaps: result.overlapping_notes?.length ?? 0 },
            };
          } catch (err) {
            return {
              content: [{ type: 'text', text: `Memex add note failed: ${String(err)}` }],
              details: { error: String(err) },
            };
          }
        },
      },
      { name: 'memex_add_note' },
    );

    // ------ memex_note_search ------

    api.registerTool(
      {
        name: 'memex_note_search',
        label: 'Memex Note Search',
        description:
          'Search source notes by hybrid retrieval. Returns ranked notes with inline metadata (title, description, tags) and snippets. Best for targeted document lookup ("Find the doc about X"). For broad exploration, use memex_memory_search. When unsure, run both in parallel.\n\nAfter memex_note_search: metadata is already inline — no extra memex_get_note_metadata calls needed.\nAfter memex_memory_search: still call memex_get_note_metadata per Note ID to filter.',
        parameters: Type.Object({
          query: Type.String({ description: 'Search query' }),
          limit: Type.Optional(Type.Number({ description: 'Max results (default: 5)' })),
          summarize: Type.Optional(
            Type.Boolean({ description: 'Synthesize an answer from results' }),
          ),
          expand_query: Type.Optional(
            Type.Boolean({ description: 'Enable multi-query expansion' }),
          ),
          strategies: Type.Optional(
            Type.Array(Type.String(), {
              description: 'Retrieval strategies: semantic, keyword, graph, temporal',
            }),
          ),
          after: Type.Optional(Type.String({ description: 'Temporal lower bound (ISO date)' })),
          before: Type.Optional(Type.String({ description: 'Temporal upper bound (ISO date)' })),
          tags: Type.Optional(
            Type.Array(Type.String(), { description: 'Filter by note tags' }),
          ),
          vault_ids: Type.Optional(
            Type.Array(Type.String(), { description: 'Scope to specific vaults' }),
          ),
        }),
        async execute(_toolCallId, params) {
          const { query, limit, summarize, expand_query, strategies, after, before, tags, vault_ids } =
            params as {
              query: string;
              limit?: number;
              summarize?: boolean;
              expand_query?: boolean;
              strategies?: NoteStrategy[];
              after?: string;
              before?: string;
              tags?: string[];
              vault_ids?: string[];
            };

          try {
            const results = await client.searchNotes(query, {
              limit,
              summarize,
              expand_query,
              strategies,
              after,
              before,
              tags,
              vault_ids,
            });

            if (results.length === 0) {
              return {
                content: [{ type: 'text', text: 'No matching notes found.' }],
                details: { count: 0 },
              };
            }

            const lines = results.map((r, i) => {
              const snippetText = r.snippets
                .map((s) => `  - [${s.node_title}] ${s.text.slice(0, 200)}`)
                .join('\n');
              const answer = r.answer ? `\n  Answer: ${r.answer}` : '';
              return `${i + 1}. Note ${r.note_id} (score: ${r.score ?? 'n/a'})${answer}\n${snippetText}`;
            });

            return {
              content: [
                { type: 'text', text: `Found ${results.length} notes:\n\n${lines.join('\n\n')}` },
              ],
              details: { count: results.length },
            };
          } catch (err) {
            return {
              content: [{ type: 'text', text: `Memex note search failed: ${String(err)}` }],
              details: { error: String(err) },
            };
          }
        },
      },
      { name: 'memex_note_search' },
    );

    // ------ memex_read_note ------

    api.registerTool(
      {
        name: 'memex_read_note',
        label: 'Memex Read Note',
        description: 'Retrieve full content of a note by UUID. Prefer memex_get_note_metadata → memex_get_page_index → memex_get_node for selective reading.',
        parameters: Type.Object({
          note_id: Type.String({ description: 'The UUID of the note' }),
        }),
        async execute(_toolCallId, params) {
          const { note_id } = params as { note_id: string };

          try {
            const note = await client.getNote(note_id);
            return {
              content: [{ type: 'text', text: JSON.stringify(note, null, 2) }],
              details: { note_id },
            };
          } catch (err) {
            return {
              content: [{ type: 'text', text: `Failed to read note: ${String(err)}` }],
              details: { error: String(err) },
            };
          }
        },
      },
      { name: 'memex_read_note' },
    );

    // ------ memex_get_note_metadata ------

    api.registerTool(
      {
        name: 'memex_get_note_metadata',
        label: 'Memex Note Metadata',
        description:
          'Cheap relevance check (~50 tokens): returns title, description, tags, publish date, source URI. Call on memory search results before memex_get_page_index to filter out irrelevant notes. After memex_note_search, metadata is already inline — no extra calls needed.',
        parameters: Type.Object({
          note_id: Type.String({ description: 'The UUID of the note' }),
        }),
        async execute(_toolCallId, params) {
          const { note_id } = params as { note_id: string };

          try {
            const result = await client.getNoteMetadata(note_id);
            return {
              content: [{ type: 'text', text: JSON.stringify(result.metadata, null, 2) }],
              details: { note_id },
            };
          } catch (err) {
            return {
              content: [{ type: 'text', text: `Failed to get note metadata: ${String(err)}` }],
              details: { error: String(err) },
            };
          }
        },
      },
      { name: 'memex_get_note_metadata' },
    );

    // ------ memex_get_page_index ------

    api.registerTool(
      {
        name: 'memex_get_page_index',
        label: 'Memex Page Index',
        description:
          'Get hierarchical page index (TOC) for a note. Only call after memex_get_note_metadata confirms relevance. Returns section titles, summaries, token estimates, and node IDs.\n\nProgressive reading: If total_tokens from metadata > 3000, use depth=0 first to get root sections, then use parent_node_id to drill into specific subtrees.',
        parameters: Type.Object({
          note_id: Type.String({ description: 'The UUID of the note' }),
          depth: Type.Optional(Type.Number({ description: 'Max tree depth (0=roots only, 1=roots+children, etc.)' })),
          parent_node_id: Type.Optional(Type.String({ description: 'Get subtree under this node' })),
        }),
        async execute(_toolCallId, params) {
          const { note_id, depth, parent_node_id } = params as {
            note_id: string;
            depth?: number;
            parent_node_id?: string;
          };

          try {
            const index = await client.getPageIndex(note_id, { depth, parent_node_id });

            const formatNode = (
              node: { id: string; title: string; summary?: string | null; token_estimate?: number | null; level: number; children: unknown[] },
              indent: number,
            ): string => {
              const prefix = '  '.repeat(indent);
              const summary = node.summary ? ` — ${node.summary}` : '';
              const tokens = node.token_estimate != null ? ` (~${node.token_estimate} tokens)` : '';
              const line = `${prefix}- [${node.id}] ${node.title}${summary}${tokens}`;
              const children = (node.children as typeof node[]).map((c) =>
                formatNode(c, indent + 1),
              );
              return [line, ...children].join('\n');
            };

            const toc = index.toc.map((n) => formatNode(n, 0)).join('\n');
            const totalTokens = index.total_tokens != null
              ? `\nTotal tokens: ${index.total_tokens}`
              : '';

            return {
              content: [{ type: 'text', text: `Table of Contents:${totalTokens}\n\n${toc}` }],
              details: { note_id, nodeCount: index.toc.length },
            };
          } catch (err) {
            return {
              content: [{ type: 'text', text: `Failed to get page index: ${String(err)}` }],
              details: { error: String(err) },
            };
          }
        },
      },
      { name: 'memex_get_page_index' },
    );

    // ------ memex_get_node ------

    api.registerTool(
      {
        name: 'memex_get_node',
        label: 'Memex Get Node',
        description: 'Read a specific note section by node ID. Call multiple in parallel when reading several sections.',
        parameters: Type.Object({
          node_id: Type.String({ description: 'The UUID of the node' }),
        }),
        async execute(_toolCallId, params) {
          const { node_id } = params as { node_id: string };

          try {
            const node = await client.getNode(node_id);
            return {
              content: [{ type: 'text', text: node.text }],
              details: { node_id, title: node.title },
            };
          } catch (err) {
            return {
              content: [{ type: 'text', text: `Failed to get node: ${String(err)}` }],
              details: { error: String(err) },
            };
          }
        },
      },
      { name: 'memex_get_node' },
    );

    // ------ memex_get_lineage ------

    api.registerTool(
      {
        name: 'memex_get_lineage',
        label: 'Memex Lineage',
        description:
          'Retrieve the provenance chain (lineage) of a memory unit, observation, note, or mental model.',
        parameters: Type.Object({
          unit_id: Type.String({ description: 'The UUID of the entity' }),
          entity_type: Type.Optional(
            Type.String({ description: 'Entity type (default: memory_unit)' }),
          ),
        }),
        async execute(_toolCallId, params) {
          const { unit_id, entity_type } = params as {
            unit_id: string;
            entity_type?: string;
          };

          try {
            const lineage = await client.getLineage(unit_id, entity_type ?? 'memory_unit');
            return {
              content: [{ type: 'text', text: JSON.stringify(lineage, null, 2) }],
              details: { unit_id, entity_type: entity_type ?? 'memory_unit' },
            };
          } catch (err) {
            return {
              content: [{ type: 'text', text: `Failed to get lineage: ${String(err)}` }],
              details: { error: String(err) },
            };
          }
        },
      },
      { name: 'memex_get_lineage' },
    );

    // ------ memex_list_entities ------

    api.registerTool(
      {
        name: 'memex_list_entities',
        label: 'Memex List Entities',
        description:
          'List or search entities in the knowledge graph. Without a query, returns top entities by relevance.\n\nEntity exploration workflow:\n1. memex_list_entities → browse/search entities by name\n2. memex_get_entity → get details (type, mention count)\n3. memex_get_entity_mentions → find facts/observations mentioning entity\n4. memex_get_entity_cooccurrences → find related entities',
        parameters: Type.Object({
          query: Type.Optional(Type.String({ description: 'Search term to filter by name' })),
          limit: Type.Optional(Type.Number({ description: 'Max entities to return (default: 20)' })),
          vault_id: Type.Optional(Type.String({ description: 'Scope to specific vault' })),
        }),
        async execute(_toolCallId, params) {
          const { query, limit, vault_id } = params as {
            query?: string;
            limit?: number;
            vault_id?: string;
          };

          try {
            const entities = await client.listEntities(query, limit ?? 20, undefined, vault_id);

            if (entities.length === 0) {
              return {
                content: [{ type: 'text', text: 'No entities found.' }],
                details: { count: 0 },
              };
            }

            const lines = entities.map(
              (e, i) =>
                `${i + 1}. ${e.name} (${e.entity_type ?? 'unknown'}) [${e.id}] — ${e.mention_count ?? 0} mentions`,
            );

            return {
              content: [
                { type: 'text', text: `Found ${entities.length} entities:\n\n${lines.join('\n')}` },
              ],
              details: { count: entities.length },
            };
          } catch (err) {
            return {
              content: [{ type: 'text', text: `Failed to list entities: ${String(err)}` }],
              details: { error: String(err) },
            };
          }
        },
      },
      { name: 'memex_list_entities' },
    );

    // ------ memex_get_entity ------

    api.registerTool(
      {
        name: 'memex_get_entity',
        label: 'Memex Get Entity',
        description:
          'Get details for a specific entity and its recent mentions in the knowledge graph.',
        parameters: Type.Object({
          entity_id: Type.String({ description: 'The UUID of the entity' }),
        }),
        async execute(_toolCallId, params) {
          const { entity_id } = params as { entity_id: string };

          try {
            const [entity, mentions] = await Promise.all([
              client.getEntity(entity_id),
              client.getEntityMentions(entity_id),
            ]);

            const mentionLines = mentions.map(
              (m, i) => `  ${i + 1}. [${m.fact_type}] ${m.text}`,
            );

            const text = [
              `Entity: ${entity.name} (${entity.entity_type ?? 'unknown'})`,
              `ID: ${entity.id}`,
              `Mentions: ${entity.mention_count ?? 0}`,
              '',
              mentionLines.length > 0
                ? `Recent mentions:\n${mentionLines.join('\n')}`
                : 'No recent mentions.',
            ].join('\n');

            return {
              content: [{ type: 'text', text }],
              details: { entity_id, mentionCount: mentions.length },
            };
          } catch (err) {
            return {
              content: [{ type: 'text', text: `Failed to get entity: ${String(err)}` }],
              details: { error: String(err) },
            };
          }
        },
      },
      { name: 'memex_get_entity' },
    );

    // ------ memex_get_entity_cooccurrences ------

    api.registerTool(
      {
        name: 'memex_get_entity_cooccurrences',
        label: 'Memex Entity Cooccurrences',
        description:
          'Find entities that frequently co-occur with a given entity. Useful for discovering related people, concepts, or topics.',
        parameters: Type.Object({
          entity_id: Type.String({ description: 'The UUID of the entity' }),
        }),
        async execute(_toolCallId, params) {
          const { entity_id } = params as { entity_id: string };

          try {
            const cooccurrences = await client.getEntityCooccurrences(entity_id);

            if (cooccurrences.length === 0) {
              return {
                content: [{ type: 'text', text: 'No co-occurring entities found.' }],
                details: { count: 0 },
              };
            }

            const lines = cooccurrences.map(
              (c, i) => `${i + 1}. ${c.name} [${c.entity_id}] — ${c.cooccurrence_count} co-occurrences`,
            );

            return {
              content: [
                { type: 'text', text: `Found ${cooccurrences.length} co-occurring entities:\n\n${lines.join('\n')}` },
              ],
              details: { count: cooccurrences.length },
            };
          } catch (err) {
            return {
              content: [{ type: 'text', text: `Failed to get cooccurrences: ${String(err)}` }],
              details: { error: String(err) },
            };
          }
        },
      },
      { name: 'memex_get_entity_cooccurrences' },
    );

    // ------ memex_get_memory_unit ------

    api.registerTool(
      {
        name: 'memex_get_memory_unit',
        label: 'Memex Get Memory Unit',
        description: 'Retrieve a single memory unit by UUID. Returns full details including confidence, status, and supersession links.',
        parameters: Type.Object({
          unit_id: Type.String({ description: 'The UUID of the memory unit' }),
        }),
        async execute(_toolCallId, params) {
          const { unit_id } = params as { unit_id: string };

          try {
            const unit = await client.getMemoryUnit(unit_id);
            return {
              content: [{ type: 'text', text: formatMemoryUnit(unit, 1) }],
              details: { unit_id },
            };
          } catch (err) {
            return {
              content: [{ type: 'text', text: `Failed to get memory unit: ${String(err)}` }],
              details: { error: String(err) },
            };
          }
        },
      },
      { name: 'memex_get_memory_unit' },
    );

    // ------ memex_get_memory_units ------

    api.registerTool(
      {
        name: 'memex_get_memory_units',
        label: 'Memex Get Memory Units (Batch)',
        description: 'Batch lookup of memory units with contradiction/supersession context. Returns full details for multiple units at once.',
        parameters: Type.Object({
          unit_ids: Type.Array(Type.String(), { description: 'List of memory unit UUIDs' }),
        }),
        async execute(_toolCallId, params) {
          const { unit_ids } = params as { unit_ids: string[] };

          try {
            const units = await client.getMemoryUnits(unit_ids);

            if (units.length === 0) {
              return {
                content: [{ type: 'text', text: 'No memory units found.' }],
                details: { count: 0 },
              };
            }

            const text = units.map((u, i) => formatMemoryUnit(u, i + 1)).join('\n\n');

            return {
              content: [{ type: 'text', text: `Found ${units.length} memory units:\n\n${text}` }],
              details: { count: units.length },
            };
          } catch (err) {
            return {
              content: [{ type: 'text', text: `Failed to get memory units: ${String(err)}` }],
              details: { error: String(err) },
            };
          }
        },
      },
      { name: 'memex_get_memory_units' },
    );

    // ------ memex_set_note_status ------

    api.registerTool(
      {
        name: 'memex_set_note_status',
        label: 'Memex Set Note Status',
        description:
          'Update note lifecycle status. Use to mark notes as superseded (replaced by another note) or appended (extended by another note). Link to the replacing/extending note via linked_note_id.',
        parameters: Type.Object({
          note_id: Type.String({ description: 'The UUID of the note' }),
          status: Type.String({ description: 'New status: active, superseded, or appended' }),
          linked_note_id: Type.Optional(Type.String({ description: 'UUID of replacing/extending note' })),
        }),
        async execute(_toolCallId, params) {
          const { note_id, status, linked_note_id } = params as {
            note_id: string;
            status: NoteStatus;
            linked_note_id?: string;
          };

          try {
            await client.setNoteStatus(note_id, status, linked_note_id);
            return {
              content: [{ type: 'text', text: `Note ${note_id} status set to "${status}".` }],
              details: { note_id, status },
            };
          } catch (err) {
            return {
              content: [{ type: 'text', text: `Failed to set note status: ${String(err)}` }],
              details: { error: String(err) },
            };
          }
        },
      },
      { name: 'memex_set_note_status' },
    );

    // ------ memex_rename_note ------

    api.registerTool(
      {
        name: 'memex_rename_note',
        label: 'Memex Rename Note',
        description: 'Rename a note by UUID.',
        parameters: Type.Object({
          note_id: Type.String({ description: 'The UUID of the note' }),
          new_title: Type.String({ description: 'The new title for the note' }),
        }),
        async execute(_toolCallId, params) {
          const { note_id, new_title } = params as { note_id: string; new_title: string };

          try {
            await client.renameNote(note_id, new_title);
            return {
              content: [{ type: 'text', text: `Note ${note_id} renamed to "${new_title}".` }],
              details: { note_id, new_title },
            };
          } catch (err) {
            return {
              content: [{ type: 'text', text: `Failed to rename note: ${String(err)}` }],
              details: { error: String(err) },
            };
          }
        },
      },
      { name: 'memex_rename_note' },
    );

    // ------ memex_reflect ------

    api.registerTool(
      {
        name: 'memex_reflect',
        label: 'Memex Reflect',
        description:
          'Trigger reflection on an entity. Synthesizes observations into mental models from recent memories about the entity.',
        parameters: Type.Object({
          entity_id: Type.String({ description: 'The UUID of the entity to reflect on' }),
          limit: Type.Optional(Type.Number({ description: 'Recent memories to consider (default: 20)' })),
          vault_id: Type.Optional(Type.String({ description: 'Scope to specific vault' })),
        }),
        async execute(_toolCallId, params) {
          const { entity_id, limit, vault_id } = params as {
            entity_id: string;
            limit?: number;
            vault_id?: string;
          };

          try {
            const result = await client.reflect(entity_id, limit, vault_id);
            return {
              content: [{ type: 'text', text: `Reflection ${result.status} for entity ${entity_id}.` }],
              details: { entity_id, status: result.status },
            };
          } catch (err) {
            return {
              content: [{ type: 'text', text: `Failed to reflect: ${String(err)}` }],
              details: { error: String(err) },
            };
          }
        },
      },
      { name: 'memex_reflect' },
    );

    // ------ memex_get_template ------

    api.registerTool(
      {
        name: 'memex_get_template',
        label: 'Memex Get Template',
        description:
          'Get a markdown template for structured note creation. Available types: technical_brief, general_note, architectural_decision_record, request_for_comments, quick_note.',
        parameters: Type.Object({
          type: Type.String({ description: 'Template type' }),
        }),
        async execute(_toolCallId, params) {
          const { type } = params as { type: string };

          const template = NOTE_TEMPLATES[type];
          if (!template) {
            return {
              content: [{ type: 'text', text: `Unknown template type: "${type}". Available: ${Object.keys(NOTE_TEMPLATES).join(', ')}` }],
              details: { error: 'unknown_template' },
            };
          }

          return {
            content: [{ type: 'text', text: template }],
            details: { type },
          };
        },
      },
      { name: 'memex_get_template' },
    );

    // ------ memex_active_vault ------

    api.registerTool(
      {
        name: 'memex_active_vault',
        label: 'Memex Active Vault',
        description: 'Get the currently active vault name and ID.',
        parameters: Type.Object({}),
        async execute() {
          try {
            const vault = await client.getActiveVault();
            return {
              content: [{ type: 'text', text: `Active vault: ${vault.name} (${vault.id})` }],
              details: { vault_id: vault.id, vault_name: vault.name },
            };
          } catch (err) {
            return {
              content: [{ type: 'text', text: `Failed to get active vault: ${String(err)}` }],
              details: { error: String(err) },
            };
          }
        },
      },
      { name: 'memex_active_vault' },
    );

    // ------ memex_list_vaults ------

    api.registerTool(
      {
        name: 'memex_list_vaults',
        label: 'Memex List Vaults',
        description: 'List all available vaults.',
        parameters: Type.Object({}),
        async execute() {
          try {
            const vaults = await client.listVaults();

            if (vaults.length === 0) {
              return {
                content: [{ type: 'text', text: 'No vaults found.' }],
                details: { count: 0 },
              };
            }

            const lines = vaults.map(
              (v, i) => `${i + 1}. ${v.name} [${v.id}]${v.description ? ` — ${v.description}` : ''}`,
            );

            return {
              content: [{ type: 'text', text: `Found ${vaults.length} vaults:\n\n${lines.join('\n')}` }],
              details: { count: vaults.length },
            };
          } catch (err) {
            return {
              content: [{ type: 'text', text: `Failed to list vaults: ${String(err)}` }],
              details: { error: String(err) },
            };
          }
        },
      },
      { name: 'memex_list_vaults' },
    );

    // ------ memex_list_notes ------

    api.registerTool(
      {
        name: 'memex_list_notes',
        label: 'Memex List Notes',
        description: 'List notes with pagination. Not recommended for discovery — use memex_note_search instead.',
        parameters: Type.Object({
          limit: Type.Optional(Type.Number({ description: 'Max notes to return (default: 20)' })),
          offset: Type.Optional(Type.Number({ description: 'Pagination offset (default: 0)' })),
          vault_id: Type.Optional(Type.String({ description: 'Scope to specific vault' })),
        }),
        async execute(_toolCallId, params) {
          const { limit, offset, vault_id } = params as {
            limit?: number;
            offset?: number;
            vault_id?: string;
          };

          try {
            const notes = await client.listNotes(limit ?? 20, offset ?? 0, vault_id);

            if (notes.length === 0) {
              return {
                content: [{ type: 'text', text: 'No notes found.' }],
                details: { count: 0 },
              };
            }

            const lines = notes.map(
              (n, i) => `${i + 1}. ${n.title} [${n.id}]${n.created_at ? ` (${n.created_at})` : ''}`,
            );

            return {
              content: [{ type: 'text', text: `Found ${notes.length} notes:\n\n${lines.join('\n')}` }],
              details: { count: notes.length },
            };
          } catch (err) {
            return {
              content: [{ type: 'text', text: `Failed to list notes: ${String(err)}` }],
              details: { error: String(err) },
            };
          }
        },
      },
      { name: 'memex_list_notes' },
    );

    // ------ memex_migrate_note ------

    api.registerTool(
      {
        name: 'memex_migrate_note',
        label: 'Memex Migrate Note',
        description: 'Move a note to a different vault.',
        parameters: Type.Object({
          note_id: Type.String({ description: 'The UUID of the note to migrate' }),
          target_vault_id: Type.String({ description: 'Target vault UUID or name' }),
        }),
        async execute(_toolCallId, params) {
          const { note_id, target_vault_id } = params as {
            note_id: string;
            target_vault_id: string;
          };

          try {
            await client.migrateNote(note_id, target_vault_id);
            return {
              content: [{ type: 'text', text: `Note ${note_id} migrated to vault ${target_vault_id}.` }],
              details: { note_id, target_vault_id },
            };
          } catch (err) {
            return {
              content: [{ type: 'text', text: `Failed to migrate note: ${String(err)}` }],
              details: { error: String(err) },
            };
          }
        },
      },
      { name: 'memex_migrate_note' },
    );

    // ------ memex_ingest_url ------

    api.registerTool(
      {
        name: 'memex_ingest_url',
        label: 'Memex Ingest URL',
        description: 'Ingest content from a URL into Memex.',
        parameters: Type.Object({
          url: Type.String({ description: 'URL to ingest' }),
          vault_id: Type.Optional(Type.String({ description: 'Target vault UUID or name' })),
          background: Type.Optional(Type.Boolean({ description: 'Queue for background processing (default: true)' })),
        }),
        async execute(_toolCallId, params) {
          const { url, vault_id, background } = params as {
            url: string;
            vault_id?: string;
            background?: boolean;
          };

          try {
            const result = await client.ingestUrl(url, vault_id, background ?? true);
            return {
              content: [{ type: 'text', text: `URL ingestion started: ${JSON.stringify(result)}` }],
              details: { url },
            };
          } catch (err) {
            return {
              content: [{ type: 'text', text: `Failed to ingest URL: ${String(err)}` }],
              details: { error: String(err) },
            };
          }
        },
      },
      { name: 'memex_ingest_url' },
    );

    // ========================================================================
    // Slash commands
    // ========================================================================

    api.registerCommand({
      name: 'recall',
      description: 'Search Memex memories',
      acceptsArgs: true,
      requireAuth: false,
      handler: async (ctx) => {
        const query = ctx.args?.trim();
        if (!query) return { text: 'Usage: /recall <query>' };

        try {
          const memories = await client.searchMemories(query);
          if (memories.length === 0) return { text: 'No relevant memories found.' };

          const lines = memories.map(
            (m, i) => `${i + 1}. [${m.fact_type}] ${m.text}`,
          );
          return { text: `Found ${memories.length} memories:\n\n${lines.join('\n')}` };
        } catch (err) {
          return { text: `Memex search failed: ${String(err)}` };
        }
      },
    });

    api.registerCommand({
      name: 'remember',
      description: 'Store a note in Memex',
      acceptsArgs: true,
      requireAuth: true,
      handler: async (ctx) => {
        const text = ctx.args?.trim();
        if (!text) return { text: 'Usage: /remember <text to store>' };

        const now = new Date();
        const content = encodeBase64(text);
        const noteKey = hashTurnKey(text, now);

        client.ingestNote({
          name: `Remember — ${now.toISOString()}`,
          note_key: noteKey,
          description: text.slice(0, 200),
          content,
          tags: cfg.defaultTags,
          author: 'openclaw',
        });

        return {
          text: `Stored in Memex: "${text.slice(0, 100)}${text.length > 100 ? '...' : ''}"`,
        };
      },
    });

    // ========================================================================
    // CLI commands
    // ========================================================================

    api.registerCli(
      ({ program }) => {
        const memex = program.command('memex').description('Memex memory plugin commands');

        memex
          .command('status')
          .description('Check Memex server connectivity')
          .action(async () => {
            try {
              const res = await fetch(`${cfg.serverUrl}/docs`);
              console.log(
                res.ok
                  ? `Memex server OK at ${cfg.serverUrl}`
                  : `Memex returned ${res.status}`,
              );
            } catch (err) {
              console.error(`Cannot reach Memex at ${cfg.serverUrl}: ${String(err)}`);
            }
          });

        memex
          .command('search')
          .description('Search Memex memories')
          .argument('<query>', 'Search query')
          .option('--limit <n>', 'Max results', '8')
          .action(async (query: string, opts: { limit: string }) => {
            const limit = parseInt(opts.limit, 10) || 8;
            try {
              const memories = (
                await client.searchMemories(query, undefined, { limit })
              ).slice(0, limit);
              for (const m of memories) {
                console.log(`[${m.fact_type}] ${m.text}`);
              }
              if (memories.length === 0) console.log('No memories found.');
            } catch (err) {
              console.error(`Search failed: ${String(err)}`);
            }
          });
      },
      { commands: ['memex'] },
    );

    // ========================================================================
    // Lifecycle hooks
    // ========================================================================

    if (cfg.autoRecall) {
      api.on('before_agent_start', async (event) => {
        if (!event.prompt || event.prompt.length < 5) return;
        turnCounter++;
        if (breaker.isOpen()) {
          api.logger.debug?.('memory-memex: circuit breaker open — skipping recall');
          return;
        }

        try {
          const signal = AbortSignal.timeout(cfg.timeoutMs);
          const memories = await client.searchMemories(event.prompt, signal);

          if (memories.length === 0) {
            breaker.recordSuccess();
            return;
          }

          // On every Nth turn, fetch entity profile (best-effort)
          let entities: EntityDTO[] = [];
          if (turnCounter % cfg.profileFrequency === 0) {
            try {
              entities = await client.listEntities(undefined, 15, signal);
            } catch (entityErr) {
              api.logger.warn?.(
                `memory-memex: entity fetch failed (best-effort): ${String(entityErr)}`,
              );
            }
          }

          api.logger.info?.(
            `memory-memex: injecting ${memories.length} memories into context`,
          );
          breaker.recordSuccess();

          return {
            prependContext: formatMemoryContext(memories, entities),
          };
        } catch (err) {
          breaker.recordFailure();
          api.logger.warn?.(`memory-memex: recall failed: ${String(err)}`);
        }
      });
    }

    if (cfg.autoCapture) {
      api.on('agent_end', async (event) => {
        if (!event.success || !event.messages || event.messages.length === 0) return;
        if (breaker.isOpen()) {
          api.logger.debug?.('memory-memex: circuit breaker open — skipping capture');
          return;
        }

        try {
          let userText: string | null = null;
          let assistantText: string | null = null;

          for (const msg of [...event.messages].reverse()) {
            if (!msg || typeof msg !== 'object') continue;
            const m = msg as Record<string, unknown>;

            if (m.role === 'user' && !userText) {
              userText = extractTextContent(m.content);
              if (cfg.captureMode === 'filtered') break;
            }
            if (m.role === 'assistant' && !assistantText) {
              assistantText = extractTextContent(m.content);
            }

            if (userText && assistantText) break;
          }

          // Strip injected context blocks to avoid re-ingesting
          // memories and entity profiles that are already stored in Memex.
          const stripInjected = (text: string) =>
            text
              .replace(/<relevant-memories>[\s\S]*?<\/relevant-memories>\s*/g, '')
              .replace(/<knowledge-profile>[\s\S]*?<\/knowledge-profile>\s*/g, '')
              .trim();

          if (userText) {
            userText = stripInjected(userText);
          }
          if (assistantText) {
            assistantText = stripInjected(assistantText);
          }

          if (!userText || userText.length < cfg.minCaptureLength) return;

          const now = new Date();

          if (cfg.sessionGrouping) {
            sessionBuffer.push({
              userMessage: userText,
              assistantMessage: cfg.captureMode === 'full' ? (assistantText ?? '') : '',
              timestamp: now,
            });

            const markdown = formatSessionNote(sessionBuffer, now, cfg.defaultTags);
            const content = encodeBase64(markdown);
            const noteKey = `session_${sessionId}`;

            client.ingestNote({
              name: `Session — ${now.toISOString()}`,
              note_key: noteKey,
              description: `Agent session captured on ${now.toISOString()}`,
              content,
              tags: cfg.defaultTags,
              author: 'openclaw',
            });

            api.logger.info?.(
              `memory-memex: session turn ${sessionBuffer.length} captured (${sessionId.slice(0, 8)})`,
            );
          } else {
            const aiResponse = cfg.captureMode === 'full' ? (assistantText ?? '') : '';
            const markdown = formatConversationNote(
              userText,
              aiResponse,
              now,
              cfg.defaultTags,
            );
            const content = encodeBase64(markdown);
            const noteKey = hashTurnKey(userText, now);

            client.ingestNote({
              name: `Conversation — ${now.toISOString()}`,
              note_key: noteKey,
              description: `Agent conversation captured on ${now.toISOString()}`,
              content,
              tags: cfg.defaultTags,
              author: 'openclaw',
            });

            api.logger.info?.('memory-memex: user message captured');
          }
        } catch (err) {
          api.logger.warn?.(`memory-memex: capture failed: ${String(err)}`);
        }
      });
    }

    // ========================================================================
    // Service
    // ========================================================================

    api.registerService({
      id: 'memory-memex',
      start: () => {
        api.logger.info(`memory-memex: service started (server: ${cfg.serverUrl})`);
      },
      stop: () => {
        api.logger.info('memory-memex: service stopped');
      },
    });
  },
};

export default memexPlugin;
