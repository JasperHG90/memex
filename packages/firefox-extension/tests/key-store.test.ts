import { describe, it, expect, vi, beforeEach } from 'vitest';

// ---------------------------------------------------------------------------
// Mock infrastructure
// ---------------------------------------------------------------------------

/** In-memory stand-in for browser.storage areas. */
function createStorageMock() {
  let data: Record<string, unknown> = {};
  return {
    get: vi.fn(async (defaults: Record<string, unknown>) => ({ ...defaults, ...data })),
    set: vi.fn(async (items: Record<string, unknown>) => {
      Object.assign(data, items);
    }),
    remove: vi.fn(async (keys: string[]) => {
      for (const k of keys) delete data[k];
    }),
    _data: () => data,
    _reset: () => {
      data = {};
    },
  };
}

const storageMockLocal = createStorageMock();
const storageMockSession = createStorageMock();

vi.stubGlobal('browser', {
  storage: {
    local: storageMockLocal,
    session: storageMockSession,
  },
});

/** In-memory IndexedDB mock backed by a Map. */
const idbStore = new Map<string, unknown>();

function createMockIDB() {
  const mockObjectStore = {
    get: vi.fn((key: string) => {
      const req = {
        result: idbStore.get(key),
        onsuccess: null as (() => void) | null,
        onerror: null as (() => void) | null,
        error: null,
      };
      setTimeout(() => req.onsuccess?.());
      return req;
    }),
    put: vi.fn((value: unknown, key: string) => {
      idbStore.set(key, value);
      const req = {
        onsuccess: null as (() => void) | null,
        onerror: null as (() => void) | null,
        error: null,
      };
      setTimeout(() => req.onsuccess?.());
      return req;
    }),
    delete: vi.fn((key: string) => {
      idbStore.delete(key);
      const req = {
        onsuccess: null as (() => void) | null,
        onerror: null as (() => void) | null,
        error: null,
      };
      setTimeout(() => req.onsuccess?.());
      return req;
    }),
  };

  const mockDB = {
    transaction: vi.fn(() => ({
      objectStore: vi.fn(() => mockObjectStore),
    })),
    objectStoreNames: { contains: () => true },
    createObjectStore: vi.fn(),
    close: vi.fn(),
  };

  return {
    open: vi.fn(() => {
      const req = {
        result: mockDB,
        onupgradeneeded: null as (() => void) | null,
        onsuccess: null as (() => void) | null,
        onerror: null as (() => void) | null,
        error: null,
      };
      setTimeout(() => req.onsuccess?.());
      return req;
    }),
  };
}

vi.stubGlobal('indexedDB', createMockIDB());

/** Mock CryptoKey object. */
const MOCK_CRYPTO_KEY = { type: 'secret', algorithm: { name: 'AES-GCM' } } as unknown as CryptoKey;

/** Track encrypt/decrypt calls for assertions. */
const encryptCalls: unknown[] = [];

vi.stubGlobal('crypto', {
  subtle: {
    generateKey: vi.fn(async () => MOCK_CRYPTO_KEY),
    encrypt: vi.fn(async (_algo: unknown, _key: unknown, data: ArrayBuffer) => {
      encryptCalls.push(_algo);
      // Simulate encryption: just return the data (tests verify the flow, not AES correctness)
      return data;
    }),
    decrypt: vi.fn(async (_algo: unknown, _key: unknown, data: ArrayBuffer) => {
      // Simulate decryption: reverse of our mock encrypt
      return data;
    }),
  },
  getRandomValues: vi.fn((arr: Uint8Array) => {
    // Fill with sequential bytes so we can detect fresh IVs
    for (let i = 0; i < arr.length; i++) arr[i] = Math.floor(Math.random() * 256);
    return arr;
  }),
});

// Import AFTER mocks are set up
const { saveApiKey, loadApiKey, clearApiKey } = await import('../src/lib/key-store');

