/**
 * Integration test — exercises the openclaw memory plugin against a live Memex server.
 *
 * Usage:
 *   npx tsx test-integration.ts
 *
 * Requires:
 *   - Memex server running on MEMEX_SERVER_URL (default: http://localhost:8000)
 *   - PostgreSQL with pgvector (docker-compose up -d)
 */

import registerPlugin from './src/index';
import type {
  AgentAfterTurnEvent,
  AgentBeforeTurnEvent,
  ConversationMessage,
  PluginContext,
} from './src/types';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function log(label: string, msg: string): void {
  console.log(`[${label}] ${msg}`);
}

/**
 * Build a fake PluginContext that records hook registrations so we can
 * invoke them manually.
 */
function createTestContext() {
  const hooks: Record<string, ((event: unknown) => Promise<void>)[]> = {};

  const ctx: PluginContext = {
    on(event: string, handler: (event: never) => Promise<void>): void {
      hooks[event] ??= [];
      hooks[event].push(handler);
    },
    logger: {
      debug: (msg: string) => log('DEBUG', msg),
      info: (msg: string) => log('INFO', msg),
      warn: (msg: string) => log('WARN', msg),
      error: (msg: string) => log('ERROR', msg),
    },
  };

  return { ctx, hooks };
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  // Increase timeout for integration testing (search + summarize can be slow)
  process.env['MEMEX_BEFORE_TURN_TIMEOUT_MS'] = '30000';
  // Lower minimum capture length so our test messages get captured
  process.env['MEMEX_MIN_CAPTURE_LENGTH'] = '10';

  const serverUrl = process.env['MEMEX_SERVER_URL'] ?? 'http://localhost:8000';
  console.log(`\n=== OpenClaw Memory Plugin — Integration Test ===`);
  console.log(`Memex server: ${serverUrl}\n`);

  // 1. Verify server is reachable
  try {
    const res = await fetch(`${serverUrl}/docs`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    log('SETUP', 'Memex server is reachable');
  } catch (err) {
    console.error(`FATAL: Cannot reach Memex server at ${serverUrl}`);
    console.error('Start it with: memex server start');
    process.exit(1);
  }

  // 2. Register plugin
  const { ctx, hooks } = createTestContext();
  await registerPlugin(ctx);
  log('SETUP', 'Plugin registered');

  const beforeTurnHandlers = hooks['agent:beforeTurn'] ?? [];
  const afterTurnHandlers = hooks['agent:afterTurn'] ?? [];
  log('SETUP', `Hooks: ${beforeTurnHandlers.length} beforeTurn, ${afterTurnHandlers.length} afterTurn`);

  // 3. Simulate beforeTurn — memory recall
  console.log('\n--- beforeTurn (memory recall) ---');
  const messages: ConversationMessage[] = [
    { role: 'user', content: 'What do you remember about recent events?' },
  ];

  let injectedContext = '';
  const beforeEvent: AgentBeforeTurnEvent = {
    messages,
    injectContext(block: string) {
      injectedContext = block;
      log('INJECT', `Context injected (${block.length} chars)`);
    },
  };

  for (const handler of beforeTurnHandlers) {
    await handler(beforeEvent);
  }

  if (injectedContext) {
    console.log('\nInjected context (first 500 chars):');
    console.log(injectedContext.slice(0, 500));
    console.log(injectedContext.length > 500 ? '...\n' : '\n');
  } else {
    log('RESULT', 'No context injected (no matching memories or empty DB)');
  }

  // 4. Simulate afterTurn — conversation capture
  console.log('--- afterTurn (conversation capture) ---');
  const afterMessages: ConversationMessage[] = [
    ...messages,
    { role: 'assistant', content: 'I recall several recent events from memory.' },
  ];

  const afterEvent: AgentAfterTurnEvent = {
    messages: afterMessages,
    response: 'I recall several recent events from memory. Here is a summary of what I found...',
  };

  for (const handler of afterTurnHandlers) {
    await handler(afterEvent);
  }
  log('RESULT', 'afterTurn completed (ingest is fire-and-forget)');

  // Give the fire-and-forget ingest a moment to complete
  await new Promise((resolve) => setTimeout(resolve, 1000));

  console.log('\n=== Integration test complete ===\n');
}

main().catch((err) => {
  console.error('Unhandled error:', err);
  process.exit(1);
});
