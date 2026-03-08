import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

import { MemexClient, encodeBase64, formatConversationNote, formatSessionNote, hashTurnKey } from '../src/memex-client';
import { makeConfig, makeMemoryUnit, ndjsonResponse, jsonResponse, errorResponse, vaultOkResponse } from './helpers';

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
      fetchSpy.mockResolvedValueOnce(vaultOkResponse());
      fetchSpy.mockResolvedValueOnce(ndjsonResponse([]));

      await client.searchMemories('test query');

      expect(fetchSpy).toHaveBeenCalledTimes(2);
      const [url, init] = fetchSpy.mock.calls[1]!;
      expect(url).toBe('http://localhost:8000/api/v1/memories/search');
      expect(init.method).toBe('POST');
      const body = JSON.parse(init.body);
      expect(body.query).toBe('test query');
      expect(body.limit).toBe(5);
      expect(body.vault_ids).toEqual(['OpenClaw']);
    });

    it('includes vault_ids with vaultId when explicitly configured', async () => {
      const config = makeConfig({ vaultId: 'vault-42' });
      const client = new MemexClient(config);
      fetchSpy.mockResolvedValueOnce(vaultOkResponse());
      fetchSpy.mockResolvedValueOnce(ndjsonResponse([]));

      await client.searchMemories('query');

      const body = JSON.parse(fetchSpy.mock.calls[1]![1].body);
      expect(body.vault_ids).toEqual(['vault-42']);
    });

    it('uses vaultName as fallback when vaultId is null', async () => {
      const config = makeConfig({ vaultId: null, vaultName: 'MyVault' });
      const client = new MemexClient(config);
      fetchSpy.mockResolvedValueOnce(vaultOkResponse());
      fetchSpy.mockResolvedValueOnce(ndjsonResponse([]));

      await client.searchMemories('query');

      const body = JSON.parse(fetchSpy.mock.calls[1]![1].body);
      expect(body.vault_ids).toEqual(['MyVault']);
    });

    it('parses NDJSON stream into MemoryUnitDTO array', async () => {
      const m1 = makeMemoryUnit({ text: 'fact one' });
      const m2 = makeMemoryUnit({ text: 'fact two' });
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(vaultOkResponse());
      fetchSpy.mockResolvedValueOnce(ndjsonResponse([m1, m2]));

      const result = await client.searchMemories('query');

      expect(result).toHaveLength(2);
      expect(result[0]!.text).toBe('fact one');
      expect(result[1]!.text).toBe('fact two');
    });

    it('returns empty array on 404 (vault not found)', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(vaultOkResponse());
      fetchSpy.mockResolvedValueOnce(errorResponse(404, 'Not Found'));

      const result = await client.searchMemories('query');
      expect(result).toEqual([]);
    });

    it('throws on non-ok response other than 404', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(vaultOkResponse());
      fetchSpy.mockResolvedValueOnce(errorResponse(500, 'Internal Server Error'));

      await expect(client.searchMemories('query')).rejects.toThrow(
        /Memex search failed: 500/,
      );
    });

    it('passes abort signal to fetch', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(vaultOkResponse());
      fetchSpy.mockResolvedValueOnce(ndjsonResponse([]));
      const controller = new AbortController();

      await client.searchMemories('query', controller.signal);

      expect(fetchSpy.mock.calls[1]![1].signal).toBe(controller.signal);
    });

    it('omits token_budget from request body when null', async () => {
      const config = makeConfig({ tokenBudget: null });
      const client = new MemexClient(config);
      // First mock for ensureVault, second for the actual search
      fetchSpy.mockResolvedValueOnce(jsonResponse({ id: 'v1', name: 'OpenClaw' }));
      fetchSpy.mockResolvedValueOnce(ndjsonResponse([]));

      await client.searchMemories('query');

      // The search request is the second fetch call (after vault check)
      const body = JSON.parse(fetchSpy.mock.calls[1]![1].body);
      expect(body).not.toHaveProperty('token_budget');
    });

    it('includes token_budget in request body when set', async () => {
      const config = makeConfig({ tokenBudget: 3000 });
      const client = new MemexClient(config);
      // First mock for ensureVault, second for the actual search
      fetchSpy.mockResolvedValueOnce(jsonResponse({ id: 'v1', name: 'OpenClaw' }));
      fetchSpy.mockResolvedValueOnce(ndjsonResponse([]));

      await client.searchMemories('query');

      // The search request is the second fetch call (after vault check)
      const body = JSON.parse(fetchSpy.mock.calls[1]![1].body);
      expect(body.token_budget).toBe(3000);
    });

    it('returns empty array when response body is null', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(vaultOkResponse());
      const nullBodyResponse = new Response(null, { status: 200 });
      fetchSpy.mockResolvedValueOnce(nullBodyResponse);

      const result = await client.searchMemories('query');
      expect(result).toEqual([]);
    });

    it('strips trailing slash from serverUrl', async () => {
      const config = makeConfig({ serverUrl: 'http://localhost:8000/' });
      const client = new MemexClient(config);
      fetchSpy.mockResolvedValueOnce(vaultOkResponse());
      fetchSpy.mockResolvedValueOnce(ndjsonResponse([]));

      await client.searchMemories('query');

      expect(fetchSpy.mock.calls[1]![0]).toBe(
        'http://localhost:8000/api/v1/memories/search',
      );
    });

    it('uses overrides.limit instead of config.searchLimit', async () => {
      const config = makeConfig({ searchLimit: 8 });
      const client = new MemexClient(config);
      fetchSpy.mockResolvedValueOnce(vaultOkResponse());
      fetchSpy.mockResolvedValueOnce(ndjsonResponse([]));

      await client.searchMemories('query', undefined, { limit: 3 });

      const body = JSON.parse(fetchSpy.mock.calls[1]![1].body);
      expect(body.limit).toBe(3);
    });

    it('uses overrides.token_budget instead of config.tokenBudget', async () => {
      const config = makeConfig({ tokenBudget: null });
      const client = new MemexClient(config);
      fetchSpy.mockResolvedValueOnce(vaultOkResponse());
      fetchSpy.mockResolvedValueOnce(ndjsonResponse([]));

      await client.searchMemories('query', undefined, { token_budget: 5000 });

      const body = JSON.parse(fetchSpy.mock.calls[1]![1].body);
      expect(body.token_budget).toBe(5000);
    });

    it('does not mutate config when overrides are provided', async () => {
      const config = makeConfig({ searchLimit: 8, tokenBudget: null });
      const client = new MemexClient(config);
      fetchSpy.mockResolvedValueOnce(vaultOkResponse());
      fetchSpy.mockResolvedValueOnce(ndjsonResponse([]));

      await client.searchMemories('query', undefined, { limit: 3, token_budget: 5000 });

      expect(config.searchLimit).toBe(8);
      expect(config.tokenBudget).toBeNull();
    });

    it('falls back to config values when overrides are not provided', async () => {
      const config = makeConfig({ searchLimit: 12, tokenBudget: 2000 });
      const client = new MemexClient(config);
      fetchSpy.mockResolvedValueOnce(vaultOkResponse());
      fetchSpy.mockResolvedValueOnce(ndjsonResponse([]));

      await client.searchMemories('query', undefined, {});

      const body = JSON.parse(fetchSpy.mock.calls[1]![1].body);
      expect(body.limit).toBe(12);
      expect(body.token_budget).toBe(2000);
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
      fetchSpy.mockResolvedValueOnce(vaultOkResponse());
      fetchSpy.mockResolvedValueOnce(jsonResponse({ status: 'ok' }, 202));

      client.ingestNote({
        name: 'test',
        description: 'desc',
        content: 'base64content',
        tags: ['tag'],
      });

      // Allow microtask to settle (vault check + ingest)
      await vi.waitFor(() => {
        expect(fetchSpy).toHaveBeenCalledTimes(2);
      });

      const [url, init] = fetchSpy.mock.calls[1]!;
      expect(url).toBe('http://localhost:8000/api/v1/ingestions?background=true');
      expect(init.method).toBe('POST');
    });

    it('merges vault_id from config when vaultId is set', async () => {
      const config = makeConfig({ vaultId: 'v-99' });
      const client = new MemexClient(config);
      fetchSpy.mockResolvedValueOnce(vaultOkResponse());
      fetchSpy.mockResolvedValueOnce(jsonResponse({}, 202));

      client.ingestNote({
        name: 'test',
        description: 'desc',
        content: 'c',
      });

      await vi.waitFor(() => {
        expect(fetchSpy).toHaveBeenCalledTimes(2);
      });

      const body = JSON.parse(fetchSpy.mock.calls[1]![1].body);
      expect(body.vault_id).toBe('v-99');
    });

    it('uses vaultName as vault_id fallback when vaultId is null', async () => {
      const config = makeConfig({ vaultId: null, vaultName: 'MyVault' });
      const client = new MemexClient(config);
      fetchSpy.mockResolvedValueOnce(vaultOkResponse());
      fetchSpy.mockResolvedValueOnce(jsonResponse({}, 202));

      client.ingestNote({
        name: 'test',
        description: 'desc',
        content: 'c',
      });

      await vi.waitFor(() => {
        expect(fetchSpy).toHaveBeenCalledTimes(2);
      });

      const body = JSON.parse(fetchSpy.mock.calls[1]![1].body);
      expect(body.vault_id).toBe('MyVault');
    });

    it('logs warning on non-ok response', async () => {
      const client = new MemexClient(makeConfig());
      const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
      fetchSpy.mockResolvedValueOnce(vaultOkResponse());
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
      fetchSpy.mockResolvedValueOnce(vaultOkResponse());
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

    it('uses injected logger.warn when provided', async () => {
      const logger = { warn: vi.fn() };
      const client = new MemexClient(makeConfig(), logger);
      fetchSpy.mockResolvedValueOnce(vaultOkResponse());
      fetchSpy.mockRejectedValueOnce(new Error('fail'));

      client.ingestNote({ name: 'n', description: 'd', content: 'c' });

      await vi.waitFor(() => {
        expect(logger.warn).toHaveBeenCalledOnce();
      });

      expect(logger.warn.mock.calls[0]![0]).toMatch(/Background ingest failed/);
    });

    it('falls back to console.warn when no logger provided', async () => {
      const client = new MemexClient(makeConfig());
      const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
      fetchSpy.mockResolvedValueOnce(vaultOkResponse());
      fetchSpy.mockRejectedValueOnce(new Error('oops'));

      client.ingestNote({ name: 'n', description: 'd', content: 'c' });

      await vi.waitFor(() => {
        expect(warnSpy).toHaveBeenCalledOnce();
      });

      expect(warnSpy.mock.calls[0]![0]).toMatch(/Background ingest failed/);
    });
  });

  // -----------------------------------------------------------------------
  // searchNotes
  // -----------------------------------------------------------------------

  describe('searchNotes', () => {
    it('sends correct request body with defaults', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(jsonResponse([]));

      await client.searchNotes('find me');

      expect(fetchSpy).toHaveBeenCalledOnce();
      const [url, init] = fetchSpy.mock.calls[0]!;
      expect(url).toBe('http://localhost:8000/api/v1/notes/search');
      expect(init.method).toBe('POST');
      const body = JSON.parse(init.body);
      expect(body.query).toBe('find me');
      expect(body.limit).toBe(5);
      expect(body.expand_query).toBe(false);
      expect(body.summarize).toBe(false);
      expect(body).not.toHaveProperty('reason');
    });

    it('passes custom opts', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(jsonResponse([]));

      await client.searchNotes('q', { limit: 3, summarize: true });

      const body = JSON.parse(fetchSpy.mock.calls[0]![1].body);
      expect(body.limit).toBe(3);
      expect(body.summarize).toBe(true);
    });

    it('passes strategy and temporal filter opts', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(jsonResponse([]));

      await client.searchNotes('q', {
        strategies: ['semantic', 'keyword'],
        after: '2025-01-01',
        before: '2025-12-31',
        tags: ['project'],
        vault_ids: ['vault-1'],
      });

      const body = JSON.parse(fetchSpy.mock.calls[0]![1].body);
      expect(body.strategies).toEqual(['semantic', 'keyword']);
      expect(body.after).toBe('2025-01-01');
      expect(body.before).toBe('2025-12-31');
      expect(body.tags).toEqual(['project']);
      expect(body.vault_ids).toEqual(['vault-1']);
    });

    it('returns parsed JSON array', async () => {
      const results = [{ note_id: 'n1', snippets: [], score: 0.9 }];
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(jsonResponse(results));

      const res = await client.searchNotes('q');

      expect(res).toHaveLength(1);
      expect(res[0]!.note_id).toBe('n1');
    });

    it('returns empty array on 404 (vault not found)', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(errorResponse(404, 'Not Found'));

      const result = await client.searchNotes('q');
      expect(result).toEqual([]);
    });

    it('throws on non-ok response other than 404', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(errorResponse(500, 'Internal Server Error'));

      await expect(client.searchNotes('q')).rejects.toThrow(
        /Memex note search failed: 500/,
      );
    });

    it('passes abort signal', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(jsonResponse([]));
      const controller = new AbortController();

      await client.searchNotes('q', undefined, controller.signal);

      expect(fetchSpy.mock.calls[0]![1].signal).toBe(controller.signal);
    });
  });

  // -----------------------------------------------------------------------
  // getNote
  // -----------------------------------------------------------------------

  describe('getNote', () => {
    it('fetches correct URL', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(jsonResponse({ id: 'note-1', title: 'My Note' }));

      await client.getNote('note-1');

      expect(fetchSpy.mock.calls[0]![0]).toBe('http://localhost:8000/api/v1/notes/note-1');
    });

    it('returns parsed JSON', async () => {
      const client = new MemexClient(makeConfig());
      const data = { id: 'note-1', title: 'My Note' };
      fetchSpy.mockResolvedValueOnce(jsonResponse(data));

      const result = await client.getNote('note-1');

      expect(result).toEqual(data);
    });

    it('throws on non-ok response', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(errorResponse(404, 'Not Found'));

      await expect(client.getNote('bad-id')).rejects.toThrow(
        /Memex getNote failed: 404/,
      );
    });
  });

  // -----------------------------------------------------------------------
  // getPageIndex
  // -----------------------------------------------------------------------

  describe('getPageIndex', () => {
    it('fetches correct URL', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(jsonResponse({ toc: [] }));

      await client.getPageIndex('note-1');

      expect(fetchSpy.mock.calls[0]![0]).toBe(
        'http://localhost:8000/api/v1/notes/note-1/page-index',
      );
    });

    it('returns parsed JSON', async () => {
      const client = new MemexClient(makeConfig());
      const data = { toc: [{ id: 'n1', title: 'Intro', level: 1, seq: 0, children: [] }] };
      fetchSpy.mockResolvedValueOnce(jsonResponse(data));

      const result = await client.getPageIndex('note-1');

      expect(result.toc).toHaveLength(1);
      expect(result.toc[0]!.title).toBe('Intro');
    });

    it('throws on non-ok response', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(errorResponse(404, 'Not Found'));

      await expect(client.getPageIndex('bad-id')).rejects.toThrow(
        /Memex getPageIndex failed: 404/,
      );
    });
  });

  // -----------------------------------------------------------------------
  // getNode
  // -----------------------------------------------------------------------

  describe('getNode', () => {
    it('fetches correct URL', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(
        jsonResponse({ id: 'nd-1', note_id: 'n-1', title: 'Sec', text: 'body', level: 1, seq: 0 }),
      );

      await client.getNode('nd-1');

      expect(fetchSpy.mock.calls[0]![0]).toBe('http://localhost:8000/api/v1/nodes/nd-1');
    });

    it('returns parsed JSON', async () => {
      const client = new MemexClient(makeConfig());
      const data = { id: 'nd-1', note_id: 'n-1', title: 'Sec', text: 'body', level: 1, seq: 0 };
      fetchSpy.mockResolvedValueOnce(jsonResponse(data));

      const result = await client.getNode('nd-1');

      expect(result.id).toBe('nd-1');
      expect(result.text).toBe('body');
    });

    it('throws on non-ok response', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(errorResponse(404, 'Not Found'));

      await expect(client.getNode('bad-id')).rejects.toThrow(
        /Memex getNode failed: 404/,
      );
    });
  });

  // -----------------------------------------------------------------------
  // getLineage
  // -----------------------------------------------------------------------

  describe('getLineage', () => {
    it('uses default entity type memory_unit', async () => {
      const client = new MemexClient(makeConfig());
      const data = { entity_type: 'memory_unit', entity: {}, derived_from: [] };
      fetchSpy.mockResolvedValueOnce(jsonResponse(data));

      await client.getLineage('unit-1');

      expect(fetchSpy.mock.calls[0]![0]).toBe(
        'http://localhost:8000/api/v1/lineage/memory_unit/unit-1',
      );
    });

    it('uses custom entity type', async () => {
      const client = new MemexClient(makeConfig());
      const data = { entity_type: 'observation', entity: {}, derived_from: [] };
      fetchSpy.mockResolvedValueOnce(jsonResponse(data));

      await client.getLineage('obs-1', 'observation');

      expect(fetchSpy.mock.calls[0]![0]).toBe(
        'http://localhost:8000/api/v1/lineage/observation/obs-1',
      );
    });

    it('returns parsed JSON', async () => {
      const client = new MemexClient(makeConfig());
      const data = { entity_type: 'memory_unit', entity: { id: 'u1' }, derived_from: [] };
      fetchSpy.mockResolvedValueOnce(jsonResponse(data));

      const result = await client.getLineage('u1');

      expect(result.entity_type).toBe('memory_unit');
    });

    it('throws on non-ok response', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(errorResponse(404, 'Not Found'));

      await expect(client.getLineage('bad-id')).rejects.toThrow(
        /Memex getLineage failed: 404/,
      );
    });
  });

  // -----------------------------------------------------------------------
  // listEntities
  // -----------------------------------------------------------------------

  describe('listEntities', () => {
    it('sends query and limit as URL params', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(ndjsonResponse([]));

      await client.listEntities('python', 10);

      const url = fetchSpy.mock.calls[0]![0] as string;
      expect(url).toContain('query=python');
      expect(url).toContain('limit=10');
    });

    it('works without query param', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(ndjsonResponse([]));

      await client.listEntities();

      const url = fetchSpy.mock.calls[0]![0] as string;
      expect(url).not.toContain('query=');
      expect(url).toContain('limit=20');
    });

    it('returns parsed NDJSON entities', async () => {
      const client = new MemexClient(makeConfig());
      const entities = [{ id: 'e1', name: 'Python', entity_type: 'technology' }];
      fetchSpy.mockResolvedValueOnce(ndjsonResponse(entities));

      const result = await client.listEntities('Python');

      expect(result).toHaveLength(1);
      expect(result[0]!.name).toBe('Python');
    });

    it('throws on non-ok response', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(errorResponse(500, 'Internal Server Error'));

      await expect(client.listEntities()).rejects.toThrow(
        /Memex listEntities failed: 500/,
      );
    });
  });

  // -----------------------------------------------------------------------
  // getEntity
  // -----------------------------------------------------------------------

  describe('getEntity', () => {
    it('fetches correct URL', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(
        jsonResponse({ id: 'e1', name: 'Python', entity_type: 'tech' }),
      );

      await client.getEntity('e1');

      expect(fetchSpy.mock.calls[0]![0]).toBe('http://localhost:8000/api/v1/entities/e1');
    });

    it('returns parsed JSON', async () => {
      const client = new MemexClient(makeConfig());
      const data = { id: 'e1', name: 'Python', entity_type: 'tech' };
      fetchSpy.mockResolvedValueOnce(jsonResponse(data));

      const result = await client.getEntity('e1');

      expect(result.name).toBe('Python');
    });

    it('throws on non-ok response', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(errorResponse(404, 'Not Found'));

      await expect(client.getEntity('bad-id')).rejects.toThrow(
        /Memex getEntity failed: 404/,
      );
    });
  });

  // -----------------------------------------------------------------------
  // getEntityMentions
  // -----------------------------------------------------------------------

  describe('getEntityMentions', () => {
    it('sends limit as URL param', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(jsonResponse([]));

      await client.getEntityMentions('e1', 5);

      const url = fetchSpy.mock.calls[0]![0] as string;
      expect(url).toContain('limit=5');
      expect(url).toContain('/entities/e1/mentions');
    });

    it('returns parsed JSON array', async () => {
      const client = new MemexClient(makeConfig());
      const mentions = [{ id: 'm1', text: 'fact about Python', fact_type: 'observation' }];
      fetchSpy.mockResolvedValueOnce(jsonResponse(mentions));

      const result = await client.getEntityMentions('e1');

      expect(result).toHaveLength(1);
      expect(result[0]!.text).toBe('fact about Python');
    });

    it('throws on non-ok response', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(errorResponse(500, 'Internal Server Error'));

      await expect(client.getEntityMentions('e1')).rejects.toThrow(
        /Memex getEntityMentions failed: 500/,
      );
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
    fetchSpy.mockResolvedValueOnce(vaultOkResponse());
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
    fetchSpy.mockResolvedValueOnce(vaultOkResponse());
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
    fetchSpy.mockResolvedValueOnce(vaultOkResponse());
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

  it('omits Assistant section when aiResponse is empty', () => {
    const note = formatConversationNote('User question', '', ts);
    expect(note).toContain('## User');
    expect(note).toContain('User question');
    expect(note).not.toContain('## Assistant');
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

// ---------------------------------------------------------------------------
// formatSessionNote
// ---------------------------------------------------------------------------

describe('formatSessionNote', () => {
  const sessionTs = new Date('2025-06-15T14:00:00.000Z');

  it('produces valid YAML frontmatter with turns count', () => {
    const turns = [
      { userMessage: 'Hello', assistantMessage: 'Hi', timestamp: new Date('2025-06-15T14:00:00.000Z') },
    ];
    const note = formatSessionNote(turns, sessionTs);

    expect(note).toContain('---');
    expect(note).toContain('date: 2025-06-15');
    expect(note).toContain('timestamp: 2025-06-15T14:00:00.000Z');
    expect(note).toContain('source: openclaw');
    expect(note).toContain('tags: [agent, openclaw]');
    expect(note).toContain('turns: 1');
  });

  it('formats multiple turns with numbered headers', () => {
    const turns = [
      { userMessage: 'First question', assistantMessage: 'First answer', timestamp: new Date('2025-06-15T14:00:00.000Z') },
      { userMessage: 'Follow up', assistantMessage: 'More info', timestamp: new Date('2025-06-15T14:01:00.000Z') },
    ];
    const note = formatSessionNote(turns, sessionTs);

    expect(note).toContain('## Turn 1 — 2025-06-15T14:00:00.000Z');
    expect(note).toContain('### User');
    expect(note).toContain('First question');
    expect(note).toContain('### Assistant');
    expect(note).toContain('First answer');
    expect(note).toContain('## Turn 2 — 2025-06-15T14:01:00.000Z');
    expect(note).toContain('Follow up');
    expect(note).toContain('More info');
    expect(note).toContain('turns: 2');
  });

  it('omits Assistant section when assistantMessage is empty', () => {
    const turns = [
      { userMessage: 'Just a question', assistantMessage: '', timestamp: new Date('2025-06-15T14:00:00.000Z') },
    ];
    const note = formatSessionNote(turns, sessionTs);

    expect(note).toContain('### User');
    expect(note).toContain('Just a question');
    expect(note).not.toContain('### Assistant');
  });

  it('uses custom tags when provided', () => {
    const turns = [
      { userMessage: 'msg', assistantMessage: 'resp', timestamp: new Date('2025-06-15T14:00:00.000Z') },
    ];
    const note = formatSessionNote(turns, sessionTs, ['session', 'debug']);

    expect(note).toContain('tags: [session, debug]');
  });

  it('uses default tags when none provided', () => {
    const turns = [
      { userMessage: 'msg', assistantMessage: 'resp', timestamp: new Date('2025-06-15T14:00:00.000Z') },
    ];
    const note = formatSessionNote(turns, sessionTs);

    expect(note).toContain('tags: [agent, openclaw]');
  });

  it('handles empty turns array', () => {
    const note = formatSessionNote([], sessionTs);

    expect(note).toContain('turns: 0');
    expect(note).not.toContain('## Turn');
  });
});

// ---------------------------------------------------------------------------
// New client methods
// ---------------------------------------------------------------------------

describe('MemexClient new methods', () => {
  let fetchSpy: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  // -----------------------------------------------------------------------
  // searchMemories — new overrides
  // -----------------------------------------------------------------------

  describe('searchMemories with new overrides', () => {
    it('passes strategies to request body', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(vaultOkResponse());
      fetchSpy.mockResolvedValueOnce(ndjsonResponse([]));

      await client.searchMemories('q', undefined, {
        strategies: ['temporal', 'graph'],
      });

      const body = JSON.parse(fetchSpy.mock.calls[1]![1].body);
      expect(body.strategies).toEqual(['temporal', 'graph']);
    });

    it('passes include_superseded to request body', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(vaultOkResponse());
      fetchSpy.mockResolvedValueOnce(ndjsonResponse([]));

      await client.searchMemories('q', undefined, {
        include_superseded: true,
      });

      const body = JSON.parse(fetchSpy.mock.calls[1]![1].body);
      expect(body.include_superseded).toBe(true);
    });

    it('passes after/before temporal filters', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(vaultOkResponse());
      fetchSpy.mockResolvedValueOnce(ndjsonResponse([]));

      await client.searchMemories('q', undefined, {
        after: '2025-01-01',
        before: '2025-12-31',
      });

      const body = JSON.parse(fetchSpy.mock.calls[1]![1].body);
      expect(body.after).toBe('2025-01-01');
      expect(body.before).toBe('2025-12-31');
    });

    it('passes tags filter', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(vaultOkResponse());
      fetchSpy.mockResolvedValueOnce(ndjsonResponse([]));

      await client.searchMemories('q', undefined, { tags: ['project', 'dev'] });

      const body = JSON.parse(fetchSpy.mock.calls[1]![1].body);
      expect(body.tags).toEqual(['project', 'dev']);
    });

    it('omits optional overrides when not provided', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(vaultOkResponse());
      fetchSpy.mockResolvedValueOnce(ndjsonResponse([]));

      await client.searchMemories('q', undefined, {});

      const body = JSON.parse(fetchSpy.mock.calls[1]![1].body);
      expect(body).not.toHaveProperty('strategies');
      expect(body).not.toHaveProperty('include_superseded');
      expect(body).not.toHaveProperty('after');
      expect(body).not.toHaveProperty('before');
      expect(body).not.toHaveProperty('tags');
    });
  });

  // -----------------------------------------------------------------------
  // getMemoryUnit
  // -----------------------------------------------------------------------

  describe('getMemoryUnit', () => {
    it('fetches correct URL', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(jsonResponse(makeMemoryUnit()));

      await client.getMemoryUnit('unit-1');

      expect(fetchSpy.mock.calls[0]![0]).toBe('http://localhost:8000/api/v1/memories/unit-1');
    });

    it('returns parsed JSON', async () => {
      const client = new MemexClient(makeConfig());
      const unit = makeMemoryUnit({ text: 'specific fact' });
      fetchSpy.mockResolvedValueOnce(jsonResponse(unit));

      const result = await client.getMemoryUnit('u1');
      expect(result.text).toBe('specific fact');
    });

    it('throws on non-ok response', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(errorResponse(404, 'Not Found'));

      await expect(client.getMemoryUnit('bad')).rejects.toThrow(/Memex getMemoryUnit failed: 404/);
    });
  });

  // -----------------------------------------------------------------------
  // getMemoryUnits (batch)
  // -----------------------------------------------------------------------

  describe('getMemoryUnits', () => {
    it('sends POST with unit_ids', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(ndjsonResponse([]));

      await client.getMemoryUnits(['u1', 'u2']);

      const [url, init] = fetchSpy.mock.calls[0]!;
      expect(url).toBe('http://localhost:8000/api/v1/memories/batch');
      expect(init.method).toBe('POST');
      const body = JSON.parse(init.body);
      expect(body.unit_ids).toEqual(['u1', 'u2']);
    });

    it('parses NDJSON stream', async () => {
      const client = new MemexClient(makeConfig());
      const m1 = makeMemoryUnit({ text: 'batch-1' });
      const m2 = makeMemoryUnit({ text: 'batch-2' });
      fetchSpy.mockResolvedValueOnce(ndjsonResponse([m1, m2]));

      const result = await client.getMemoryUnits(['u1', 'u2']);
      expect(result).toHaveLength(2);
    });
  });

  // -----------------------------------------------------------------------
  // setNoteStatus
  // -----------------------------------------------------------------------

  describe('setNoteStatus', () => {
    it('sends PATCH with status', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(jsonResponse({ status: 'success' }));

      await client.setNoteStatus('note-1', 'superseded', 'note-2');

      const [url, init] = fetchSpy.mock.calls[0]!;
      expect(url).toBe('http://localhost:8000/api/v1/notes/note-1/status');
      expect(init.method).toBe('PATCH');
      const body = JSON.parse(init.body);
      expect(body.status).toBe('superseded');
      expect(body.linked_note_id).toBe('note-2');
    });

    it('throws on non-ok response', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(errorResponse(404, 'Not Found'));

      await expect(client.setNoteStatus('bad', 'active')).rejects.toThrow(
        /Memex setNoteStatus failed: 404/,
      );
    });
  });

  // -----------------------------------------------------------------------
  // renameNote
  // -----------------------------------------------------------------------

  describe('renameNote', () => {
    it('sends PATCH with new_title', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(jsonResponse({ status: 'success' }));

      await client.renameNote('note-1', 'New Title');

      const [url, init] = fetchSpy.mock.calls[0]!;
      expect(url).toBe('http://localhost:8000/api/v1/notes/note-1/title');
      expect(init.method).toBe('PATCH');
      const body = JSON.parse(init.body);
      expect(body.new_title).toBe('New Title');
    });
  });

  // -----------------------------------------------------------------------
  // getEntityCooccurrences
  // -----------------------------------------------------------------------

  describe('getEntityCooccurrences', () => {
    it('fetches correct URL and parses NDJSON', async () => {
      const client = new MemexClient(makeConfig());
      const coocs = [{ entity_id: 'e2', name: 'React', cooccurrence_count: 5 }];
      fetchSpy.mockResolvedValueOnce(ndjsonResponse(coocs));

      const result = await client.getEntityCooccurrences('e1');

      expect(fetchSpy.mock.calls[0]![0]).toBe(
        'http://localhost:8000/api/v1/entities/e1/cooccurrences',
      );
      expect(result).toHaveLength(1);
      expect(result[0]!.name).toBe('React');
    });

    it('throws on non-ok response', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(errorResponse(500, 'Error'));

      await expect(client.getEntityCooccurrences('e1')).rejects.toThrow(
        /Memex getEntityCooccurrences failed/,
      );
    });
  });

  // -----------------------------------------------------------------------
  // listEntities with vaultId
  // -----------------------------------------------------------------------

  describe('listEntities with vaultId', () => {
    it('passes vault_id query param', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(ndjsonResponse([]));

      await client.listEntities('python', 10, undefined, 'vault-42');

      const url = fetchSpy.mock.calls[0]![0] as string;
      expect(url).toContain('vault_id=vault-42');
    });
  });

  // -----------------------------------------------------------------------
  // getPageIndex with depth/parent_node_id
  // -----------------------------------------------------------------------

  describe('getPageIndex with opts', () => {
    it('passes depth and parent_node_id as query params', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(jsonResponse({ toc: [] }));

      await client.getPageIndex('note-1', { depth: 0, parent_node_id: 'node-1' });

      const url = fetchSpy.mock.calls[0]![0] as string;
      expect(url).toContain('depth=0');
      expect(url).toContain('parent_node_id=node-1');
    });

    it('omits params when not provided', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(jsonResponse({ toc: [] }));

      await client.getPageIndex('note-1');

      const url = fetchSpy.mock.calls[0]![0] as string;
      expect(url).toBe('http://localhost:8000/api/v1/notes/note-1/page-index');
    });
  });

  // -----------------------------------------------------------------------
  // reflect
  // -----------------------------------------------------------------------

  describe('reflect', () => {
    it('sends POST with entity_id', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(jsonResponse({ status: 'queued', entity_id: 'e1' }));

      const result = await client.reflect('e1', 30, 'vault-1');

      const [url, init] = fetchSpy.mock.calls[0]!;
      expect(url).toBe('http://localhost:8000/api/v1/reflections');
      expect(init.method).toBe('POST');
      const body = JSON.parse(init.body);
      expect(body.entity_id).toBe('e1');
      expect(body.limit_recent_memories).toBe(30);
      expect(body.vault_id).toBe('vault-1');
      expect(result.status).toBe('queued');
    });
  });

  // -----------------------------------------------------------------------
  // listVaults
  // -----------------------------------------------------------------------

  describe('listVaults', () => {
    it('fetches correct URL and parses NDJSON', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(ndjsonResponse([{ id: 'v1', name: 'Default' }]));

      const result = await client.listVaults();

      expect(fetchSpy.mock.calls[0]![0]).toBe('http://localhost:8000/api/v1/vaults');
      expect(result).toHaveLength(1);
      expect(result[0]!.name).toBe('Default');
    });
  });

  // -----------------------------------------------------------------------
  // getActiveVault
  // -----------------------------------------------------------------------

  describe('getActiveVault', () => {
    it('returns first vault with is_default=true', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(ndjsonResponse([{ id: 'v1', name: 'Default' }]));

      const result = await client.getActiveVault();

      const url = fetchSpy.mock.calls[0]![0] as string;
      expect(url).toContain('is_default=true');
      expect(result.name).toBe('Default');
    });

    it('throws when no vaults found', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(ndjsonResponse([]));

      await expect(client.getActiveVault()).rejects.toThrow(/No active vault found/);
    });
  });

  // -----------------------------------------------------------------------
  // listNotes
  // -----------------------------------------------------------------------

  describe('listNotes', () => {
    it('sends limit, offset, and vault_id params', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(ndjsonResponse([]));

      await client.listNotes(10, 5, 'vault-1');

      const url = fetchSpy.mock.calls[0]![0] as string;
      expect(url).toContain('limit=10');
      expect(url).toContain('offset=5');
      expect(url).toContain('vault_id=vault-1');
    });
  });

  // -----------------------------------------------------------------------
  // migrateNote
  // -----------------------------------------------------------------------

  describe('migrateNote', () => {
    it('sends POST with target_vault_id', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(jsonResponse({ status: 'success' }));

      await client.migrateNote('note-1', 'vault-2');

      const [url, init] = fetchSpy.mock.calls[0]!;
      expect(url).toBe('http://localhost:8000/api/v1/notes/note-1/migrate');
      expect(init.method).toBe('POST');
      const body = JSON.parse(init.body);
      expect(body.target_vault_id).toBe('vault-2');
    });
  });

  // -----------------------------------------------------------------------
  // ingestUrl
  // -----------------------------------------------------------------------

  describe('ingestUrl', () => {
    it('sends POST with url and background param', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(jsonResponse({ status: 'queued' }));

      await client.ingestUrl('https://example.com', 'vault-1', true);

      const [url, init] = fetchSpy.mock.calls[0]!;
      expect(url).toBe('http://localhost:8000/api/v1/ingestions/url?background=true');
      expect(init.method).toBe('POST');
      const body = JSON.parse(init.body);
      expect(body.url).toBe('https://example.com');
      expect(body.vault_id).toBe('vault-1');
    });

    it('omits background query param when false', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(jsonResponse({ status: 'ok' }));

      await client.ingestUrl('https://example.com', undefined, false);

      const url = fetchSpy.mock.calls[0]![0] as string;
      expect(url).toBe('http://localhost:8000/api/v1/ingestions/url');
    });
  });

  // -----------------------------------------------------------------------
  // addNote
  // -----------------------------------------------------------------------

  describe('addNote', () => {
    it('sends POST with full note data', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(vaultOkResponse());
      fetchSpy.mockResolvedValueOnce(jsonResponse({
        status: 'ok',
        document_id: 'doc-1',
        unit_ids: ['u1'],
        overlapping_notes: [{ note_id: 'n2', title: 'Similar', similarity: 0.85 }],
      }));

      const result = await client.addNote({
        name: 'Test',
        description: 'A test note',
        content: 'base64data',
        tags: ['test'],
        author: 'openclaw',
      });

      expect(result.document_id).toBe('doc-1');
      expect(result.overlapping_notes).toHaveLength(1);
    });

    it('throws on non-ok response', async () => {
      const client = new MemexClient(makeConfig());
      fetchSpy.mockResolvedValueOnce(vaultOkResponse());
      fetchSpy.mockResolvedValueOnce(errorResponse(400, 'Bad Request'));

      await expect(
        client.addNote({ name: 'test', description: 'd', content: 'c', tags: [] }),
      ).rejects.toThrow(/Memex addNote failed: 400/);
    });
  });
});
