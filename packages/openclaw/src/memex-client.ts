import { createHash } from 'node:crypto';

import type {
  IngestResponse,
  MemorySearchRequest,
  MemorySummaryRequest,
  MemorySummaryResponse,
  MemoryUnitDTO,
  NoteCreateRequest,
  PluginConfig,
} from './types';

export class MemexClient {
  private readonly baseUrl: string;
  private readonly config: PluginConfig;

  constructor(config: PluginConfig) {
    this.config = config;
    this.baseUrl = config.serverUrl.replace(/\/$/, '');
  }

  /**
   * POST /api/v1/memories/search
   * Returns a stream of MemoryUnitDTO via NDJSON.
   */
  async searchMemories(query: string, signal?: AbortSignal): Promise<MemoryUnitDTO[]> {
    const request: MemorySearchRequest = {
      query,
      limit: this.config.searchLimit,
      skip_opinion_formation: true,
      ...(this.config.vaultId != null ? { vault_ids: [this.config.vaultId] } : {}),
    };

    const response = await fetch(`${this.baseUrl}/api/v1/memories/search`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
      signal,
    });

    if (!response.ok) {
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
    const body: NoteCreateRequest = {
      ...request,
      ...(this.config.vaultId != null ? { vault_id: this.config.vaultId } : {}),
    };

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
        console.warn(`[memex-openclaw] Background ingest failed: ${message}`);
      });
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
    '',
    '## Assistant',
    '',
    aiResponse,
    '',
  ].join('\n');
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
