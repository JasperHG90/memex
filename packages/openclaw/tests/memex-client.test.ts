import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

import { MemexClient, encodeBase64, formatConversationNote, hashTurnKey } from '../src/memex-client';
import { makeConfig, makeMemoryUnit, ndjsonResponse, jsonResponse, errorResponse } from './helpers';

// ---------------------------------------------------------------------------
// MemexClient
// ---------------------------------------------------------------------------

describe('MemexClient', () => {
  let fetchSpy: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  // -----------------------------------------------------------------------
  // searchMemories
  // -----------------------------------------------------------------------

  describe('searchMemories', () => {
    it('sends correct request body', async () => {
      const config = makeConfig({ searchLimit: 5 });
      const client = new MemexClient(config);
      fetchSpy.mockResolvedValueOnce(ndjsonResponse([]));

      await client.searchMemories('test query');

      expect(fetchSpy).toHaveBeenCalledOnce();
      const [url, init] = fetchSpy.mock.calls[0]!;
      expect(url).toBe('http://localhost:8000/api/v1/memories/search');
      expect(init.method).toBe('POST');
      const body = JSON.parse(init.body);
      expect(body.query).toBe('test query');
      expect(body.limit).toBe(5);
      expect(body.skip_opinion_formation).toBe(true);
      expect(body.vault_ids).toBeUndefined();
    });

    it('includes vault_ids when vaultId is configured', async () => {
      const config = makeConfig({ vaultId: 'vault-42' });
      const client = new MemexClient(config);
      fetchSpy.mockResolvedValueOnce(ndjsonResponse([]));

      await client.searchMemories('query');

      const body = JSON.parse(fetchSpy.mock.calls[0]![1].body);
      expect(body.vault_ids).toEqual(['vault-42']);
    });

    it('parses NDJSON stream into MemoryUnitDTO array', async () => {
      const m1 = makeMemoryUnit({ text: 'fact one' });
      const m2 = makeMemoryUnit({ text: 'fact two' });
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(ndjsonResponse([m1, m2]));

      const result = await client.searchMemories('query');

      expect(result).toHaveLength(2);
      expect(result[0]!.text).toBe('fact one');
      expect(result[1]!.text).toBe('fact two');
    });

    it('throws on non-ok response', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(errorResponse(500, 'Internal Server Error'));

      await expect(client.searchMemories('query')).rejects.toThrow(
        /Memex search failed: 500/,
      );
    });

    it('passes abort signal to fetch', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(ndjsonResponse([]));
      const controller = new AbortController();

      await client.searchMemories('query', controller.signal);

      expect(fetchSpy.mock.calls[0]![1].signal).toBe(controller.signal);
    });

    it('returns empty array when response body is null', async () => {
      const client = new MemexClient(makeConfig());
      const nullBodyResponse = new Response(null, { status: 200 });
      fetchSpy.mockResolvedValueOnce(nullBodyResponse);

      const result = await client.searchMemories('query');
      expect(result).toEqual([]);
    });

    it('strips trailing slash from serverUrl', async () => {
      const config = makeConfig({ serverUrl: 'http://localhost:8000/' });
      const client = new MemexClient(config);
      fetchSpy.mockResolvedValueOnce(ndjsonResponse([]));

      await client.searchMemories('query');

      expect(fetchSpy.mock.calls[0]![0]).toBe(
        'http://localhost:8000/api/v1/memories/search',
      );
    });
  });

  // -----------------------------------------------------------------------
  // summarizeMemories
  // -----------------------------------------------------------------------

  describe('summarizeMemories', () => {
    it('sends correct request body', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(jsonResponse({ summary: 'The summary' }));

      await client.summarizeMemories('query', ['text1', 'text2']);

      const [url, init] = fetchSpy.mock.calls[0]!;
      expect(url).toBe('http://localhost:8000/api/v1/memories/summary');
      const body = JSON.parse(init.body);
      expect(body.query).toBe('query');
      expect(body.texts).toEqual(['text1', 'text2']);
    });

    it('returns parsed JSON response', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(jsonResponse({ summary: 'Sum it up' }));

      const result = await client.summarizeMemories('q', ['t']);

      expect(result.summary).toBe('Sum it up');
    });

    it('throws on non-ok response', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(errorResponse(503, 'Service Unavailable'));

      await expect(client.summarizeMemories('q', ['t'])).rejects.toThrow(
        /Memex summary failed: 503/,
      );
    });

    it('passes abort signal to fetch', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(jsonResponse({ summary: 'ok' }));
      const controller = new AbortController();

      await client.summarizeMemories('q', ['t'], controller.signal);

      expect(fetchSpy.mock.calls[0]![1].signal).toBe(controller.signal);
    });
  });

  // -----------------------------------------------------------------------
  // ingestNote
  // -----------------------------------------------------------------------

  describe('ingestNote', () => {
    it('sends fire-and-forget POST request', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(jsonResponse({ status: 'ok' }, 202));

      client.ingestNote({
        name: 'test',
        description: 'desc',
        content: 'base64content',
        tags: ['tag'],
      });

      // Allow microtask to settle
      await vi.waitFor(() => {
        expect(fetchSpy).toHaveBeenCalledOnce();
      });

      const [url, init] = fetchSpy.mock.calls[0]!;
      expect(url).toBe('http://localhost:8000/api/v1/ingestions?background=true');
      expect(init.method).toBe('POST');
    });

    it('merges vault_id from config', async () => {
      const config = makeConfig({ vaultId: 'v-99' });
      const client = new MemexClient(config);
      fetchSpy.mockResolvedValueOnce(jsonResponse({}, 202));

      client.ingestNote({
        name: 'test',
        description: 'desc',
        content: 'c',
      });

      await vi.waitFor(() => {
        expect(fetchSpy).toHaveBeenCalledOnce();
      });

      const body = JSON.parse(fetchSpy.mock.calls[0]![1].body);
      expect(body.vault_id).toBe('v-99');
    });

    it('logs warning on non-ok response', async () => {
      const client = new MemexClient(makeConfig());
      const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
      fetchSpy.mockResolvedValueOnce(errorResponse(500, 'boom'));

      client.ingestNote({
        name: 'test',
        description: 'desc',
        content: 'c',
      });

      await vi.waitFor(() => {
        expect(warnSpy).toHaveBeenCalledOnce();
      });

      expect(warnSpy.mock.calls[0]![0]).toMatch(/Background ingest failed/);
    });

    it('logs warning on network error', async () => {
      const client = new MemexClient(makeConfig());
      const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
      fetchSpy.mockRejectedValueOnce(new Error('Network down'));

      client.ingestNote({
        name: 'test',
        description: 'desc',
        content: 'c',
      });

      await vi.waitFor(() => {
        expect(warnSpy).toHaveBeenCalledOnce();
      });

      expect(warnSpy.mock.calls[0]![0]).toMatch(/Network down/);
    });
  });
});

