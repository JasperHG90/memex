/**
 * Memex Memory Plugin for OpenClaw
 *
 * Provides long-term memory via the Memex REST API with:
 *   - Auto-recall: search relevant memories before each agent turn
 *   - Auto-capture: store conversation turns as Markdown notes
 *   - Circuit breaker: 3 failures -> 60s cooldown to avoid blocking the agent
 *   - 9 agent tools for full Memex access
 *   - Slash commands: /recall and /remember
 *   - CLI: memex status, memex search
 */

import { Type } from '@sinclair/typebox';
import type { OpenClawPluginApi } from 'openclaw/plugin-sdk';

import { CircuitBreaker } from './circuit-breaker';
import { parseConfig } from './config';
import { extractTextContent, formatMemoryContext } from './formatting';
import {
  MemexClient,
  encodeBase64,
  formatConversationNote,
  hashTurnKey,
} from './memex-client';

const memexPlugin = {
  id: 'memory-memex',
  name: 'Memory (Memex)',
  description: 'Memex-backed long-term memory with auto-recall/capture via Memex REST API',
  kind: 'memory' as const,

  register(api: OpenClawPluginApi) {
    const cfg = parseConfig(api.pluginConfig);
    const client = new MemexClient(cfg, api.logger);
    const breaker = new CircuitBreaker({ failureThreshold: 3, resetTimeoutMs: 60_000 });

    api.logger.info(
      `memory-memex: registered (server: ${cfg.serverUrl}, recall: ${cfg.autoRecall}, capture: ${cfg.autoCapture})`,
    );

    // ========================================================================
    // Agent Tools
    // ========================================================================

    api.registerTool(
      {
        name: 'memex_search',
        label: 'Memex Search',
        description:
          'Search through Memex long-term memories. Use when you need context about past conversations, facts, or relevant knowledge.',
        parameters: Type.Object({
          query: Type.String({ description: 'Search query' }),
          limit: Type.Optional(Type.Number({ description: 'Max results (default: 8)' })),
        }),
        async execute(_toolCallId, params) {
          const { query, limit } = params as { query: string; limit?: number };
          const origLimit = cfg.searchLimit;
          if (limit != null) cfg.searchLimit = limit;

          const effectiveLimit = limit ?? cfg.searchLimit;
          try {
            const memories = (await client.searchMemories(query)).slice(0, effectiveLimit);
            cfg.searchLimit = origLimit;

            if (memories.length === 0) {
              return {
                content: [{ type: 'text', text: 'No relevant memories found.' }],
                details: { count: 0 },
              };
            }

            const text = memories
              .map((m, i) => `${i + 1}. [${m.fact_type}] ${m.text}`)
              .join('\n');

            return {
              content: [{ type: 'text', text: `Found ${memories.length} memories:\n\n${text}` }],
              details: { count: memories.length },
            };
          } catch (err) {
            cfg.searchLimit = origLimit;
            return {
              content: [{ type: 'text', text: `Memex search failed: ${String(err)}` }],
              details: { error: String(err) },
            };
          }
        },
      },
      { name: 'memex_search' },
    );

    api.registerTool(
      {
        name: 'memex_store',
        label: 'Memex Store',
        description:
          'Store a note in Memex for long-term memory. Use for important facts, decisions, or context.',
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

    api.registerTool(
      {
        name: 'memex_note_search',
        label: 'Memex Note Search',
        description:
          'Search source notes with optional synthesis. Returns ranked notes with snippets.',
        parameters: Type.Object({
          query: Type.String({ description: 'Search query' }),
          limit: Type.Optional(Type.Number({ description: 'Max results (default: 5)' })),
          summarize: Type.Optional(
            Type.Boolean({ description: 'Synthesize an answer from results' }),
          ),
          reason: Type.Optional(
            Type.Boolean({ description: 'Annotate relevant sections with reasoning' }),
          ),
          expand_query: Type.Optional(
            Type.Boolean({ description: 'Enable multi-query expansion' }),
          ),
        }),
        async execute(_toolCallId, params) {
          const { query, limit, summarize, reason, expand_query } = params as {
            query: string;
            limit?: number;
            summarize?: boolean;
            reason?: boolean;
            expand_query?: boolean;
          };

          try {
            const results = await client.searchNotes(query, {
              limit,
              summarize,
              reason,
              expand_query,
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

    api.registerTool(
      {
        name: 'memex_read_note',
        label: 'Memex Read Note',
        description: 'Retrieve the full content and metadata of a note by its UUID.',
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

    api.registerTool(
      {
        name: 'memex_get_page_index',
        label: 'Memex Page Index',
        description:
          'Get the hierarchical page index (table of contents) for a note. Returns section titles, summaries, and node IDs.',
        parameters: Type.Object({
          note_id: Type.String({ description: 'The UUID of the note' }),
        }),
        async execute(_toolCallId, params) {
          const { note_id } = params as { note_id: string };

          try {
            const index = await client.getPageIndex(note_id);

            const formatNode = (
              node: { id: string; title: string; summary?: string | null; level: number; children: unknown[] },
              indent: number,
            ): string => {
              const prefix = '  '.repeat(indent);
              const summary = node.summary ? ` — ${node.summary}` : '';
              const line = `${prefix}- [${node.id}] ${node.title}${summary}`;
              const children = (node.children as typeof node[]).map((c) =>
                formatNode(c, indent + 1),
              );
              return [line, ...children].join('\n');
            };

            const toc = index.toc.map((n) => formatNode(n, 0)).join('\n');

            return {
              content: [{ type: 'text', text: `Table of Contents:\n\n${toc}` }],
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

    api.registerTool(
      {
        name: 'memex_get_node',
        label: 'Memex Get Node',
        description: 'Retrieve the full text content of a specific note section by its node ID.',
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

    api.registerTool(
      {
        name: 'memex_list_entities',
        label: 'Memex List Entities',
        description:
          'List or search entities in the knowledge graph. Without a query, returns top entities by relevance.',
        parameters: Type.Object({
          query: Type.Optional(Type.String({ description: 'Search term to filter by name' })),
          limit: Type.Optional(Type.Number({ description: 'Max entities to return (default: 20)' })),
        }),
        async execute(_toolCallId, params) {
          const { query, limit } = params as { query?: string; limit?: number };

          try {
            const entities = await client.listEntities(query, limit ?? 20);

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
            const origLimit = cfg.searchLimit;
            cfg.searchLimit = limit;
            try {
              const memories = (await client.searchMemories(query)).slice(0, limit);
              for (const m of memories) {
                console.log(`[${m.fact_type}] ${m.text}`);
              }
              if (memories.length === 0) console.log('No memories found.');
            } catch (err) {
              console.error(`Search failed: ${String(err)}`);
            }
            cfg.searchLimit = origLimit;
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

          api.logger.info?.(
            `memory-memex: injecting ${memories.length} memories into context`,
          );
          breaker.recordSuccess();

          return {
            prependContext: formatMemoryContext(memories),
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

          // Only capture user messages to avoid self-poisoning feedback loops:
          // recalled memories → assistant response → re-captured → recalled again.
          for (const msg of [...event.messages].reverse()) {
            if (!msg || typeof msg !== 'object') continue;
            const m = msg as Record<string, unknown>;

            if (m.role === 'user') {
              userText = extractTextContent(m.content);
              break;
            }
          }

          // Strip injected <relevant-memories> block to avoid re-ingesting
          // memories that are already stored in Memex.
          if (userText) {
            userText = userText.replace(
              /<relevant-memories>[\s\S]*?<\/relevant-memories>\s*/,
              '',
            ).trim();
          }

          if (!userText || userText.length < cfg.minCaptureLength) return;

          const now = new Date();
          const markdown = formatConversationNote(
            userText,
            '',
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
          });

          api.logger.info?.('memory-memex: user message captured');
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