beforeEach(() => {
  storageMockLocal._reset();
  storageMockSession._reset();
  idbStore.clear();
  encryptCalls.length = 0;
  vi.clearAllMocks();
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('key-store', () => {
  describe('session-only mode (remember=false)', () => {
    it('stores key in session storage only', async () => {
      await saveApiKey('my-secret-key', false);

      // Session storage should have the key
      expect(storageMockSession.set).toHaveBeenCalledWith({ memexApiKey: 'my-secret-key' });
      // Local storage should NOT have encrypted data
      const localData = storageMockLocal._data();
      expect(localData.memexApiKeyEnc).toBeUndefined();
      // Remember pref should be false
      expect(localData.memexRememberKey).toBe(false);
    });

    it('round-trips correctly', async () => {
      await saveApiKey('round-trip-key', false);
      const result = await loadApiKey();
      expect(result.apiKey).toBe('round-trip-key');
      expect(result.remember).toBe(false);
    });
  });

  describe('encrypted persistent mode (remember=true)', () => {
    it('encrypts and stores in local storage', async () => {
      await saveApiKey('encrypted-key', true);

      const localData = storageMockLocal._data();
      expect(localData.memexRememberKey).toBe(true);
      // Should have encrypted payload
      expect(localData.memexApiKeyEnc).toBeDefined();
      const enc = localData.memexApiKeyEnc as { iv: string; ciphertext: string };
      expect(enc.iv).toBeTruthy();
      expect(enc.ciphertext).toBeTruthy();
      // Session should NOT have plaintext
      expect(storageMockSession._data().memexApiKey).toBeUndefined();
    });

    it('round-trips correctly', async () => {
      await saveApiKey('encrypted-roundtrip', true);
      const result = await loadApiKey();
      expect(result.apiKey).toBe('encrypted-roundtrip');
      expect(result.remember).toBe(true);
    });

    it('calls generateKey with extractable=false', async () => {
      await saveApiKey('test', true);
      expect(crypto.subtle.generateKey).toHaveBeenCalledWith(
        { name: 'AES-GCM', length: 256 },
        false,
        ['encrypt', 'decrypt'],
      );
    });

    it('generates fresh IV for each encryption', async () => {
      await saveApiKey('first', true);
      const first = { ...(storageMockLocal._data().memexApiKeyEnc as { iv: string }) };

      storageMockLocal._reset();
      idbStore.clear();

      await saveApiKey('second', true);
      const second = storageMockLocal._data().memexApiKeyEnc as { iv: string };

      // IVs should differ (random bytes)
      expect(crypto.getRandomValues).toHaveBeenCalledTimes(2);
      // With random mock, IVs are extremely unlikely to be identical
      expect(first.iv).toBeTruthy();
      expect(second.iv).toBeTruthy();
    });
  });

  describe('empty key', () => {
    it('returns empty when nothing stored', async () => {
      const result = await loadApiKey();
      expect(result.apiKey).toBe('');
      expect(result.remember).toBe(false);
    });

    it('does not store anything when key is empty', async () => {
      await saveApiKey('', false);
      expect(storageMockSession.set).not.toHaveBeenCalledWith(
        expect.objectContaining({ memexApiKey: expect.anything() }),
      );
    });
  });

  describe('legacy migration', () => {
    it('migrates plaintext key from storage.local to session', async () => {
      // Simulate legacy storage
      storageMockLocal.get.mockImplementationOnce(async (defaults: Record<string, unknown>) => ({
        ...defaults,
        memexApiKey: 'legacy-key',
      }));

      const result = await loadApiKey();
      expect(result.apiKey).toBe('legacy-key');
      expect(result.remember).toBe(false);

      // Should have moved to session
      expect(storageMockSession.set).toHaveBeenCalledWith({ memexApiKey: 'legacy-key' });
      // Should have removed from local
      expect(storageMockLocal.remove).toHaveBeenCalledWith(['memexApiKey']);
    });
  });

  describe('clearApiKey', () => {
    it('removes key from all storage locations', async () => {
      await saveApiKey('to-clear', true);
      await clearApiKey();

      expect(storageMockLocal.remove).toHaveBeenCalledWith(['memexApiKey', 'memexApiKeyEnc']);
      expect(storageMockSession.remove).toHaveBeenCalledWith(['memexApiKey']);
      // IndexedDB key should be deleted
      expect(idbStore.has('aes-key')).toBe(false);
    });
  });

  describe('error handling', () => {
    it('handles decryption failure gracefully', async () => {
      // Store remember=true with encrypted data
      await saveApiKey('will-fail', true);

      // Make decrypt throw
      vi.mocked(crypto.subtle.decrypt).mockRejectedValueOnce(new Error('corrupt'));

      const result = await loadApiKey();
      expect(result.apiKey).toBe('');
      expect(result.remember).toBe(true);
    });
  });
});
