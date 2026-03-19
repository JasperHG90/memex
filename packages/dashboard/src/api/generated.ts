/**
 * Auto-generated Zod schemas from the Memex OpenAPI spec.
 * Re-run with: npm run generate-api
 */
import { z } from 'zod';

// --- Enums ---

export const FactTypes = z.enum(['world', 'event', 'observation']);
export type FactTypes = z.infer<typeof FactTypes>;

// --- Core DTOs ---

export const VaultDTO = z.object({
  id: z.string().uuid(),
  name: z.string(),
  description: z.union([z.string(), z.null()]).optional(),
  is_active: z.boolean().optional().default(false),
});
export type VaultDTO = z.infer<typeof VaultDTO>;

export const CreateVaultRequest = z.object({
  name: z.string(),
  description: z.union([z.string(), z.null()]).optional(),
});
export type CreateVaultRequest = z.infer<typeof CreateVaultRequest>;

export const EntityDTO = z.object({
  id: z.string().uuid(),
  name: z.string(),
  mention_count: z.number().int().optional().default(0),
  vault_id: z.union([z.string(), z.null()]).optional(),
  entity_type: z.union([z.string(), z.null()]).optional(),
  metadata: z.record(z.string(), z.unknown()).optional().default({}),
});
export type EntityDTO = z.infer<typeof EntityDTO>;

export const StrategyDebugInfo = z.object({
  strategy_name: z.string(),
  rank: z.number().int(),
  rrf_score: z.number(),
  raw_score: z.union([z.number(), z.null()]).optional(),
  timing_ms: z.union([z.number(), z.null()]).optional(),
});
export type StrategyDebugInfo = z.infer<typeof StrategyDebugInfo>;

export const SupersessionInfo = z.object({
  unit_id: z.string().uuid(),
  unit_text: z.string(),
  note_title: z.union([z.string(), z.null()]).optional(),
  relation: z.string(),
});
export type SupersessionInfo = z.infer<typeof SupersessionInfo>;

export const MemoryUnitDTO = z.object({
  id: z.string().uuid(),
  text: z.string(),
  fact_type: FactTypes,
  status: z.string().optional().default('active'),
  vault_id: z.union([z.string(), z.null()]).optional(),
  note_id: z.union([z.string(), z.null()]).optional(),
  chunk_id: z.union([z.string(), z.null()]).optional(),
  node_ids: z.array(z.string()).optional().default([]),
  source_note_ids: z.array(z.string().uuid()).optional(),
  metadata: z.record(z.string(), z.unknown()).optional(),
  score: z.union([z.number(), z.null()]).optional(),
  confidence: z.number().optional().default(1.0),
  debug_info: z.union([z.array(StrategyDebugInfo), z.null()]).optional(),
  superseded_by: z.union([z.array(SupersessionInfo), z.null()]).optional(),
  mentioned_at: z.union([z.string(), z.null()]).optional(),
  occurred_start: z.union([z.string(), z.null()]).optional(),
  occurred_end: z.union([z.string(), z.null()]).optional(),
});
export type MemoryUnitDTO = z.infer<typeof MemoryUnitDTO>;

export const ObservationDTO = z.object({
  id: z.union([z.string(), z.null()]).optional(),
  title: z.string(),
  content: z.string(),
  trend: z.union([z.string(), z.null()]).optional(),
  evidence: z.array(z.record(z.string(), z.unknown())).optional(),
});
export type ObservationDTO = z.infer<typeof ObservationDTO>;

export const NoteDTO = z.object({
  id: z.string().uuid(),
  title: z.union([z.string(), z.null()]).optional(),
  name: z.union([z.string(), z.null()]).optional(),
  original_text: z.union([z.string(), z.null()]).optional(),
  created_at: z.string(),
  publish_date: z.union([z.string(), z.null()]).optional(),
  vault_id: z.string().uuid(),
  assets: z.array(z.string()).optional(),
  doc_metadata: z.record(z.string(), z.unknown()).optional(),
});
export type NoteDTO = z.infer<typeof NoteDTO>;

