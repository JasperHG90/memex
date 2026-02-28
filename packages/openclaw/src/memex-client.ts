import { createHash } from 'node:crypto';

import type {
  EntityDTO,
  EntityMentionDTO,
  IngestResponse,
  LineageResponse,
  MemorySearchRequest,
  MemorySummaryRequest,
  MemorySummaryResponse,
  MemoryUnitDTO,
  NodeDTO,
  NoteCreateRequest,
  NoteSearchResult,
  PageIndexOutput,
  PluginConfig,
  SessionTurn,
} from './types';

export class MemexClient {
  private readonly baseUrl: string;
  private readonly config: PluginConfig;
  private readonly logger?: { warn?: (msg: string) => void };
  private vaultVerified = false;

  constructor(config: PluginConfig, logger?: { warn?: (msg: string) => void }) {
    this.config = config;
    this.baseUrl = config.serverUrl.replace(/\/$/, '');
    this.logger = logger;
  }

  /**
   * Ensure the configured vault exists, creating it if needed.
   * Called lazily on first operation that needs it.
   */
  private async ensureVault(): Promise<void> {
    if (this.vaultVerified) return;

    const vaultIdentifier = this.config.vaultId ?? this.config.vaultName;
    if (!vaultIdentifier) {
      this.vaultVerified = true;
      return;
    }

    try {
      const response = await fetch(
        `${this.baseUrl}/api/v1/vaults/${encodeURIComponent(vaultIdentifier)}`,
      );
      if (response.ok) {
        this.vaultVerified = true;
        return;
      }
      if (response.status === 404) {
        // Vault doesn't exist — create it
        const name = this.config.vaultName ?? String(vaultIdentifier);
        const createResponse = await fetch(`${this.baseUrl}/api/v1/vaults`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            name,
            description: 'Auto-created by OpenClaw',
          }),
        });
        if (!createResponse.ok) {
          const text = await createResponse.text();
          this.logger?.warn?.(
            `[memex-openclaw] Failed to create vault "${name}": ${createResponse.status} - ${text}`,
          );
        }
        this.vaultVerified = true;
        return;
      }
      // Other errors — mark verified to avoid retrying every call
      this.logger?.warn?.(
        `[memex-openclaw] Vault check failed: ${response.status} ${response.statusText}`,
      );
      this.vaultVerified = true;
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      this.logger?.warn?.(`[memex-openclaw] Vault check failed: ${message}`);
      this.vaultVerified = true;
    }
  }

  /**
   * POST /api/v1/memories/search
   * Returns a stream of MemoryUnitDTO via NDJSON.
   */
  async searchMemories(
    query: string,
    signal?: AbortSignal,
    overrides?: { limit?: number; token_budget?: number | null },
  ): Promise<MemoryUnitDTO[]> {
    await this.ensureVault();
    const vaultIdentifier = this.config.vaultId ?? this.config.vaultName;
    const effectiveLimit = overrides?.limit ?? this.config.searchLimit;
    const effectiveTokenBudget = overrides?.token_budget !== undefined
      ? overrides.token_budget
      : this.config.tokenBudget;
    const request: MemorySearchRequest = {
      query,
      limit: effectiveLimit,
      skip_opinion_formation: true,
      vault_ids: [vaultIdentifier],
      ...(effectiveTokenBudget != null && { token_budget: effectiveTokenBudget }),
    };

    const response = await fetch(`${this.baseUrl}/api/v1/memories/search`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
      signal,
    });

    if (!response.ok) {
      // 404 = vault not found or empty — treat as "no memories" rather than
      // a hard error so the agent doesn't retry in an infinite loop.
      if (response.status === 404) {
        this.logger?.warn?.(`Memex vault not found (404) — returning empty results`);
        return [];
      }
      throw new Error(`Memex search failed: ${response.status} ${response.statusText}`);
    }

    return this._parseNdjsonStream<MemoryUnitDTO>(response);
  }

  /**
   * POST /api/v1/memories/summary
   * Returns a plain JSON { summary: string } response.
   */
  async summarizeMemories(
    query: string,
    texts: string[],
    signal?: AbortSignal,
  ): Promise<MemorySummaryResponse> {
    const request: MemorySummaryRequest = { query, texts };

    const response = await fetch(`${this.baseUrl}/api/v1/memories/summary`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
      signal,
    });

    if (!response.ok) {
      throw new Error(`Memex summary failed: ${response.status} ${response.statusText}`);
    }

    return response.json() as Promise<MemorySummaryResponse>;
  }

  /**
   * POST /api/v1/ingestions?background=true
   * Fire-and-forget: returns 202 immediately; errors are logged, never thrown.
   */
  ingestNote(request: NoteCreateRequest): void {
    const vaultIdentifier = this.config.vaultId ?? this.config.vaultName;
    const body: NoteCreateRequest = {
      ...request,
      vault_id: vaultIdentifier,
    };

    void (async () => {
      try {
        await this.ensureVault();
      } catch {
        // ensureVault already logs warnings
      }

      void fetch(`${this.baseUrl}/api/v1/ingestions?background=true`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
        .then((response: Response): Promise<void> | void => {
          if (!response.ok) {
            return response.text().then((text: string) => {
              throw new Error(`Memex ingest failed: ${response.status} - ${text}`);
            });
          }
        })
        .catch((err: unknown) => {
          const message = err instanceof Error ? err.message : String(err);
          const warn = this.logger?.warn ?? console.warn;
          warn(`[memex-openclaw] Background ingest failed: ${message}`);
        });
    })();
  }

  /**
   * POST /api/v1/notes/search
   * Search source notes with optional synthesis.
   */
  async searchNotes(
    query: string,
    opts?: { limit?: number; expand_query?: boolean; reason?: boolean; summarize?: boolean },
    signal?: AbortSignal,
  ): Promise<NoteSearchResult[]> {
    const body = {
      query,
      limit: opts?.limit ?? 5,
      expand_query: opts?.expand_query ?? false,
      reason: opts?.reason ?? false,
      summarize: opts?.summarize ?? false,
    };

    const response = await fetch(`${this.baseUrl}/api/v1/notes/search`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal,
    });

    if (!response.ok) {
      if (response.status === 404) {
        this.logger?.warn?.(`Memex vault not found (404) — returning empty note results`);
        return [];
      }
      throw new Error(`Memex note search failed: ${response.status} ${response.statusText}`);
    }

    return response.json() as Promise<NoteSearchResult[]>;
  }

  /** GET /api/v1/notes/{id} */
  async getNote(noteId: string, signal?: AbortSignal): Promise<Record<string, unknown>> {
    const response = await fetch(`${this.baseUrl}/api/v1/notes/${noteId}`, { signal });
    if (!response.ok) {
      throw new Error(`Memex getNote failed: ${response.status} ${response.statusText}`);
    }
    return response.json() as Promise<Record<string, unknown>>;
  }

  /** GET /api/v1/notes/{id}/page-index */
  async getPageIndex(noteId: string, signal?: AbortSignal): Promise<PageIndexOutput> {
    const response = await fetch(
      `${this.baseUrl}/api/v1/notes/${noteId}/page-index`,
      { signal },
    );
    if (!response.ok) {
      throw new Error(`Memex getPageIndex failed: ${response.status} ${response.statusText}`);
    }
    return response.json() as Promise<PageIndexOutput>;
  }

  /** GET /api/v1/nodes/{id} */
  async getNode(nodeId: string, signal?: AbortSignal): Promise<NodeDTO> {
    const response = await fetch(`${this.baseUrl}/api/v1/nodes/${nodeId}`, { signal });
    if (!response.ok) {
      throw new Error(`Memex getNode failed: ${response.status} ${response.statusText}`);
    }
    return response.json() as Promise<NodeDTO>;
  }

  /** GET /api/v1/lineage/{type}/{id} */
  async getLineage(
    unitId: string,
    entityType = 'memory_unit',
    signal?: AbortSignal,
  ): Promise<LineageResponse> {
    const response = await fetch(
      `${this.baseUrl}/api/v1/lineage/${entityType}/${unitId}`,
      { signal },
    );
    if (!response.ok) {
      throw new Error(`Memex getLineage failed: ${response.status} ${response.statusText}`);
    }
    return response.json() as Promise<LineageResponse>;
  }

  /** GET /api/v1/entities */
  async listEntities(
    query?: string,
    limit = 20,
    signal?: AbortSignal,
  ): Promise<EntityDTO[]> {
    const params = new URLSearchParams();
    if (query) params.set('query', query);
    params.set('limit', String(limit));
    const qs = params.toString();
    const response = await fetch(`${this.baseUrl}/api/v1/entities?${qs}`, { signal });
    if (!response.ok) {
      throw new Error(`Memex listEntities failed: ${response.status} ${response.statusText}`);
    }
    return this._parseNdjsonStream<EntityDTO>(response);
  }

  /** GET /api/v1/entities/{id} */
  async getEntity(entityId: string, signal?: AbortSignal): Promise<EntityDTO> {
    const response = await fetch(`${this.baseUrl}/api/v1/entities/${entityId}`, { signal });
    if (!response.ok) {
      throw new Error(`Memex getEntity failed: ${response.status} ${response.statusText}`);
    }
    return response.json() as Promise<EntityDTO>;
  }

  /** GET /api/v1/entities/{id}/mentions */
  async getEntityMentions(
    entityId: string,
    limit = 10,
    signal?: AbortSignal,
  ): Promise<EntityMentionDTO[]> {
    const params = new URLSearchParams({ limit: String(limit) });
    const response = await fetch(
      `${this.baseUrl}/api/v1/entities/${entityId}/mentions?${params}`,
      { signal },
    );
    if (!response.ok) {
      throw new Error(
        `Memex getEntityMentions failed: ${response.status} ${response.statusText}`,
      );
    }
    return response.json() as Promise<EntityMentionDTO[]>;
  }

  /**
   * Parse a streaming NDJSON response body into typed objects.
   * Uses ReadableStream reader + TextDecoder with streaming mode to handle
   * chunked transfers without buffering the entire body.
   */
  private async _parseNdjsonStream<T>(response: Response): Promise<T[]> {
    if (response.body == null) {
      return [];
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder('utf-8');
    const results: T[] = [];
    let buffer = '';

    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      // Retain the last (potentially incomplete) fragment for the next chunk.
      buffer = lines.pop() ?? '';

      for (const line of lines) {
        const trimmed = line.trim();
        if (trimmed) {
          results.push(JSON.parse(trimmed) as T);
        }
      }
    }

    // Flush any bytes remaining in the decoder after the stream closes.
    buffer += decoder.decode();
    if (buffer.trim()) {
      results.push(JSON.parse(buffer.trim()) as T);
    }

    return results;
  }
}

