/** Vault as returned by GET /api/v1/vaults (NDJSON). */
export interface VaultDTO {
  id: string;
  name: string;
  description: string | null;
  is_active: boolean;
  note_count: number;
  last_note_added_at: string | null;
}

/** Response from POST /api/v1/ingestions. */
export interface IngestResponse {
  note_id: string;
  [key: string]: unknown;
}

/** Data extracted from a page by the content script. */
export interface ExtractedArticle {
  title: string;
  markdown: string;
  excerpt: string;
  byline: string;
  siteName: string;
  url: string;
  hostname: string;
}

export interface ExtractResult {
  error?: string;
  title?: string;
  markdown?: string;
  excerpt?: string;
  byline?: string;
  siteName?: string;
  publishedTime?: string;
  url?: string;
  hostname?: string;
  /** Map of local filename → base64-encoded image bytes. */
  images?: Record<string, string>;
}

/** Message sent from popup to content script. */
export interface ExtractMessage {
  action: 'extract';
}

/** Extension settings stored in browser.storage.local. */
export interface Settings {
  memexServerUrl: string;
  memexApiKey: string;
}