export const NodeDTO = z.object({
  id: z.string().uuid(),
  note_id: z.string().uuid(),
  vault_id: z.string().uuid(),
  title: z.string(),
  text: z.string(),
  level: z.number().int(),
  seq: z.number().int(),
  status: z.string(),
  created_at: z.string(),
});
export type NodeDTO = z.infer<typeof NodeDTO>;

export const SectionSummaryDTO = z.object({
  who: z.union([z.string(), z.null()]).optional(),
  what: z.union([z.string(), z.null()]).optional(),
  how: z.union([z.string(), z.null()]).optional(),
  when: z.union([z.string(), z.null()]).optional(),
  where: z.union([z.string(), z.null()]).optional(),
});
export type SectionSummaryDTO = z.infer<typeof SectionSummaryDTO>;

export const BlockSummaryDTO = z.object({
  topic: z.string(),
  key_points: z.array(z.string()).optional().default([]),
});
export type BlockSummaryDTO = z.infer<typeof BlockSummaryDTO>;

export const NoteSearchResult = z.object({
  note_id: z.string().uuid(),
  metadata: z.record(z.string(), z.unknown()),
  summaries: z.array(BlockSummaryDTO).optional().default([]),
  score: z.number().optional().default(0),
  vault_id: z.union([z.string(), z.null()]).optional(),
  vault_name: z.union([z.string(), z.null()]).optional(),
  reasoning: z.union([z.array(z.record(z.string(), z.unknown())), z.null()]).optional(),
  answer: z.union([z.string(), z.null()]).optional(),
  note_status: z.union([z.string(), z.null()]).optional(),
});
export type NoteSearchResult = z.infer<typeof NoteSearchResult>;

// --- Request schemas ---

export const RetrievalRequest = z.object({
  query: z.string(),
  limit: z.number().int().optional().default(10),
  offset: z.number().int().optional().default(0),
  token_budget: z.union([z.number(), z.null()]).optional(),
  vault_ids: z.union([z.array(z.string()), z.null()]).optional(),
  filters: z.record(z.string(), z.unknown()).optional(),
  after: z.union([z.string(), z.null()]).optional(),
  before: z.union([z.string(), z.null()]).optional(),
  tags: z.union([z.array(z.string()), z.null()]).optional(),
  rerank: z.boolean().optional().default(true),
  min_score: z.union([z.number(), z.null()]).optional(),
  strategy_weights: z.union([z.record(z.string(), z.number()), z.null()]).optional(),
  strategies: z.union([z.array(z.string()), z.null()]).optional(),
  include_vectors: z.boolean().optional().default(false),
  include_stale: z.boolean().optional().default(false),
  include_superseded: z.boolean().optional(),
  debug: z.boolean().optional(),
});
export type RetrievalRequest = z.infer<typeof RetrievalRequest>;

export const NoteSearchRequest = z.object({
  query: z.string(),
  limit: z.number().int().optional().default(10),
  vault_ids: z.union([z.array(z.string()), z.null()]).optional(),
  expand_query: z.boolean().optional().default(false),
  fusion_strategy: z.string().optional().default('rrf'),
  strategies: z.array(z.string()).optional().default(['semantic', 'keyword', 'graph', 'temporal']),
  strategy_weights: z.union([z.record(z.string(), z.number()), z.null()]).optional(),
  after: z.union([z.string(), z.null()]).optional(),
  before: z.union([z.string(), z.null()]).optional(),
  tags: z.union([z.array(z.string()), z.null()]).optional(),
  reason: z.boolean().optional().default(false),
  summarize: z.boolean().optional().default(false),
  mmr_lambda: z.union([z.number(), z.null()]).optional(),
});
export type NoteSearchRequest = z.infer<typeof NoteSearchRequest>;

