/**
 * OpenClaw memory plugin for Memex.
 *
 * Registers two lifecycle hooks:
 *   - agent:beforeTurn  — auto-recall: injects relevant memories as context
 *   - agent:afterTurn   — auto-capture: stores the current turn as a note
 *
 * Configuration is read from environment variables (consistent with the
 * Memex MCP server configuration):
 *
 *   MEMEX_SERVER_URL             default: http://localhost:8000
 *   MEMEX_SEARCH_LIMIT           default: 8
 *   MEMEX_DEFAULT_TAGS           default: agent,openclaw
 *   MEMEX_VAULT_ID               optional
 *   MEMEX_BEFORE_TURN_TIMEOUT_MS default: 3000
 *   MEMEX_MIN_CAPTURE_LENGTH     default: 50
 */

import { CircuitBreaker } from './circuit-breaker';
import {
  MemexClient,
  encodeBase64,
  formatConversationNote,
  hashTurnKey,
} from './memex-client';
import type {
  AgentAfterTurnEvent,
  AgentBeforeTurnEvent,
  PluginConfig,
  PluginContext,
} from './types';

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------

function resolveConfig(): PluginConfig {
  return {
    serverUrl: process.env['MEMEX_SERVER_URL'] ?? 'http://localhost:8000',
    searchLimit: parseInt(process.env['MEMEX_SEARCH_LIMIT'] ?? '8', 10),
    defaultTags: (process.env['MEMEX_DEFAULT_TAGS'] ?? 'agent,openclaw')
      .split(',')
      .map((t) => t.trim())
      .filter(Boolean),
    vaultId: process.env['MEMEX_VAULT_ID'] ?? null,
    beforeTurnTimeoutMs: parseInt(process.env['MEMEX_BEFORE_TURN_TIMEOUT_MS'] ?? '3000', 10),
    minCaptureLength: parseInt(process.env['MEMEX_MIN_CAPTURE_LENGTH'] ?? '50', 10),
  };
}

// ---------------------------------------------------------------------------
// Plugin entry point
// ---------------------------------------------------------------------------

export default async function registerPlugin(ctx: PluginContext): Promise<void> {
  const config = resolveConfig();
  const client = new MemexClient(config);
  const breaker = new CircuitBreaker({ failureThreshold: 3, resetTimeoutMs: 60_000 });

  // -------------------------------------------------------------------------
  // agent:beforeTurn — auto-recall (awaited; must complete within timeout)
  // -------------------------------------------------------------------------
  ctx.on('agent:beforeTurn', async (event: AgentBeforeTurnEvent): Promise<void> => {
    if (breaker.isOpen()) {
      ctx.logger.debug('[memex-openclaw] Circuit breaker open — skipping memory recall');
      return;
    }

    const userMessage = findLastUserMessage(event.messages);
    if (userMessage == null) return;

    const signal = AbortSignal.timeout(config.beforeTurnTimeoutMs);

    try {
      const memories = await client.searchMemories(userMessage, signal);

      if (memories.length === 0) {
        breaker.recordSuccess();
        return;
      }

      const texts = memories.map((m) => m.text);
      let body: string;

      try {
        const summary = await client.summarizeMemories(userMessage, texts, signal);
        body = summary.summary;
      } catch (summaryErr: unknown) {
        // Degrade gracefully: format top-5 raw snippets if summarization fails.
        const errMsg = summaryErr instanceof Error ? summaryErr.message : String(summaryErr);
        ctx.logger.warn(`[memex-openclaw] Summary failed (using raw snippets): ${errMsg}`);
        body = texts
          .slice(0, 5)
          .map((t, i) => `[${i}] ${t}`)
          .join('\n\n');
      }

      event.injectContext(formatMemoryContext(body, memories.length));
      breaker.recordSuccess();
    } catch (err: unknown) {
      breaker.recordFailure();
      const message = err instanceof Error ? err.message : String(err);
      ctx.logger.error(`[memex-openclaw] beforeTurn recall failed: ${message}`);
      // Never propagate — a memory failure must not block the user's turn.
    }
  });

  // -------------------------------------------------------------------------
  // agent:afterTurn — auto-capture (fire-and-forget; never blocks)
  // -------------------------------------------------------------------------
  ctx.on('agent:afterTurn', async (event: AgentAfterTurnEvent): Promise<void> => {
    try {
      const userMessage = findLastUserMessage(event.messages);
      if (userMessage == null) return;

      if (userMessage.length < config.minCaptureLength) {
        ctx.logger.debug('[memex-openclaw] Message too short — skipping capture');
        return;
      }

      const now = new Date();
      const markdown = formatConversationNote(userMessage, event.response, now);
      const content = encodeBase64(markdown);
      const noteKey = hashTurnKey(userMessage, now);

      client.ingestNote({
        name: `Conversation — ${now.toISOString()}`,
        note_key: noteKey,
        description: `Agent conversation captured on ${now.toISOString()}`,
        content,
        tags: config.defaultTags,
        ...(config.vaultId != null ? { vault_id: config.vaultId } : {}),
      });
      // ingestNote is fire-and-forget; errors are logged inside the client.
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : String(err);
      ctx.logger.error(`[memex-openclaw] afterTurn capture failed: ${message}`);
      // Never propagate — a capture failure must not surface to the user.
    }
  });

  ctx.logger.info('[memex-openclaw] Memory plugin registered');
}

// ---------------------------------------------------------------------------
// Private helpers
// ---------------------------------------------------------------------------

function findLastUserMessage(
  messages: Array<{ role: string; content: string }>,
): string | undefined {
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i]?.role === 'user') {
      return messages[i]?.content;
    }
  }
  return undefined;
}

function formatMemoryContext(body: string, count: number): string {
  return [
    '<!-- MEMEX MEMORY CONTEXT START -->',
    `Relevant memories (${count} retrieved):`,
    '',
    body,
    '<!-- MEMEX MEMORY CONTEXT END -->',
  ].join('\n');
}
