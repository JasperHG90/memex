import type { VaultDTO, IngestResponse } from '../types';

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
  const resp = await fetch(`${serverUrl}/api/v1/vaults`, {
    headers: authHeaders(apiKey),
  });
  if (!resp.ok) {
    throw new Error(`Failed to fetch vaults: ${resp.status} ${resp.statusText}`);
  }
  const text = await resp.text();
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
  },
): Promise<IngestResponse> {
  const url = `${serverUrl}/api/v1/ingestions${note.background ? '?background=true' : ''}`;
  const resp = await fetch(url, {
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
    }),
  });
  if (!resp.ok) {
    const body = await resp.text();
    throw new Error(`Save failed: ${resp.status} — ${body}`);
  }
  return (await resp.json()) as IngestResponse;
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
  },
): Promise<IngestResponse> {
  const formData = new FormData();
  formData.append('files', new Blob([file.bytes], { type: file.contentType }), file.filename);
  if (file.vaultId) {
    formData.append('metadata', JSON.stringify({ vault_id: file.vaultId }));
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