export const NoteCreateDTO = z.object({
  name: z.string(),
  note_key: z.union([z.string(), z.null()]).optional(),
  description: z.string(),
  content: z.string(),
  files: z.record(z.string(), z.string()).optional(),
  tags: z.array(z.string()).optional(),
  vault_id: z.union([z.string(), z.null()]).optional(),
});
export type NoteCreateDTO = z.infer<typeof NoteCreateDTO>;

export const SummaryRequest = z.object({
  query: z.string(),
  texts: z.array(z.string()).max(50),
});
export type SummaryRequest = z.infer<typeof SummaryRequest>;

export const SummaryResponse = z.object({
  summary: z.string(),
});
export type SummaryResponse = z.infer<typeof SummaryResponse>;

// --- Response schemas ---

export const IngestResponse = z.object({
  status: z.string(),
  note_id: z.union([z.string(), z.null()]).optional(),
  unit_ids: z.array(z.string().uuid()).optional(),
  reason: z.union([z.string(), z.null()]).optional(),
});
export type IngestResponse = z.infer<typeof IngestResponse>;

export const BatchIngestResponse = z.object({
  processed_count: z.number().int().default(0),
  skipped_count: z.number().int().default(0),
  failed_count: z.number().int().default(0),
  note_ids: z.array(z.string()),
  errors: z.array(z.record(z.string(), z.unknown())),
});
export type BatchIngestResponse = z.infer<typeof BatchIngestResponse>;

export const BatchJobStatus = z.object({
  job_id: z.string().uuid(),
  status: z.string(),
  progress: z.union([z.string(), z.null()]).optional(),
  result: z.union([BatchIngestResponse, z.null()]).optional(),
});
export type BatchJobStatus = z.infer<typeof BatchJobStatus>;

export const SystemStatsCountsDTO = z.object({
  notes: z.number().int().default(0),
  memories: z.number().int(),
  entities: z.number().int(),
  reflection_queue: z.number().int(),
});
export type SystemStatsCountsDTO = z.infer<typeof SystemStatsCountsDTO>;

export const TokenUsageStatDTO = z.object({
  date: z.string(),
  total_tokens: z.number().int(),
});
export type TokenUsageStatDTO = z.infer<typeof TokenUsageStatDTO>;

export const TokenUsageResponse = z.object({
  usage: z.array(TokenUsageStatDTO),
});
export type TokenUsageResponse = z.infer<typeof TokenUsageResponse>;

export const ReflectionResultDTO = z.object({
  entity_id: z.string().uuid(),
  new_observations: z.array(ObservationDTO),
  status: z.string().optional().default('success'),
});
export type ReflectionResultDTO = z.infer<typeof ReflectionResultDTO>;

export const ReflectionQueueDTO = z.object({
  entity_id: z.string().uuid(),
  vault_id: z.string().uuid(),
  priority_score: z.number().optional().default(0),
});
export type ReflectionQueueDTO = z.infer<typeof ReflectionQueueDTO>;

// Recursive type for lineage
type LineageResponseType = {
  entity_type: string;
  entity: Record<string, unknown>;
  derived_from?: LineageResponseType[];
};

export const LineageResponse: z.ZodType<LineageResponseType> = z.lazy(() =>
  z.object({
    entity_type: z.string(),
    entity: z.record(z.string(), z.unknown()),
    derived_from: z.array(LineageResponse).optional(),
  }),
);
export type LineageResponse = z.infer<typeof LineageResponse>;

// --- Entity mention response ---

export const EntityMention = z.object({
  unit: MemoryUnitDTO,
  document: NoteDTO,
});
export type EntityMention = z.infer<typeof EntityMention>;

// --- Co-occurrence ---

export const CooccurrenceRecord = z.object({
  entity_id_1: z.string().uuid(),
  entity_id_2: z.string().uuid(),
  cooccurrence_count: z.number().int(),
  vault_id: z.string().uuid(),
});
export type CooccurrenceRecord = z.infer<typeof CooccurrenceRecord>;