// ---------------------------------------------------------------------------
// Pure helper functions
// ---------------------------------------------------------------------------

/**
 * Format a single conversation turn as a Markdown note with YAML frontmatter.
 * Captures only the current turn (last user message + AI response), not the
 * full history, so notes grow linearly with conversations.
 */
export function formatConversationNote(
  userMessage: string,
  aiResponse: string,
  timestamp: Date,
  tags: string[] = ['agent', 'openclaw'],
): string {
  const iso = timestamp.toISOString();
  const dateStr = iso.split('T')[0] ?? iso;
  const tagList = tags.map((t) => t.trim()).filter(Boolean);

  return [
    '---',
    `date: ${dateStr}`,
    `timestamp: ${iso}`,
    'source: openclaw',
    `tags: [${tagList.join(', ')}]`,
    '---',
    '',
    '## User',
    '',
    userMessage,
    ...(aiResponse ? ['', '## Assistant', '', aiResponse] : []),
    '',
  ].join('\n');
}

/**
 * Format a multi-turn session as a Markdown note with YAML frontmatter.
 * Each turn gets its own section header with a timestamp.
 */
export function formatSessionNote(
  turns: SessionTurn[],
  timestamp: Date,
  tags: string[] = ['agent', 'openclaw'],
): string {
  const iso = timestamp.toISOString();
  const dateStr = iso.split('T')[0] ?? iso;
  const tagList = tags.map((t) => t.trim()).filter(Boolean);

  const parts = [
    '---',
    `date: ${dateStr}`,
    `timestamp: ${iso}`,
    'source: openclaw',
    `tags: [${tagList.join(', ')}]`,
    `turns: ${turns.length}`,
    '---',
  ];

  for (let i = 0; i < turns.length; i++) {
    const turn = turns[i]!;
    const turnIso = turn.timestamp.toISOString();
    parts.push('', `## Turn ${i + 1} — ${turnIso}`);
    parts.push('', '### User', '', turn.userMessage);
    if (turn.assistantMessage) {
      parts.push('', '### Assistant', '', turn.assistantMessage);
    }
  }

  parts.push('');
  return parts.join('\n');
}

/** Encode a UTF-8 string as Base64 (Node.js Buffer). */
export function encodeBase64(content: string): string {
  return Buffer.from(content, 'utf-8').toString('base64');
}

/**
 * Derive a stable idempotency key from the message content + timestamp.
 * Using SHA-256 of (userMessage + ISO timestamp) avoids silent duplicate
 * drops that minute-granularity keys cause for fast consecutive turns.
 */
export function hashTurnKey(userMessage: string, timestamp: Date): string {
  return createHash('sha256')
    .update(userMessage + timestamp.toISOString())
    .digest('hex')
    .slice(0, 32);
}

// Re-export for convenience so callers can import everything from this module.
export type { IngestResponse, MemoryUnitDTO };
