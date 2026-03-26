/**
 * Secure API key storage for the Memex extension.
 *
 * Two modes controlled by a "Remember API key" checkbox:
 * - Session-only (default): key stored in browser.storage.session (memory only, lost on restart)
 * - Encrypted persistent: key encrypted with AES-GCM using a non-exportable CryptoKey in IndexedDB
 */

import type { EncryptedKeyData } from '../types';

// ---------------------------------------------------------------------------
// Storage key names
// ---------------------------------------------------------------------------

const IDB_NAME = 'memex-keystore';
const IDB_STORE = 'keys';
const IDB_KEY_ID = 'aes-key';

/** Encrypted key payload in browser.storage.local. */
const STORAGE_ENCRYPTED_KEY = 'memexApiKeyEnc';
/** Boolean: whether the user opted to remember the key. */
const STORAGE_REMEMBER_KEY = 'memexRememberKey';
/** Plain API key — used in session storage and as legacy migration source. */
const LEGACY_STORAGE_KEY = 'memexApiKey';

// ---------------------------------------------------------------------------
// IndexedDB helpers
// ---------------------------------------------------------------------------

function openDB(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(IDB_NAME, 1);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(IDB_STORE)) {
        db.createObjectStore(IDB_STORE);
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

/** Retrieve or generate the AES-GCM encryption key (non-exportable). */
async function getEncryptionKey(): Promise<CryptoKey> {
  const db = await openDB();

  // Try to load existing key
  const existing = await new Promise<CryptoKey | undefined>((resolve, reject) => {
    const tx = db.transaction(IDB_STORE, 'readonly');
    const req = tx.objectStore(IDB_STORE).get(IDB_KEY_ID);
    req.onsuccess = () => resolve(req.result as CryptoKey | undefined);
    req.onerror = () => reject(req.error);
  });

  if (existing) {
    db.close();
    return existing;
  }

  // Generate a new non-exportable key
  const key = await crypto.subtle.generateKey(
    { name: 'AES-GCM', length: 256 },
    false, // extractable: false — JS cannot read raw key bytes
    ['encrypt', 'decrypt'],
  );

  // Store in IndexedDB (supports structured clone of CryptoKey)
  await new Promise<void>((resolve, reject) => {
    const tx = db.transaction(IDB_STORE, 'readwrite');
    const req = tx.objectStore(IDB_STORE).put(key, IDB_KEY_ID);
    req.onsuccess = () => resolve();
    req.onerror = () => reject(req.error);
  });

  db.close();
  return key;
}

async function deleteEncryptionKey(): Promise<void> {
  try {
    const db = await openDB();
    await new Promise<void>((resolve, reject) => {
      const tx = db.transaction(IDB_STORE, 'readwrite');
      const req = tx.objectStore(IDB_STORE).delete(IDB_KEY_ID);
      req.onsuccess = () => resolve();
      req.onerror = () => reject(req.error);
    });
    db.close();
  } catch {
    // IndexedDB may not exist yet — that's fine
  }
}

// ---------------------------------------------------------------------------
// Crypto helpers
// ---------------------------------------------------------------------------

function toBase64(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf);
  let binary = '';
  for (const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary);
}

function fromBase64(b64: string): Uint8Array {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

async function encryptValue(key: CryptoKey, plaintext: string): Promise<EncryptedKeyData> {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const encoded = new TextEncoder().encode(plaintext);
  const ciphertext = await crypto.subtle.encrypt(
    { name: 'AES-GCM', iv: iv as BufferSource },
    key,
    encoded as BufferSource,
  );
  return { iv: toBase64(iv.buffer as ArrayBuffer), ciphertext: toBase64(ciphertext) };
}

async function decryptValue(key: CryptoKey, data: EncryptedKeyData): Promise<string> {
  const iv = fromBase64(data.iv);
  const ciphertext = fromBase64(data.ciphertext);
  const plaintext = await crypto.subtle.decrypt(
    { name: 'AES-GCM', iv: iv as BufferSource },
    key,
    ciphertext as BufferSource,
  );
  return new TextDecoder().decode(plaintext);
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Save the API key using the chosen storage mode.
 *
 * @param apiKey   The plaintext API key
 * @param remember If true, encrypt and persist in storage.local.
 *                 If false, store in storage.session (memory only).
 */
export async function saveApiKey(apiKey: string, remember: boolean): Promise<void> {
  // Clear both locations first
  await browser.storage.local.remove([LEGACY_STORAGE_KEY, STORAGE_ENCRYPTED_KEY]);
  await browser.storage.session.remove([LEGACY_STORAGE_KEY]);

  // Persist the remember preference
  await browser.storage.local.set({ [STORAGE_REMEMBER_KEY]: remember });

  if (!apiKey) return;

  if (remember) {
    const cryptoKey = await getEncryptionKey();
    const encrypted = await encryptValue(cryptoKey, apiKey);
    await browser.storage.local.set({ [STORAGE_ENCRYPTED_KEY]: encrypted });
  } else {
    await browser.storage.session.set({ [LEGACY_STORAGE_KEY]: apiKey });
  }
}

/**
 * Load the API key from whichever storage mode was used.
 * Handles migration from legacy plaintext storage.
 */
export async function loadApiKey(): Promise<{ apiKey: string; remember: boolean }> {
  // 1. Migration: check for legacy plaintext key in storage.local
  const legacy = await browser.storage.local.get({ [LEGACY_STORAGE_KEY]: '' });
  const legacyKey = (legacy as Record<string, string>)[LEGACY_STORAGE_KEY];
  if (legacyKey) {
    // Move to session storage (safe default), delete legacy entry
    await browser.storage.session.set({ [LEGACY_STORAGE_KEY]: legacyKey });
    await browser.storage.local.remove([LEGACY_STORAGE_KEY]);
    return { apiKey: legacyKey, remember: false };
  }

  // 2. Load remember preference
  const prefs = await browser.storage.local.get({ [STORAGE_REMEMBER_KEY]: false });
  const remember = (prefs as Record<string, boolean>)[STORAGE_REMEMBER_KEY];

  // 3. Encrypted persistent path
  if (remember) {
    const stored = await browser.storage.local.get({ [STORAGE_ENCRYPTED_KEY]: null });
    const encData = (stored as Record<string, EncryptedKeyData | null>)[STORAGE_ENCRYPTED_KEY];
    if (encData) {
      try {
        const cryptoKey = await getEncryptionKey();
        const apiKey = await decryptValue(cryptoKey, encData);
        return { apiKey, remember: true };
      } catch {
        // Decryption failed (key lost, data corrupt) — clear and let user re-enter
        await browser.storage.local.remove([STORAGE_ENCRYPTED_KEY]);
      }
    }
    return { apiKey: '', remember: true };
  }

  // 4. Session-only path
  const session = await browser.storage.session.get({ [LEGACY_STORAGE_KEY]: '' });
  const sessionKey = (session as Record<string, string>)[LEGACY_STORAGE_KEY];
  return { apiKey: sessionKey || '', remember: false };
}

/**
 * Clear the API key from all storage locations and delete the encryption key.
 */
export async function clearApiKey(): Promise<void> {
  await browser.storage.local.remove([LEGACY_STORAGE_KEY, STORAGE_ENCRYPTED_KEY]);
  await browser.storage.session.remove([LEGACY_STORAGE_KEY]);
  await deleteEncryptionKey();
}
