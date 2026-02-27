import type {
  AgentAfterTurnEvent,
  AgentBeforeTurnEvent,
  ConversationMessage,
  MemoryUnitDTO,
  PluginConfig,
  PluginContext,
} from '../src/types';

// ---------------------------------------------------------------------------
// Factory: PluginConfig
// ---------------------------------------------------------------------------

export function makeConfig(overrides: Partial<PluginConfig> = {}): PluginConfig {
  return {
    serverUrl: 'http://localhost:8000',
    searchLimit: 8,
    defaultTags: ['agent', 'openclaw'],
    vaultId: null,
    beforeTurnTimeoutMs: 3000,
    minCaptureLength: 50,
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Factory: MemoryUnitDTO
// ---------------------------------------------------------------------------

let memoryCounter = 0;

export function makeMemoryUnit(overrides: Partial<MemoryUnitDTO> = {}): MemoryUnitDTO {
  memoryCounter++;
  return {
    id: `mem-${memoryCounter}`,
    text: `Memory fact #${memoryCounter}`,
    fact_type: 'observation',
    source_document_ids: [],
    metadata: {},
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Factory: PluginContext (mock)
// ---------------------------------------------------------------------------

type EventHandler<T> = (event: T) => Promise<void>;

export interface MockPluginContext extends PluginContext {
  handlers: {
    beforeTurn: EventHandler<AgentBeforeTurnEvent>[];
    afterTurn: EventHandler<AgentAfterTurnEvent>[];
  };
  logger: {
    debug: ReturnType<typeof vi.fn>;
    info: ReturnType<typeof vi.fn>;
    warn: ReturnType<typeof vi.fn>;
    error: ReturnType<typeof vi.fn>;
  };
}

export function makePluginContext(): MockPluginContext {
  const handlers: MockPluginContext['handlers'] = {
    beforeTurn: [],
    afterTurn: [],
  };

  return {
    handlers,
    on(event: string, handler: EventHandler<never>) {
      if (event === 'agent:beforeTurn') {
        handlers.beforeTurn.push(handler as EventHandler<AgentBeforeTurnEvent>);
      } else if (event === 'agent:afterTurn') {
        handlers.afterTurn.push(handler as EventHandler<AgentAfterTurnEvent>);
      }
    },
    logger: {
      debug: vi.fn(),
      info: vi.fn(),
      warn: vi.fn(),
      error: vi.fn(),
    },
  };
}

// ---------------------------------------------------------------------------
// Factory: Conversation messages
// ---------------------------------------------------------------------------

export function makeMessages(
  ...msgs: Array<{ role: ConversationMessage['role']; content: string }>
): ConversationMessage[] {
  return msgs.map((m) => ({ role: m.role, content: m.content }));
}

// ---------------------------------------------------------------------------
// Fake fetch response helpers
// ---------------------------------------------------------------------------

/** Build a Response that streams NDJSON lines. */
export function ndjsonResponse(items: unknown[]): Response {
  const ndjson = items.map((item) => JSON.stringify(item)).join('\n') + '\n';
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(ndjson));
      controller.close();
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { 'Content-Type': 'application/x-ndjson' },
  });
}

/** Build a standard JSON response. */
export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

/** Build an error response. */
export function errorResponse(status: number, body = 'Internal Server Error'): Response {
  return new Response(body, { status, statusText: body });
}
