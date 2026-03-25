import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fetchVaults, saveNote } from '../src/lib/memex-api';

/**
 * Mock browser.runtime.sendMessage — the API client now routes fetch calls
 * through the background script via proxyFetch messages.
 */
const mockSendMessage = vi.fn();
vi.stubGlobal('browser', { runtime: { sendMessage: mockSendMessage } });

beforeEach(() => {
  mockSendMessage.mockReset();
});

/** Helper: create a proxyFetch response. */
function proxyResp(ok: boolean, status: number, body: string, statusText = '') {
  return { ok, status, statusText, body };
}

describe('fetchVaults', () => {
  it('parses NDJSON response into vault array', async () => {
    const ndjson = [
      '{"id":"aaa","name":"personal","description":null,"is_active":true,"note_count":5,"last_note_added_at":null}',
      '{"id":"bbb","name":"work","description":"Work notes","is_active":false,"note_count":2,"last_note_added_at":null}',
    ].join('\n');

    mockSendMessage.mockResolvedValueOnce(proxyResp(true, 200, ndjson));

    const vaults = await fetchVaults('http://localhost:8000', '');
    expect(vaults).toHaveLength(2);
    expect(vaults[0].name).toBe('personal');
    expect(vaults[0].is_active).toBe(true);
    expect(vaults[1].name).toBe('work');
  });

  it('handles single vault', async () => {
    mockSendMessage.mockResolvedValueOnce(
      proxyResp(
        true,
        200,
        '{"id":"ccc","name":"solo","description":null,"is_active":true,"note_count":0,"last_note_added_at":null}\n',
      ),
    );

    const vaults = await fetchVaults('http://localhost:8000', '');
    expect(vaults).toHaveLength(1);
    expect(vaults[0].id).toBe('ccc');
  });

  it('handles empty response', async () => {
    mockSendMessage.mockResolvedValueOnce(proxyResp(true, 200, ''));

    const vaults = await fetchVaults('http://localhost:8000', '');
    expect(vaults).toHaveLength(0);
  });

  it('sends API key header when provided', async () => {
    mockSendMessage.mockResolvedValueOnce(proxyResp(true, 200, ''));

    await fetchVaults('http://localhost:8000', 'my-secret-key');

    expect(mockSendMessage).toHaveBeenCalledWith({
      action: 'proxyFetch',
      url: 'http://localhost:8000/api/v1/vaults',
      init: { headers: { 'X-API-Key': 'my-secret-key' } },
    });
  });

  it('sends no API key header when empty', async () => {
    mockSendMessage.mockResolvedValueOnce(proxyResp(true, 200, ''));

    await fetchVaults('http://localhost:8000', '');

    expect(mockSendMessage).toHaveBeenCalledWith({
      action: 'proxyFetch',
      url: 'http://localhost:8000/api/v1/vaults',
      init: { headers: {} },
    });
  });

  it('throws on non-ok response', async () => {
    mockSendMessage.mockResolvedValueOnce(
      proxyResp(false, 500, '', 'Internal Server Error'),
    );

    await expect(fetchVaults('http://localhost:8000', '')).rejects.toThrow(
      'Failed to fetch vaults: 500 Internal Server Error',
    );
  });
});

describe('saveNote', () => {
  it('sends correct request shape', async () => {
    mockSendMessage.mockResolvedValueOnce(
      proxyResp(true, 200, '{"note_id":"note-123"}'),
    );

    const result = await saveNote('http://localhost:8000', 'key123', {
      name: 'Test Note',
      description: 'A test',
      content: 'Hello world',
      tags: ['test', 'demo'],
      vaultId: 'vault-abc',
    });

    expect(result.note_id).toBe('note-123');

    const msg = mockSendMessage.mock.calls[0][0];
    expect(msg.action).toBe('proxyFetch');
    expect(msg.url).toBe('http://localhost:8000/api/v1/ingestions');
    expect(msg.init.method).toBe('POST');
    expect(msg.init.headers['Content-Type']).toBe('application/json');
    expect(msg.init.headers['X-API-Key']).toBe('key123');

    const body = JSON.parse(msg.init.body);
    expect(body.name).toBe('Test Note');
    expect(body.description).toBe('A test');
    expect(body.tags).toEqual(['test', 'demo']);
    expect(body.vault_id).toBe('vault-abc');
    // Content should be base64 encoded
    expect(atob(body.content)).toBe('Hello world');
  });

  it('encodes UTF-8 content correctly', async () => {
    mockSendMessage.mockResolvedValueOnce(
      proxyResp(true, 200, '{"note_id":"note-456"}'),
    );

    await saveNote('http://localhost:8000', '', {
      name: 'Unicode Note',
      description: '',
      content: 'Caf\u00e9 \u2014 na\u00efve',
      tags: [],
      vaultId: undefined,
    });

    const body = JSON.parse(mockSendMessage.mock.calls[0][0].init.body);
    // Decode the base64 and verify UTF-8
    const decoded = new TextDecoder().decode(
      Uint8Array.from(atob(body.content), (c) => c.charCodeAt(0)),
    );
    expect(decoded).toBe('Caf\u00e9 \u2014 na\u00efve');
  });

  it('throws on non-ok response', async () => {
    mockSendMessage.mockResolvedValueOnce(
      proxyResp(false, 422, '{"detail":"Validation error"}', 'Unprocessable Entity'),
    );

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
    mockSendMessage.mockResolvedValueOnce(
      proxyResp(true, 200, '{"note_id":"n"}'),
    );

    await saveNote('http://localhost:8000', '', {
      name: 'N',
      description: '',
      content: 'x',
      tags: [],
      vaultId: undefined,
    });

    const headers = mockSendMessage.mock.calls[0][0].init.headers;
    expect(headers['X-API-Key']).toBeUndefined();
  });

  it('sends files map when images are provided', async () => {
    mockSendMessage.mockResolvedValueOnce(
      proxyResp(true, 200, '{"note_id":"note-img"}'),
    );

    await saveNote('http://localhost:8000', '', {
      name: 'Article with Images',
      description: '',
      content: '# Article\n\n![](image-0.jpg)',
      tags: [],
      vaultId: 'vault-1',
      files: { 'image-0.jpg': 'BASE64DATA' },
    });

    const body = JSON.parse(mockSendMessage.mock.calls[0][0].init.body);
    expect(body.files).toEqual({ 'image-0.jpg': 'BASE64DATA' });
  });

  it('sends empty files map when no images provided', async () => {
    mockSendMessage.mockResolvedValueOnce(
      proxyResp(true, 200, '{"note_id":"note-no-img"}'),
    );

    await saveNote('http://localhost:8000', '', {
      name: 'Text Only',
      description: '',
      content: 'No images here',
      tags: [],
      vaultId: undefined,
    });

    const body = JSON.parse(mockSendMessage.mock.calls[0][0].init.body);
    expect(body.files).toEqual({});
  });
});
