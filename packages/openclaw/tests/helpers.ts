import type { MemoryUnitDTO, PluginConfig } from '../src/types';

// ---------------------------------------------------------------------------
// Factory: PluginConfig
// ---------------------------------------------------------------------------

export function makeConfig(overrides: Partial<PluginConfig> = {}): PluginConfig {
  return {
    serverUrl: 'http://localhost:8000',
    searchLimit: 8,
    tokenBudget: null,
    defaultTags: ['agent', 'openclaw'],
    vaultId: null,
    vaultName: 'OpenClaw',
    beforeTurnTimeoutMs: 3000,
    minCaptureLength: 50,
    timeoutMs: 5000,
    autoRecall: true,
    autoCapture: true,
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
// Factory: OpenClawPluginApi (mock)
// ---------------------------------------------------------------------------

export interface MockOpenClawPluginApi {
  pluginConfig: Record<string, unknown>;
  logger: {
    debug: ReturnType<typeof vi.fn>;
    info: ReturnType<typeof vi.fn>;
    warn: ReturnType<typeof vi.fn>;
    error: ReturnType<typeof vi.fn>;
  };
  tools: Map<string, { definition: unknown; execute: (...args: unknown[]) => Promise<unknown> }>;
  commands: Map<string, { definition: unknown; handler: (ctx: unknown) => Promise<unknown> }>;
  hooks: Map<string, Array<(...args: unknown[]) => Promise<unknown>>>;
  cliRegistrations: Array<(ctx: unknown) => void>;
  services: Map<string, { start: () => void; stop: () => void }>;
  registerTool(def: unknown, meta: unknown): void;
  registerCommand(def: unknown): void;
  on(event: string, handler: (...args: unknown[]) => Promise<unknown>): void;
  registerCli(fn: (ctx: unknown) => void, meta: unknown): void;
  registerService(def: unknown): void;
}

export function makeOpenClawPluginApi(
  configOverrides: Record<string, unknown> = {},
): MockOpenClawPluginApi {
  const tools = new Map<
    string,
    { definition: unknown; execute: (...args: unknown[]) => Promise<unknown> }
  >();
  const commands = new Map<
    string,
    { definition: unknown; handler: (ctx: unknown) => Promise<unknown> }
  >();
  const hooks = new Map<string, Array<(...args: unknown[]) => Promise<unknown>>>();
  const cliRegistrations: Array<(ctx: unknown) => void> = [];
  const services = new Map<string, { start: () => void; stop: () => void }>();

  const api: MockOpenClawPluginApi = {
    pluginConfig: configOverrides,
    logger: {
      debug: vi.fn(),
      info: vi.fn(),
      warn: vi.fn(),
      error: vi.fn(),
    },
    tools,
    commands,
    hooks,
    cliRegistrations,
    services,

    registerTool(def: unknown, _meta: unknown) {
      const d = def as { name: string; execute: (...args: unknown[]) => Promise<unknown> };
      tools.set(d.name, { definition: def, execute: d.execute });
    },

    registerCommand(def: unknown) {
      const d = def as { name: string; handler: (ctx: unknown) => Promise<unknown> };
      commands.set(d.name, { definition: def, handler: d.handler });
    },

    on(event: string, handler: (...args: unknown[]) => Promise<unknown>) {
      if (!hooks.has(event)) hooks.set(event, []);
      hooks.get(event)!.push(handler);
    },

    registerCli(fn: (ctx: unknown) => void, _meta: unknown) {
      cliRegistrations.push(fn);
    },

    registerService(def: unknown) {
      const d = def as { id: string; start: () => void; stop: () => void };
      services.set(d.id, { start: d.start, stop: d.stop });
    },
  };

  return api;
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

/** Build a 200 OK response for the vault check (ensureVault). */
export function vaultOkResponse(): Response {
  return jsonResponse({ id: 'v1', name: 'OpenClaw' });
}

/** Build an error response. */
export function errorResponse(status: number, body = 'Internal Server Error'): Response {
  return new Response(body, { status, statusText: body });
}
