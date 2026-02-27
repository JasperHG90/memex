/**
 * Memex API interfaces — mirror Python Pydantic schemas from
 * packages/common/src/memex_common/schemas.py
 *
 * OpenClaw plugin interfaces (ConversationMessage, AgentBeforeTurnEvent,
 * AgentAfterTurnEvent, PluginContext) are provided by @openclaw/plugin-sdk.
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

// ---------------------------------------------------------------------------
// Note search DTOs
// ---------------------------------------------------------------------------

/** Body for POST /api/v1/notes/search */
export interface NoteSearchRequest {
  query: string;
  limit?: number;
  expand_query?: boolean;
  reason?: boolean;
  summarize?: boolean;
}

export interface NoteSearchSnippet {
  text: string;
  node_title: string;
  node_id: string;
}

export interface NoteSearchResult {
  note_id: string;
  metadata?: Record<string, unknown>;
  snippets: NoteSearchSnippet[];
  score?: number | null;
  reasoning?: string[];
  answer?: string | null;
}

// ---------------------------------------------------------------------------
// Page index / node DTOs
// ---------------------------------------------------------------------------

export interface PageIndexNode {
  id: string;
  title: string;
  summary?: string | null;
  level: number;
  seq: number;
  children: PageIndexNode[];
}

export interface PageIndexOutput {
  toc: PageIndexNode[];
}

export interface NodeDTO {
  id: string;
  note_id: string;
  title: string;
  text: string;
  level: number;
  seq: number;
}

// ---------------------------------------------------------------------------
// Lineage DTOs
// ---------------------------------------------------------------------------

export interface LineageItem {
  entity_type: string;
  entity: Record<string, unknown>;
  derived_from: LineageItem[];
}

export type LineageResponse = LineageItem;

// ---------------------------------------------------------------------------
// Entity DTOs
// ---------------------------------------------------------------------------

export interface EntityDTO {
  id: string;
  name: string;
  entity_type?: string | null;
  mention_count?: number | null;
}

export interface EntityMentionDTO {
  id: string;
  text: string;
  fact_type: string;
  score?: number | null;
}

// ---------------------------------------------------------------------------
// Plugin configuration
// ---------------------------------------------------------------------------

/** Resolved plugin configuration. */
export interface PluginConfig {
  serverUrl: string;
  searchLimit: number;
  defaultTags: string[];
  vaultId: string | null;
  vaultName: string;
  beforeTurnTimeoutMs: number;
  minCaptureLength: number;
  autoRecall: boolean;
  autoCapture: boolean;
  timeoutMs: number;
}
