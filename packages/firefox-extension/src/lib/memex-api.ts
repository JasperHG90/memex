import type { VaultDTO, IngestResponse } from '../types';

/**
 * Route fetch through the background script to bypass CORS restrictions
 * that Firefox imposes on extension pages (popup/options) for requests
 * with custom headers like X-API-Key.
 */
async function bgFetch(
  url: string,
  init?: { method?: string; headers?: Record<string, string>; body?: string },
): Promise<{ ok: boolean; status: number; statusText: string; text: () => string }> {
  const resp = (await browser.runtime.sendMessage({
    action: 'proxyFetch',
    url,
    init,
  })) as { ok: boolean; status: number; statusText: string; body: string };
  return {
    ok: resp.ok,
    status: resp.status,
    statusText: resp.statusText,
    text: () => resp.body,
  };
}

/** Known tracking query parameters to strip for URL canonicalization. */
const TRACKING_PARAMS = new Set([
  'utm_source',
  'utm_medium',
  'utm_campaign',
  'utm_term',
  'utm_content',
  'utm_id',
  'fbclid',
  'gclid',
  'gad_source',
  'mc_cid',
  'mc_eid',
  'ref',
  'ref_src',
  'ref_url',
]);

/**
 * Canonicalize a URL for use as a stable note_key.
 * Strips fragments, known tracking params, and trailing slashes from the path.
 */
export function canonicalizeUrl(raw: string): string {
  try {
    const url = new URL(raw);
    url.hash = '';
    for (const param of TRACKING_PARAMS) {
      url.searchParams.delete(param);
    }
    url.searchParams.sort();
    // Strip trailing slash from path (but keep "/" for root)
    if (url.pathname.length > 1 && url.pathname.endsWith('/')) {
      url.pathname = url.pathname.slice(0, -1);
    }
    return url.toString();
  } catch {
    return raw;
  }
}

/** Encode a UTF-8 string to base64 (handles non-ASCII). */
function utf8ToBase64(str: string): string {
  const bytes = new TextEncoder().encode(str);
  let binary = '';
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary);
}

function authHeaders(apiKey: string): Record<string, string> {
  return apiKey ? { 'X-API-Key': apiKey } : {};
}

/**
 * Fetch all vaults from the Memex server.
 * The /vaults endpoint returns NDJSON (one JSON object per line).
 */
export async function fetchVaults(serverUrl: string, apiKey: string): Promise<VaultDTO[]> {
  const resp = await bgFetch(`${serverUrl}/api/v1/vaults`, {
    headers: authHeaders(apiKey),
  });
  if (!resp.ok) {
    throw new Error(`Failed to fetch vaults: ${resp.status} ${resp.statusText}`);
  }
  const text = resp.text();
  return text
    .trim()
    .split('\n')
    .filter((line) => line.length > 0)
    .map((line) => JSON.parse(line) as VaultDTO);
}

/**
 * Save a note via the ingestion endpoint (markdown content).
 */
export async function saveNote(
  serverUrl: string,
  apiKey: string,
  note: {
    name: string;
    description: string;
    content: string;
    tags: string[];
    vaultId: string | undefined;
    background?: boolean;
    files?: Record<string, string>;
    noteKey?: string;
    userNotes?: string;
  },
): Promise<IngestResponse> {
  const url = `${serverUrl}/api/v1/ingestions${note.background ? '?background=true' : ''}`;
  const resp = await bgFetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...authHeaders(apiKey),
    },
    body: JSON.stringify({
      name: note.name,
      description: note.description,
      content: utf8ToBase64(note.content),
      tags: note.tags,
      vault_id: note.vaultId,
      files: note.files ?? {},
      ...(note.noteKey ? { note_key: note.noteKey } : {}),
      ...(note.userNotes ? { user_notes: note.userNotes } : {}),
    }),
  });
  if (!resp.ok) {
    const body = resp.text();
    throw new Error(`Save failed: ${resp.status} — ${body}`);
  }
  return JSON.parse(resp.text()) as IngestResponse;
}

/**
 * Upload a file (PDF, etc.) via the upload endpoint.
 * The server handles parsing (MarkItDown for non-markdown files).
 */
export async function uploadFile(
  serverUrl: string,
  apiKey: string,
  file: {
    bytes: ArrayBuffer;
    filename: string;
    contentType: string;
    vaultId: string | undefined;
    noteKey?: string;
    userNotes?: string;
  },
): Promise<IngestResponse> {
  const formData = new FormData();
  formData.append('files', new Blob([file.bytes], { type: file.contentType }), file.filename);
  const meta: Record<string, string> = {};
  if (file.vaultId) meta.vault_id = file.vaultId;
  if (file.noteKey) meta.note_key = file.noteKey;
  if (file.userNotes) meta.user_notes = file.userNotes;
  if (Object.keys(meta).length > 0) {
    formData.append('metadata', JSON.stringify(meta));
  }

  const resp = await fetch(`${serverUrl}/api/v1/ingestions/upload?background=true`, {
    method: 'POST',
    headers: authHeaders(apiKey),
    body: formData,
  });
  if (!resp.ok) {
    const body = await resp.text();
    throw new Error(`Upload failed: ${resp.status} — ${body}`);
  }
  return (await resp.json()) as IngestResponse;
}
