/**
 * Memex API interfaces — mirror Python Pydantic schemas from
 * packages/common/src/memex_common/schemas.py
 *
 * OpenClaw plugin interfaces (ConversationMessage, AgentBeforeTurnEvent,
 * AgentAfterTurnEvent, PluginContext) are provided by @openclaw/plugin-sdk.
 */

// ---------------------------------------------------------------------------
// Strategy & status types
// ---------------------------------------------------------------------------

export type MemoryStrategy = 'semantic' | 'keyword' | 'graph' | 'temporal' | 'mental_model';
export type NoteStrategy = 'semantic' | 'keyword' | 'graph' | 'temporal';
export type NoteStatus = 'active' | 'superseded' | 'appended';

export type NoteTemplateType =
  | 'technical_brief'
  | 'general_note'
  | 'architectural_decision_record'
  | 'request_for_comments'
  | 'quick_note';

// ---------------------------------------------------------------------------
// Memex REST API DTOs
// ---------------------------------------------------------------------------

/** Supersession link from contradiction detection. */
export interface SupersessionLink {
  unit_id: string;
  unit_text: string;
  relation: string;
}

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
  confidence?: number | null;
  mentioned_at?: string | null;
  occurred_start?: string | null;
  occurred_end?: string | null;
  unit_metadata?: {
    superseded_by?: SupersessionLink[];
  } | null;
}

/** Body for POST /api/v1/memories/search */
export interface MemorySearchRequest {
  query: string;
  limit?: number;
  offset?: number;
  vault_ids?: string[] | null;
  token_budget?: number | null;
  strategies?: MemoryStrategy[] | null;
  include_superseded?: boolean;
  after?: string | null;
  before?: string | null;
  tags?: string[] | null;
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
  author?: string | null;
}

/** Response from POST /api/v1/ingestions */
export interface IngestResponse {
  status: string;
  document_id?: string | null;
  unit_ids: string[];
  reason?: string | null;
  overlapping_notes?: OverlappingNote[] | null;
}

/** Overlap warning returned from note ingestion. */
export interface OverlappingNote {
  note_id: string;
  title?: string | null;
  similarity: number;
}

// ---------------------------------------------------------------------------
// Note search DTOs
// ---------------------------------------------------------------------------

/** Body for POST /api/v1/notes/search */
export interface NoteSearchRequest {
  query: string;
  limit?: number;
  expand_query?: boolean;
  summarize?: boolean;
  strategies?: NoteStrategy[] | null;
  after?: string | null;
  before?: string | null;
  tags?: string[] | null;
  vault_ids?: string[] | null;
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
// Note management DTOs
// ---------------------------------------------------------------------------

/** Body for PATCH /api/v1/notes/{id}/status */
export interface SetNoteStatusRequest {
  status: NoteStatus;
  linked_note_id?: string | null;
}

/** Body for PATCH /api/v1/notes/{id}/title */
export interface RenameNoteRequest {
  new_title: string;
}

/** Body for POST /api/v1/notes/{id}/migrate */
export interface MigrateNoteRequest {
  target_vault_id: string;
}

/** Minimal note DTO returned from list/get operations. */
export interface NoteDTO {
  id: string;
  title: string;
  created_at?: string | null;
  vault_id?: string | null;
  doc_metadata?: Record<string, unknown> | null;
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
  token_estimate?: number | null;
  children: PageIndexNode[];
}

export interface PageMetadata {
  title?: string | null;
  description?: string | null;
  tags?: string[] | null;
  total_tokens?: number | null;
}

export interface PageIndexOutput {
  toc: PageIndexNode[];
  metadata?: PageMetadata | null;
  total_tokens?: number | null;
}

export interface NoteMetadataOutput {
  note_id: string;
  metadata: Record<string, unknown> | null;
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
  note_id?: string | null;
}

export interface CooccurrenceDTO {
  entity_id: string;
  name: string;
  cooccurrence_count: number;
}

// ---------------------------------------------------------------------------
// Vault DTOs
// ---------------------------------------------------------------------------

export interface VaultDTO {
  id: string;
  name: string;
  description?: string | null;
}

// ---------------------------------------------------------------------------
// Reflection DTOs
// ---------------------------------------------------------------------------

export interface ReflectionRequest {
  entity_id: string;
  limit_recent_memories?: number;
  vault_id?: string | null;
}

export interface ReflectionResultDTO {
  status: string;
  entity_id?: string | null;
  observations?: unknown[] | null;
}

// ---------------------------------------------------------------------------
// Plugin configuration
// ---------------------------------------------------------------------------

export type CaptureMode = 'filtered' | 'full';

export interface SessionTurn {
  userMessage: string;
  assistantMessage: string;
  timestamp: Date;
}

/** Resolved plugin configuration. */
export interface PluginConfig {
  serverUrl: string;
  searchLimit: number;
  tokenBudget: number | null;
  defaultTags: string[];
  vaultId: string | null;
  vaultName: string;
  beforeTurnTimeoutMs: number;
  minCaptureLength: number;
  autoRecall: boolean;
  autoCapture: boolean;
  timeoutMs: number;
  profileFrequency: number;
  captureMode: CaptureMode;
  sessionGrouping: boolean;
}
