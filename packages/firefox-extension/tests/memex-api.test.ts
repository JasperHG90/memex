import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fetchVaults, saveNote } from '../src/lib/memex-api';

const mockFetch = vi.fn();
vi.stubGlobal('fetch', mockFetch);

beforeEach(() => {
  mockFetch.mockReset();
});

describe('fetchVaults', () => {
  it('parses NDJSON response into vault array', async () => {
    const ndjson = [
      '{"id":"aaa","name":"personal","description":null,"is_active":true,"note_count":5,"last_note_added_at":null}',
      '{"id":"bbb","name":"work","description":"Work notes","is_active":false,"note_count":2,"last_note_added_at":null}',
    ].join('\n');

    mockFetch.mockResolvedValueOnce({
      ok: true,
      text: () => Promise.resolve(ndjson),
    });

    const vaults = await fetchVaults('http://localhost:8000', '');
    expect(vaults).toHaveLength(2);
    expect(vaults[0].name).toBe('personal');
    expect(vaults[0].is_active).toBe(true);
    expect(vaults[1].name).toBe('work');
  });

  it('handles single vault', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      text: () =>
        Promise.resolve(
          '{"id":"ccc","name":"solo","description":null,"is_active":true,"note_count":0,"last_note_added_at":null}\n',
        ),
    });

    const vaults = await fetchVaults('http://localhost:8000', '');
    expect(vaults).toHaveLength(1);
    expect(vaults[0].id).toBe('ccc');
  });

  it('handles empty response', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      text: () => Promise.resolve(''),
    });

    const vaults = await fetchVaults('http://localhost:8000', '');
    expect(vaults).toHaveLength(0);
  });

  it('sends API key header when provided', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      text: () => Promise.resolve(''),
    });

    await fetchVaults('http://localhost:8000', 'my-secret-key');

    expect(mockFetch).toHaveBeenCalledWith('http://localhost:8000/api/v1/vaults', {
      headers: { 'X-API-Key': 'my-secret-key' },
    });
  });

  it('sends no API key header when empty', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      text: () => Promise.resolve(''),
    });

    await fetchVaults('http://localhost:8000', '');

    expect(mockFetch).toHaveBeenCalledWith('http://localhost:8000/api/v1/vaults', {
      headers: {},
    });
  });

  it('throws on non-ok response', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 500,
      statusText: 'Internal Server Error',
    });

    await expect(fetchVaults('http://localhost:8000', '')).rejects.toThrow(
      'Failed to fetch vaults: 500 Internal Server Error',
    );
  });
});

describe('saveNote', () => {
  it('sends correct request shape', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ note_id: 'note-123' }),
    });

    const result = await saveNote('http://localhost:8000', 'key123', {
      name: 'Test Note',
      description: 'A test',
      content: 'Hello world',
      tags: ['test', 'demo'],
      vaultId: 'vault-abc',
    });

    expect(result.note_id).toBe('note-123');

    const [url, opts] = mockFetch.mock.calls[0];
    expect(url).toBe('http://localhost:8000/api/v1/ingestions');
    expect(opts.method).toBe('POST');
    expect(opts.headers['Content-Type']).toBe('application/json');
    expect(opts.headers['X-API-Key']).toBe('key123');

    const body = JSON.parse(opts.body);
    expect(body.name).toBe('Test Note');
    expect(body.description).toBe('A test');
    expect(body.tags).toEqual(['test', 'demo']);
    expect(body.vault_id).toBe('vault-abc');
    // Content should be base64 encoded
    expect(atob(body.content)).toBe('Hello world');
  });

  it('encodes UTF-8 content correctly', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ note_id: 'note-456' }),
    });

    await saveNote('http://localhost:8000', '', {
      name: 'Unicode Note',
      description: '',
      content: 'Caf\u00e9 \u2014 na\u00efve',
      tags: [],
      vaultId: undefined,
    });

    const body = JSON.parse(mockFetch.mock.calls[0][1].body);
    // Decode the base64 and verify UTF-8
    const decoded = new TextDecoder().decode(
      Uint8Array.from(atob(body.content), (c) => c.charCodeAt(0)),
    );
    expect(decoded).toBe('Caf\u00e9 \u2014 na\u00efve');
  });

  it('throws on non-ok response', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 422,
      text: () => Promise.resolve('{"detail":"Validation error"}'),
    });

    await expect(
      saveNote('http://localhost:8000', '', {
        name: 'Bad Note',
        description: '',
        content: 'x',
        tags: [],
        vaultId: undefined,
      }),
    ).rejects.toThrow('Save failed: 422');
  });

  it('omits API key header when empty', async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ note_id: 'n' }),
    });

    await saveNote('http://localhost:8000', '', {
      name: 'N',
      description: '',
      content: 'x',
      tags: [],
      vaultId: undefined,
    });

    const headers = mockFetch.mock.calls[0][1].headers;
    expect(headers['X-API-Key']).toBeUndefined();
  });
});