// ---------------------------------------------------------------------------
// NDJSON parsing (via searchMemories — exercises _parseNdjsonStream)
// ---------------------------------------------------------------------------

describe('NDJSON parsing', () => {
  let fetchSpy: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('parses a single NDJSON object', async () => {
    const client = new MemexClient(makeConfig());
    const m = makeMemoryUnit({ text: 'solo' });
    fetchSpy.mockResolvedValueOnce(ndjsonResponse([m]));

    const result = await client.searchMemories('q');
    expect(result).toHaveLength(1);
    expect(result[0]!.text).toBe('solo');
  });

  it('handles empty lines between objects', async () => {
    const client = new MemexClient(makeConfig());
    const m1 = makeMemoryUnit({ text: 'a' });
    const m2 = makeMemoryUnit({ text: 'b' });
    // Build response with extra blank lines
    const ndjson = `${JSON.stringify(m1)}\n\n\n${JSON.stringify(m2)}\n`;
    const encoder = new TextEncoder();
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode(ndjson));
        controller.close();
      },
    });
    fetchSpy.mockResolvedValueOnce(
      new Response(stream, { status: 200 }),
    );

    const result = await client.searchMemories('q');
    expect(result).toHaveLength(2);
  });

  it('handles chunked delivery', async () => {
    const client = new MemexClient(makeConfig());
    const m1 = makeMemoryUnit({ text: 'chunked-1' });
    const m2 = makeMemoryUnit({ text: 'chunked-2' });
    const full = `${JSON.stringify(m1)}\n${JSON.stringify(m2)}\n`;
    const encoder = new TextEncoder();
    const mid = Math.floor(full.length / 2);

    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        // Deliver in two chunks, split mid-line
        controller.enqueue(encoder.encode(full.slice(0, mid)));
        controller.enqueue(encoder.encode(full.slice(mid)));
        controller.close();
      },
    });
    fetchSpy.mockResolvedValueOnce(
      new Response(stream, { status: 200 }),
    );

    const result = await client.searchMemories('q');
    expect(result).toHaveLength(2);
    expect(result[0]!.text).toBe('chunked-1');
    expect(result[1]!.text).toBe('chunked-2');
  });
});

