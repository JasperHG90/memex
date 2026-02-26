/**
 * Memex API interfaces — mirror Python Pydantic schemas from
 * packages/common/src/memex_common/schemas.py
 *
 * OpenClaw plugin interfaces are hand-rolled below; they should match
 * @openclaw/plugin-types once that package is published.
 */

// ---------------------------------------------------------------------------
// Memex REST API DTOs
// ---------------------------------------------------------------------------

/** Result item from POST /api/v1/memories/search (NDJSON stream). */
export interface MemoryUnitDTO {
  id: string;
  text: string;
  fact_type: string;
  vault_id?: string | null;
  document_id?: string | null;
  source_document_ids: string[];
  metadata: Record<string, unknown>;
  score?: number | null;
  mentioned_at?: string | null;
  occurred_start?: string | null;
  occurred_end?: string | null;
}

/** Body for POST /api/v1/memories/search */
export interface MemorySearchRequest {
  query: string;
  limit?: number;
  offset?: number;
  vault_ids?: string[] | null;
  skip_opinion_formation?: boolean;
  token_budget?: number | null;
}

/** Body for POST /api/v1/memories/summary */
export interface MemorySummaryRequest {
  query: string;
  texts: string[];
}

/** Response from POST /api/v1/memories/summary */
export interface MemorySummaryResponse {
  summary: string;
}

/**
 * Body for POST /api/v1/ingestions
 * Mirrors NoteCreateDTO from memex_common/schemas.py.
 * `content` and `files` values must be Base64 encoded.
 */
export interface NoteCreateRequest {
  name: string;
  note_key?: string | null;
  description: string;
  content: string;
  files?: Record<string, string>;
  tags?: string[];
  vault_id?: string | null;
}

/** Response from POST /api/v1/ingestions */
export interface IngestResponse {
  status: string;
  document_id?: string | null;
  unit_ids: string[];
  reason?: string | null;
}

/** Resolved plugin configuration. */
export interface PluginConfig {
  serverUrl: string;
  searchLimit: number;
  defaultTags: string[];
  vaultId: string | null;
  beforeTurnTimeoutMs: number;
  minCaptureLength: number;
}

// ---------------------------------------------------------------------------
// OpenClaw plugin contract
// (hand-rolled until @openclaw/plugin-types is published to npm)
// ---------------------------------------------------------------------------

export interface ConversationMessage {
  role: 'user' | 'assistant' | 'system';
  content: string;
  timestamp?: string;
}

/** Event passed to `agent:beforeTurn` hooks. */
export interface AgentBeforeTurnEvent {
  /** Messages in the current conversation so far. */
  messages: ConversationMessage[];
  /** Inject additional context that will be prepended to the next LLM call. */
  injectContext(contextBlock: string): void;
}

/** Event passed to `agent:afterTurn` hooks. */
export interface AgentAfterTurnEvent {
  /** Full conversation messages including the latest AI response. */
  messages: ConversationMessage[];
  /** The latest AI response text. */
  response: string;
}

/** Context object passed to `registerPlugin`. */
export interface PluginContext {
  on(event: 'agent:beforeTurn', handler: (event: AgentBeforeTurnEvent) => Promise<void>): void;
  on(event: 'agent:afterTurn', handler: (event: AgentAfterTurnEvent) => Promise<void>): void;
  logger: {
    debug(msg: string, ...args: unknown[]): void;
    info(msg: string, ...args: unknown[]): void;
    warn(msg: string, ...args: unknown[]): void;
    error(msg: string, ...args: unknown[]): void;
  };
}