// ---------------------------------------------------------------------------
// formatConversationNote
// ---------------------------------------------------------------------------

describe('formatConversationNote', () => {
  const ts = new Date('2025-06-15T12:30:00.000Z');

  it('produces valid YAML frontmatter', () => {
    const note = formatConversationNote('Hello', 'Hi there', ts);
    expect(note).toContain('---');
    expect(note).toContain('date: 2025-06-15');
    expect(note).toContain('timestamp: 2025-06-15T12:30:00.000Z');
    expect(note).toContain('source: openclaw');
  });

  it('uses default tags when none provided', () => {
    const note = formatConversationNote('msg', 'resp', ts);
    expect(note).toContain('tags: [agent, openclaw]');
  });

  it('uses custom tags when provided', () => {
    const note = formatConversationNote('msg', 'resp', ts, ['custom', 'test']);
    expect(note).toContain('tags: [custom, test]');
  });

  it('includes user message and AI response sections', () => {
    const note = formatConversationNote('User says this', 'AI responds with that', ts);
    expect(note).toContain('## User');
    expect(note).toContain('User says this');
    expect(note).toContain('## Assistant');
    expect(note).toContain('AI responds with that');
  });

  it('extracts date correctly from ISO timestamp', () => {
    const midnight = new Date('2024-01-01T00:00:00.000Z');
    const note = formatConversationNote('a', 'b', midnight);
    expect(note).toContain('date: 2024-01-01');
  });

  it('filters empty tags', () => {
    const note = formatConversationNote('msg', 'resp', ts, ['valid', '', ' ']);
    expect(note).toContain('tags: [valid]');
  });
});

// ---------------------------------------------------------------------------
// encodeBase64
// ---------------------------------------------------------------------------

describe('encodeBase64', () => {
  it('encodes ASCII string', () => {
    expect(encodeBase64('hello')).toBe(Buffer.from('hello').toString('base64'));
  });

  it('encodes UTF-8 string', () => {
    const emoji = 'Hello 🌍';
    expect(encodeBase64(emoji)).toBe(Buffer.from(emoji, 'utf-8').toString('base64'));
  });

  it('returns empty string for empty input', () => {
    expect(encodeBase64('')).toBe('');
  });
});

// ---------------------------------------------------------------------------
// hashTurnKey
// ---------------------------------------------------------------------------

describe('hashTurnKey', () => {
  const ts = new Date('2025-06-15T12:00:00.000Z');

  it('returns deterministic output for same input', () => {
    const a = hashTurnKey('hello', ts);
    const b = hashTurnKey('hello', ts);
    expect(a).toBe(b);
  });

  it('returns 32-char hex string', () => {
    const key = hashTurnKey('test', ts);
    expect(key).toHaveLength(32);
    expect(key).toMatch(/^[0-9a-f]{32}$/);
  });

  it('produces different keys for different messages', () => {
    const a = hashTurnKey('message A', ts);
    const b = hashTurnKey('message B', ts);
    expect(a).not.toBe(b);
  });

  it('produces different keys for different timestamps', () => {
    const ts2 = new Date('2025-06-15T12:00:01.000Z');
    const a = hashTurnKey('same message', ts);
    const b = hashTurnKey('same message', ts2);
    expect(a).not.toBe(b);
  });
});
